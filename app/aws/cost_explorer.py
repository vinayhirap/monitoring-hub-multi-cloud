# app/aws/cost_explorer.py
"""
Actual AWS CloudWatch charges from Cost Explorer (read-only).

WHY: app/collector/api_usage.py only ESTIMATES cost (units x list price, free tiers ignored). This module asks AWS what
it actually billed for the CloudWatch service, so the estimate can be checked against reality.

PERMISSION: exactly one IAM action, ce:GetCostAndUsage (deploy/iam/monitoring-hub-cost-read.json). It is read-only,
cannot change anything, and Cost Explorer does not support resource-level restriction, so Resource is "*".

COST OF ASKING: AWS bills every Cost Explorer API request (each page) $0.01. So results are cached in
aws_cost_actuals and refreshed at most once per REFRESH_HOURS per account (default 24h = about $0.30 per account per
month); a manual refresh is limited to one per MANUAL_COOLDOWN_MINUTES. Page loads never call Cost Explorer.

DATA LIMITS: Cost Explorer lags the real usage by up to ~24-48h, so the newest day is partial ("estimated") and usually
lower than what CloudOps estimates for the same day. The endpoint is always us-east-1, whatever region usage ran in.
A member account only sees its own charges; a management (payer) account sees every linked account's.

Off by default: nothing here makes an AWS call unless ACTUAL_COST_ENABLED is set.
"""
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

CE_REGION = "us-east-1"
CE_SERVICE = "AmazonCloudWatch"
LOOKBACK_DAYS = 31                 # default window of fetch_cloudwatch_cost(); refresh_account passes the two below
BACKFILL_DAYS = 90                 # first read of an account: enough history for the explorer
REFRESH_DAYS = 14                  # every later read: only the days AWS can still restate (about 48 h) plus margin
REFRESH_HOURS = 24
MANUAL_COOLDOWN_MINUTES = 60
MAX_PAGES = 5
MAX_PAGES_BACKFILL = 12            # one-off, so a 90 day first read is not cut off
CE_PRICE_PER_REQUEST = 0.01

STATUS_OK = "ok"
STATUS_PERMISSION = "permission_missing"
STATUS_NOT_ENABLED = "not_enabled"
STATUS_ERROR = "error"

_refresh_lock = threading.Lock()
_CHECK_EVERY_SECONDS = 1800
_last_check = 0.0


def enabled():
    return os.getenv("ACTUAL_COST_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


# Usage-type prefix -> region. AWS prefixes every regional usage type (APS3-CW:Requests); no prefix means us-east-1.
# Only prefixes that are documented/verified are listed; an unknown prefix is shown as the raw prefix, never guessed.
REGION_PREFIX = {
    "USE1": "us-east-1", "USE2": "us-east-2", "USW1": "us-west-1", "USW2": "us-west-2",
    "EUW1": "eu-west-1", "EUW2": "eu-west-2", "EUW3": "eu-west-3", "EUC1": "eu-central-1", "EUN1": "eu-north-1",
    "APS1": "ap-southeast-1", "APS2": "ap-southeast-2", "APS3": "ap-south-1", "APN1": "ap-northeast-1",
    "APN2": "ap-northeast-2", "APN3": "ap-northeast-3", "CAN1": "ca-central-1", "SAE1": "sa-east-1",
}


def usage_region(usage_type):
    """'APS3-CW:Requests' -> 'ap-south-1'. No prefix (e.g. 'CW:Requests') -> 'us-east-1'. Unknown prefix -> the raw
    prefix (so a new region is visible and never mislabelled)."""
    u = str(usage_type or "")
    head, sep, _ = u.partition("-")
    if sep and head.isalnum() and head.upper() == head and len(head) in (4, 5) and head[-1].isdigit():
        return REGION_PREFIX.get(head, head)
    return "us-east-1"


# Who causes a charge. CloudOps only ever READS CloudWatch (verified: no PutMetricData, PutMetricAlarm or log/flow-log
# configuration anywhere in this repo), so only GetMetricData can be attributed to its polling. Everything else on the
# CloudWatch bill is created by agents, alarms, log sources or other tools already in the monitored accounts.
CAT_CLOUDOPS = "cloudops_reads"
CAT_AGENTS = "agents_alarms"
CAT_LOGS = "logs"
CAT_OTHER = "other"
CATEGORY_LABELS = {
    CAT_CLOUDOPS: "CloudOps polling (GetMetricData)",
    CAT_AGENTS: "Agents, alarms and API requests",
    CAT_LOGS: "Logs and log delivery",
    CAT_OTHER: "Other CloudWatch charges",
}


def usage_category(usage_type):
    """usage type -> one of the CAT_* keys. Order matters: S3 egress and vended logs contain 'Logs'/'Bytes' words but are
    log delivery, and CW:Requests also carries PutMetricData from agents, so it is NOT attributed to CloudOps."""
    u = str(usage_type or "")
    if "GMD-Metrics" in u:
        return CAT_CLOUDOPS
    if "S3-Egress" in u or "VendedLog" in u or "DataProcessing" in u or "DataIngestion" in u or "TimedStorage" in u or "Logs" in u:
        return CAT_LOGS
    if "MetricMonitorUsage" in u or "AlarmMonitorUsage" in u or "Alarm" in u or u.endswith(":Requests"):
        return CAT_AGENTS
    return CAT_OTHER


def classify_usage_type(usage_type):
    """Best-effort human label for a CloudWatch usage type. The raw usage type is always shown next to it, because
    AWS can add types; anything not recognised is labelled 'Other CloudWatch charge', never dropped.
    Order matters: S3 egress is checked BEFORE 'Logs' because 'S3-Egress-Bytes-WAFLogs' contains 'Logs' but is a
    data-transfer charge for log delivery, not log ingestion/storage."""
    u = str(usage_type or "")
    if "S3-Egress" in u:
        return "Log delivery to S3 (data transfer)"
    if "GMD-Metrics" in u:
        return "GetMetricData (metrics requested)"
    if u.endswith("CW:Requests") or u.endswith(":Requests"):
        return "API requests (ListMetrics, GetMetricStatistics, others)"
    if "MetricMonitorUsage" in u:
        return "Custom metrics stored"
    if "AlarmMonitorUsage" in u or "Alarm" in u:
        return "Alarms"
    if "VendedLog" in u:
        return "Vended logs (delivered)"
    if "DataProcessing" in u or "DataIngestion" in u or "TimedStorage" in u or "Logs" in u:
        return "Logs"
    return "Other CloudWatch charge"


def classify_error(exc):
    """boto ClientError (or anything) -> (status, short message). Never raises."""
    code = ""
    try:
        code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "") or ""
    except Exception:
        pass
    text = f"{code}: {exc}"[:400]
    if code in ("AccessDeniedException", "AccessDenied", "UnauthorizedOperation"):
        return STATUS_PERMISSION, text
    if code in ("DataUnavailableException", "OptInRequired", "SubscriptionRequiredException"):
        return STATUS_NOT_ENABLED, text
    return STATUS_ERROR, text


def parse_cost_response(pages):
    """pages: list of get_cost_and_usage responses -> list of dicts
    {billed_account_id, usage_type, usage_date (date), amount_usd (float), estimated (bool)}.
    Groups come back as Keys [usage_type, linked_account] (the GroupBy order). Unreadable amounts are skipped and
    counted nowhere as zero. Negative amounts (credits/refunds) are kept: they are part of the real bill."""
    out = []
    for page in pages or ():
        if not isinstance(page, dict):
            continue
        for period in page.get("ResultsByTime", []) or ():
            start = (period.get("TimePeriod") or {}).get("Start")
            try:
                d = date.fromisoformat(start)
            except (TypeError, ValueError):
                continue
            est = bool(period.get("Estimated"))
            for g in period.get("Groups", []) or ():
                keys = g.get("Keys") or []
                if len(keys) < 2:
                    continue
                try:
                    amt = float(((g.get("Metrics") or {}).get("UnblendedCost") or {}).get("Amount"))
                except (TypeError, ValueError):
                    continue
                if amt != amt:
                    continue
                uq = (g.get("Metrics") or {}).get("UsageQuantity") or {}
                try:
                    qty = float(uq.get("Amount"))
                    if qty != qty:
                        qty = None
                except (TypeError, ValueError):
                    qty = None
                out.append({"usage_type": str(keys[0]), "billed_account_id": str(keys[1]),
                            "usage_date": d, "amount_usd": amt, "estimated": est,
                            "quantity": qty, "unit": (str(uq.get("Unit"))[:40] if uq.get("Unit") else None)})
    return out


def fetch_cloudwatch_cost_ex(session, today=None, days=LOOKBACK_DAYS, max_pages=MAX_PAGES):
    """One account's CloudWatch charges for the last `days` days, daily, by usage type and billed account, with both
    cost and usage quantity (same request, no extra charge). Returns (rows, ce_requests, truncated). `truncated` is
    True when AWS still had more pages after max_pages requests: the caller must say so, never present it as complete."""
    from app.aws.boto_config import STANDARD_RETRY
    today = today or datetime.now(timezone.utc).date()
    ce = session.client("ce", region_name=CE_REGION, config=STANDARD_RETRY)
    kwargs = {
        "TimePeriod": {"Start": (today - timedelta(days=days)).isoformat(),
                       "End": (today + timedelta(days=1)).isoformat()},   # End is exclusive
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost", "UsageQuantity"],
        "Filter": {"Dimensions": {"Key": "SERVICE", "Values": [CE_SERVICE]}},
        "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}, {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}],
    }
    pages, requests, truncated = [], 0, False
    while requests < max_pages:
        resp = ce.get_cost_and_usage(**kwargs)
        requests += 1
        pages.append(resp)
        token = resp.get("NextPageToken")
        if not token:
            break
        kwargs["NextPageToken"] = token
    else:
        truncated = True
    return parse_cost_response(pages), requests, truncated


def fetch_cloudwatch_cost(session, today=None):
    """Backwards-compatible wrapper: (rows, ce_requests)."""
    rows, requests, _ = fetch_cloudwatch_cost_ex(session, today)
    return rows, requests


# ── persistence ────────────────────────────────────────────────────────────
def _accounts():
    from app.db import get_connection
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT id, account_name, account_id, role_arn, auth_mode, external_id, default_region "
                    "FROM aws_accounts WHERE status = 'active' AND provider = 'aws'")
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def _last_attempt(account_pk):
    from app.db import get_connection
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT UNIX_TIMESTAMP(MAX(fetched_at)) FROM aws_cost_fetch_log WHERE aws_account_id = %s",
                    (account_pk,))
        r = cur.fetchone()
        return float(r[0]) if r and r[0] is not None else None
    finally:
        cur.close()
        conn.close()


def _backfilled(account_pk):
    """True once a read of at least BACKFILL_DAYS succeeded for this account (so later reads only need REFRESH_DAYS).
    Any failure (no database, column not there yet because migration 125 is pending) means: read the long window."""
    conn = cur = None
    try:
        from app.db import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT MAX(window_days) FROM aws_cost_fetch_log WHERE aws_account_id = %s AND status = 'ok'",
                    (account_pk,))
        r = cur.fetchone()
        return bool(r and r[0] is not None and int(r[0]) >= BACKFILL_DAYS)
    except Exception:
        return False
    finally:
        for h in (cur, conn):
            try:
                if h is not None:
                    h.close()
            except Exception:
                pass


def _save(account, status, requests, rows, error, window_days=None):
    from app.db import get_connection
    conn = get_connection()
    cur = conn.cursor()
    try:
        try:
            for r in rows:
                cur.execute(
                    "INSERT INTO aws_cost_actuals (billed_account_id, usage_type, usage_date, amount_usd, estimated, fetched_via, "
                    "usage_quantity, usage_unit) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE amount_usd=VALUES(amount_usd), "
                    "estimated=VALUES(estimated), fetched_via=VALUES(fetched_via), usage_quantity=VALUES(usage_quantity), "
                    "usage_unit=VALUES(usage_unit), fetched_at=CURRENT_TIMESTAMP",
                    (r["billed_account_id"], r["usage_type"][:120], r["usage_date"], round(r["amount_usd"], 6),
                     1 if r["estimated"] else 0, account["id"], r.get("quantity"), r.get("unit")))
            cur.execute("INSERT INTO aws_cost_fetch_log (aws_account_id, account_name, status, ce_requests, rows_written, error, window_days) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (account["id"], account.get("account_name"), status, requests, len(rows), (error or None) and error[:500], window_days))
        except Exception as e:
            if "1054" not in str(e) and "Unknown column" not in str(e):
                raise
            conn.rollback()                # migration 125 not applied yet: store what the old schema can hold
            for r in rows:
                cur.execute(
                    "INSERT INTO aws_cost_actuals (billed_account_id, usage_type, usage_date, amount_usd, estimated, fetched_via) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE amount_usd=VALUES(amount_usd), "
                    "estimated=VALUES(estimated), fetched_via=VALUES(fetched_via), fetched_at=CURRENT_TIMESTAMP",
                    (r["billed_account_id"], r["usage_type"][:120], r["usage_date"], round(r["amount_usd"], 6),
                     1 if r["estimated"] else 0, account["id"]))
            cur.execute("INSERT INTO aws_cost_fetch_log (aws_account_id, account_name, status, ce_requests, rows_written, error) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (account["id"], account.get("account_name"), status, requests, len(rows), (error or None) and error[:500]))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def refresh_account(account):
    """Fetch and store one account. Never raises; returns {account, status, ce_requests, rows, error}.
    First read: BACKFILL_DAYS of history. Every later read: REFRESH_DAYS (AWS only restates the last ~48 h), which keeps
    the per-day Cost Explorer charge at one request per account instead of re-reading months of settled data."""
    status, requests, rows, error, window = STATUS_OK, 0, [], None, REFRESH_DAYS
    try:
        from app.aws.sts import get_boto3_session
        window = REFRESH_DAYS if _backfilled(account["id"]) else BACKFILL_DAYS
        rows, requests, truncated = fetch_cloudwatch_cost_ex(
            get_boto3_session(account), days=window,
            max_pages=MAX_PAGES if window == REFRESH_DAYS else MAX_PAGES_BACKFILL)
        if truncated:
            status, error = STATUS_ERROR, f"Cost Explorer returned more pages than the {requests}-request limit: data is incomplete"
    except Exception as e:
        status, error = classify_error(e)
        logger.warning(f"[actual-cost] {account.get('account_name')}: {status}: {error}")
    try:
        _save(account, status, requests, rows, error, window_days=window if status == STATUS_OK else None)
    except Exception:
        logger.exception("[actual-cost] could not store result")
    return {"account": account.get("account_name"), "status": status, "ce_requests": requests,
            "rows": len(rows), "error": error}


def refresh_due(force=False, now=None):
    """Refresh every active AWS account whose last attempt is older than REFRESH_HOURS (force: MANUAL_COOLDOWN_MINUTES).
    One refresh at a time per process. Returns the list of per-account results; [] when disabled or busy."""
    if not enabled():
        return []
    if not _refresh_lock.acquire(blocking=False):
        return []
    try:
        now = time.time() if now is None else now
        min_age = (MANUAL_COOLDOWN_MINUTES * 60) if force else (REFRESH_HOURS * 3600)
        results = []
        for acct in _accounts():
            last = _last_attempt(acct["id"])
            if last is not None and now - last < min_age:
                continue
            results.append(refresh_account(acct))
        return results
    except Exception:
        logger.exception("[actual-cost] refresh failed")
        return []
    finally:
        _refresh_lock.release()


def maybe_refresh_async():
    """Scheduler hook: cheap when nothing is due (one small query per account), never blocks the polling loop."""
    global _last_check
    if not enabled() or _refresh_lock.locked() or time.time() - _last_check < _CHECK_EVERY_SECONDS:
        return
    _last_check = time.time()
    threading.Thread(target=refresh_due, name="actual-cost-refresh", daemon=True).start()
