# app/collector/tag_sync.py
"""
Tag knowledge for resource types whose discovery stores no real AWS tags (workstream B).

One paginated Resource Groups Tagging API sweep (GetResources) per account and region per discovery cycle,
matched to `resources` rows by app/aws/arn_match.py (pure, tested). The result is stored in
resources.synced_tags / tags_synced_at (migration 095), NEVER in resources.tags: discovery rewrites `tags`
every run and the legacy alert evaluator reads tags["Environment"], so merging would change alerting.

Flag TAG_SYNC_ENABLED (default off). Off means: no AWS call, no query on the new columns, nothing changes.

What the sweep proves, and what it does not (see arn_match.py's docstring for the full rule):
  * a matched ARN proves the resource's tags;
  * absence is turned into "untagged" only for ABSENCE_VERIFIED_TYPES, only from a completed sweep that
    returned something, and only when the type's matching passes the canary;
  * any failure (AccessDenied, throttling, a page that did not arrive) discards that sweep entirely and
    leaves every stored value as it was. Stored values older than TAG_SYNC_MAX_AGE_HOURS (default 6) are
    ignored by readers, so a permanent failure degrades to not_checked instead of freezing a stale claim.

Never raises into the discovery loop.
"""
import json
import logging
import os
import time

from app import environment_classifier as ec
from app.aws import arn_match

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_HOURS = 6
REFRESH_MINUTES = 60              # re-stamp an unchanged result at most this often (keeps writes small)
STALE_RESOURCE_HOURS = 48         # same freshness rule as env_sync
_CHUNK = 500
_ERROR_LOG_INTERVAL = 24 * 3600
_last_error_log = {}              # (account id, region, code) -> time of the last log line
_columns_ok = False


def enabled():
    return ec.tag_sync_enabled()


def max_age_hours():
    try:
        v = int(os.getenv("TAG_SYNC_MAX_AGE_HOURS", str(DEFAULT_MAX_AGE_HOURS)))
        return v if v > 0 else DEFAULT_MAX_AGE_HOURS
    except ValueError:
        return DEFAULT_MAX_AGE_HOURS


def absence_types():
    """TAG_SYNC_ABSENCE_TYPES may only NARROW arn_match.ABSENCE_VERIFIED_TYPES: unset = all of them,
    'none' = no untagged claims at all, 'logs,kms' = just those. It can never add a type."""
    raw = os.getenv("TAG_SYNC_ABSENCE_TYPES", "").strip().lower()
    if not raw:
        return arn_match.ABSENCE_VERIFIED_TYPES
    if raw == "none":
        return frozenset()
    return arn_match.ABSENCE_VERIFIED_TYPES & {t.strip() for t in raw.split(",") if t.strip()}


def columns_ready(cursor):
    """True when migration 095 is applied. A positive answer is cached; a negative one is re-checked."""
    global _columns_ok
    if _columns_ok:
        return True
    cursor.execute("SELECT COUNT(*) AS n FROM information_schema.columns WHERE table_schema = DATABASE() "
                   "AND table_name = 'resources' AND column_name IN ('synced_tags', 'tags_synced_at')")
    row = cursor.fetchone()
    n = row["n"] if isinstance(row, dict) else row[0]
    _columns_ok = int(n) == 2
    return _columns_ok


def synced_select(cursor, alias="r"):
    """SQL select-list fragment giving the FRESH tag-sync result as `synced_tags` (NULL when the flag is off,
    the migration is missing, or the stored value is older than the max age). Safe to call whenever; adds
    nothing to the query unless the flag is on and migration 095 is applied."""
    if enabled():
        try:
            if columns_ready(cursor):
                return (f"IF({alias}.tags_synced_at >= DATE_SUB(NOW(), INTERVAL {int(max_age_hours())} HOUR), "
                        f"{alias}.synced_tags, NULL) AS synced_tags")
        except Exception as e:
            logger.debug(f"[tag-sync] column check failed, treating as off: {e}")
    return "NULL AS synced_tags"


# ---------------------------------------------------------------------------------------------
# AWS sweep
# ---------------------------------------------------------------------------------------------
def sweep_region(session, region):
    """One paginated GetResources for `region`. -> {"region", "ok", "items", "pages", "error"}.
    ANY exception (AccessDenied, throttling after the client's own retries, network) makes the whole
    sweep ok=False and returns no items: a partial result must never be read as "the rest is untagged"."""
    out = {"region": region, "ok": False, "items": [], "pages": 0, "error": None}
    try:
        from app.aws.boto_config import STANDARD_RETRY
        client = session.client("resourcegroupstaggingapi", region_name=region, config=STANDARD_RETRY)
        items = []
        for page in client.get_paginator("get_resources").paginate(ResourcesPerPage=100):
            out["pages"] += 1
            for m in page.get("ResourceTagMappingList", []):
                arn = m.get("ResourceARN")
                if arn:
                    items.append({"arn": arn, "tags": {t.get("Key"): t.get("Value") for t in m.get("Tags", [])
                                                         if t.get("Key")}})
        out["items"] = items
        out["ok"] = True
    except Exception as e:
        out["error"] = e
        out["items"] = []
    return out


def _error_code(e):
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        return (resp.get("Error") or {}).get("Code") or type(e).__name__
    return type(e).__name__


def _log_sweep_failure(account, region, e):
    code = _error_code(e)
    key = (account.get("id"), region, code)
    now = time.time()
    if now - _last_error_log.get(key, 0) < _ERROR_LOG_INTERVAL:
        return
    _last_error_log[key] = now
    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
        logger.warning(f"[tag-sync] {account.get('account_name')}/{region}: {code} on tag:GetResources -- tag status "
                       f"stays as it was (not_checked) for the extra types. Grant tag:GetResources to the monitoring role.")
    else:
        logger.warning(f"[tag-sync] {account.get('account_name')}/{region}: sweep failed ({code}: {e}); "
                       f"stored tag status left untouched (logged once per day)")


def regions_for(account, rows):
    """Regions to sweep: the discovery region, plus us-east-1 when the account has S3 / CloudFront rows (global
    services, positive matching only)."""
    regions = []
    default = account.get("default_region")
    if default:
        regions.append(default)
    if any(str(r.get("resource_type")).lower() in arn_match.GLOBAL_TYPES for r in rows) and "us-east-1" not in regions:
        regions.append("us-east-1")
    return regions


# ---------------------------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------------------------
_ROWS_SQL = """
    SELECT r.id, r.resource_type, r.resource_id, r.region, r.tags,
           r.synced_tags AS stored_synced,
           TIMESTAMPDIFF(MINUTE, r.tags_synced_at, NOW()) AS synced_age_min
    FROM resources r
    WHERE r.aws_account_id = %s
      AND (r.instance_state IS NULL OR r.instance_state != 'terminated')
      AND (r.last_seen_at IS NULL OR r.last_seen_at >= DATE_SUB(NOW(), INTERVAL %s HOUR))
      AND r.resource_type IN ({types})
"""


def load_rows(cursor, account_pk):
    types = sorted(arn_match.SYNC_TYPES)
    cursor.execute(_ROWS_SQL.format(types=", ".join(["%s"] * len(types))),
                   [account_pk, STALE_RESOURCE_HOURS, *types])
    return cursor.fetchall()


def _canon(d):
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


def writes_for(rows, decisions):
    """-> [(synced_json, row id)] for decisions whose stored value changed or is due a refresh."""
    out = []
    for r in rows:
        d = decisions.get(r["id"])
        if d is None:
            continue
        new = _canon(d.tags)
        old = ec.parse_synced(r.get("stored_synced"))
        age = r.get("synced_age_min")
        if old is not None and _canon(old) == new and age is not None and age < REFRESH_MINUTES:
            continue
        out.append((new, r["id"]))
    return out


def _get_accounts():
    from app.collector.discovery.runner import _get_active_accounts
    return _get_active_accounts()


def _get_session(account):
    from app.collector.discovery.runner import _get_session
    return _get_session(account)


def run_tag_sync(account_ids=None, dry_run=False, sweeper=None):
    """Sweep, match, store. Returns a report dict; never raises.
        {"accounts": {account name: {"regions": {...}, "types": {...}, "written": n}}, "calls": pages,
         "written": n, "skipped": reason|None}
    `sweeper(account, region) -> sweep dict` replaces the AWS call (tests, report script)."""
    from app.db import get_connection
    report = {"accounts": {}, "calls": 0, "written": 0, "skipped": None, "dry_run": bool(dry_run)}
    try:
        accounts = [a for a in _get_accounts() if account_ids is None or a["id"] in account_ids]
    except Exception as e:
        report["skipped"] = f"could not list accounts: {e}"
        return report

    for account in accounts:
        number = str(account.get("account_id") or "").strip()
        name = account.get("account_name") or account["id"]
        if not number:
            report["accounts"][name] = {"skipped": "no AWS account number on the account row"}
            continue
        conn = get_connection()
        try:
            cur = conn.cursor(dictionary=True)
            try:
                if not columns_ready(cur):
                    report["skipped"] = "migration 095 not applied"
                    logger.debug("[tag-sync] migration 095 not applied yet -- skipping")
                    return report
                rows = load_rows(cur, account["id"])
            finally:
                cur.close()
            conn.commit()
            if not rows:
                report["accounts"][name] = {"regions": {}, "types": {}, "written": 0}
                continue

            session = None if sweeper else _get_session(account)
            if sweeper is None and session is None:
                report["accounts"][name] = {"skipped": "no session"}
                continue
            sweeps = []
            for region in regions_for(account, rows):
                sw = sweeper(account, region) if sweeper else sweep_region(session, region)
                report["calls"] += sw.get("pages", 0) or 0
                if not sw.get("ok"):
                    _log_sweep_failure(account, region, sw.get("error") or RuntimeError("sweep failed"))
                sweeps.append(sw)

            plan = arn_match.plan_account(rows, sweeps, number, absence_types())
            writes = writes_for(rows, plan["decisions"])
            written = 0
            if writes and not dry_run:
                writes.sort(key=lambda w: w[1])          # ascending id, same lock order as env_sync
                wcur = conn.cursor()
                try:
                    for i in range(0, len(writes), _CHUNK):
                        wcur.executemany("UPDATE resources SET synced_tags = %s, tags_synced_at = NOW() WHERE id = %s",
                                         writes[i:i + _CHUNK])
                        conn.commit()
                        written += len(writes[i:i + _CHUNK])
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    wcur.close()
            report["written"] += written
            report["accounts"][name] = {
                "regions": {s["region"]: {"ok": bool(s.get("ok")), "resources": len(s.get("items") or []),
                                          "pages": s.get("pages", 0)} for s in sweeps},
                "types": plan["types"], "written": written,
            }
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            report["accounts"][name] = {"error": str(e)}
            logger.warning(f"[tag-sync] {name} failed (non-fatal): {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    if report["calls"]:
        try:
            from app.collector import api_usage
            api_usage.record("aws", "tag_sync", calls=report["calls"], units=0)
        except Exception:
            pass
    return report


def sync_after_discovery():
    """Discovery-loop hook: runs BEFORE env_sync.sync_after_discovery so the classification pass sees the fresh
    result. Does nothing, and makes no AWS call, unless TAG_SYNC_ENABLED is on. Never raises."""
    if not enabled():
        return
    try:
        rep = run_tag_sync()
        if rep.get("written"):
            logger.info(f"[tag-sync] stored tag results for {rep['written']} resources in {rep['calls']} API call(s)")
    except Exception as e:
        logger.warning(f"[tag-sync] failed (non-fatal): {e}")
