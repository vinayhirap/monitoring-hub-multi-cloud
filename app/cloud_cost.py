# app/cloud_cost.py
"""
Actual (billed) cost of CloudOps's own paid metric calls on Azure and GCP. Read-only. The AWS equivalent is
app/aws/cost_explorer.py; both feed the same Cloud Cost page.

Nothing here makes a cloud call unless ACTUAL_COST_ENABLED=1 AND an active Azure subscription / GCP project exists.

AZURE  Cost Management Query API (ActualCost, daily, MeterCategory "Azure Monitor", grouped by meter and location).
       Needs the role "Cost Management Reader" on the subscription for the same service principal CloudOps already uses.
       No extra charge for the query. Billed in USD columns when the account exposes them; otherwise the read fails with
       a clear message instead of showing local-currency numbers as dollars.

GCP    BigQuery Cloud Billing export (service "Cloud Monitoring", per project). GCP has no API that returns cost, so the
       billing account must export to BigQuery (Billing > Billing export > Standard usage cost; it is not retroactive)
       and GCP_BILLING_EXPORT_TABLE must name the table as project.dataset.table. The service account needs
       roles/bigquery.jobUser on a project and roles/bigquery.dataViewer on the export dataset. Credits (including the
       monthly free tier) are netted off, so the figure is what is actually charged. The query itself is a BigQuery job
       and is billed by bytes scanned; that is recorded per refresh (the first TiB each month is free at Google).

WHAT COUNTS AS CLOUDOPS: only the meter/SKU for metric API calls (CloudOps reads metrics; it never writes any). Matching
       is by regular expression, overridable per deployment (AZURE_CLOUDOPS_METER_REGEX, GCP_CLOUDOPS_SKU_REGEX), because
       Microsoft and Google rename meters. Anything else in the same service is counted as "other" and only totalled in
       the page footnote, so a wrong pattern shows up as zero CloudOps cost next to a non-zero "not shown" total instead
       of silently hiding or inventing a number.

NOT VERIFIED AGAINST A LIVE AZURE/GCP ACCOUNT when written: the response shapes follow the vendors' documentation and are
covered by unit tests with documented-shape fixtures. The first real refresh reports its outcome per account on the page.
"""
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

PROVIDERS = ("azure", "gcp")
BACKFILL_DAYS = 90
REFRESH_DAYS = 14
REFRESH_HOURS = 24
MANUAL_COOLDOWN_MINUTES = 60
AZURE_API_VERSION = "2023-11-01"
AZURE_MAX_PAGES = 12

STATUS_OK = "ok"
STATUS_PERMISSION = "permission_missing"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_ERROR = "error"

CAT_CLOUDOPS = "cloudops_reads"
CAT_OTHER = "other"
CATEGORY_LABELS = {CAT_CLOUDOPS: "CloudOps metric API calls", CAT_OTHER: "Other charges in the same service"}

_refresh_lock = threading.Lock()
_CHECK_EVERY_SECONDS = 1800
_last_check = 0.0


class CostFetchError(Exception):
    """A read failed in a way worth telling the operator about. `status` is one of the STATUS_* values."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def enabled():
    return os.getenv("ACTUAL_COST_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


# ── classification (what is CloudOps's cost) ─────────────────────────────────
# Azure Monitor charges metric API calls per 1,000 calls; GCP charges Monitoring API requests. Ingestion, logs, alerts
# and similar are not CloudOps's. Defaults are best-effort and overridable (see module docstring).
_AZURE_DEFAULT = r"(?i)(metric.*(quer|api|call|request))|((quer|api|call|request).*metric)"
_AZURE_EXCLUDE = r"(?i)(alert|log|ingest|retention|custom metric|application insights|prometheus)"
_GCP_DEFAULT = r"(?i)(api|request|read)"
_GCP_EXCLUDE = r"(?i)(ingest|volume|log|trace|alert|uptime|profil|prometheus)"


def _regex(env_name, default):
    raw = os.getenv(env_name, "").strip()
    try:
        return re.compile(raw if raw else default)
    except re.error:
        logger.warning(f"{env_name} is not a valid regular expression; using the default")
        return re.compile(default)


def cloudops_category(provider, sku):
    """CAT_CLOUDOPS for the metric-API-call meter/SKU of that provider, CAT_OTHER for everything else."""
    s = str(sku or "")
    if provider == "azure":
        ok = _regex("AZURE_CLOUDOPS_METER_REGEX", _AZURE_DEFAULT).search(s)
        bad = not os.getenv("AZURE_CLOUDOPS_METER_REGEX", "").strip() and re.search(_AZURE_EXCLUDE, s)
    elif provider == "gcp":
        ok = _regex("GCP_CLOUDOPS_SKU_REGEX", _GCP_DEFAULT).search(s)
        bad = not os.getenv("GCP_CLOUDOPS_SKU_REGEX", "").strip() and re.search(_GCP_EXCLUDE, s)
    else:
        return CAT_OTHER
    return CAT_CLOUDOPS if ok and not bad else CAT_OTHER


def encode_usage_type(region, sku):
    """Cost Explorer-shaped rows carry one 'usage type' string; for Azure/GCP it is 'region|sku'."""
    return f"{region or ''}|{sku or ''}"


def split_usage_type(usage_type):
    region, _, sku = str(usage_type or "").partition("|")
    return region, sku


def explorer_adapter(provider):
    """Functions build_explorer() uses to read a provider's rows (it defaults to the AWS ones)."""
    return {
        "region_of": lambda u: split_usage_type(u)[0] or "global",
        "category_of": lambda u: cloudops_category(provider, split_usage_type(u)[1]),
        "label_of": lambda u: split_usage_type(u)[1] or "(unnamed)",
        "category_labels": CATEGORY_LABELS,
    }


# ── Azure ────────────────────────────────────────────────────────────────────
def _azure_date(v):
    """UsageDate comes back as 20261015 (number) for daily queries; accept ISO strings too."""
    s = str(v).strip()
    try:
        if re.fullmatch(r"\d{8}", s):
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_azure_query(payload, subscription_id):
    """Cost Management query response -> [{billed_account, region, sku, usage_date, amount_usd, quantity, unit}].
    Raises CostFetchError if the amounts are not in USD (a local-currency number must never be shown as dollars)."""
    props = (payload or {}).get("properties") or payload or {}
    cols = [str(c.get("name", "")).lower() for c in props.get("columns", [])]

    def col(*names):
        for nm in names:
            if nm in cols:
                return cols.index(nm)
        return None

    i_cost = col("costusd", "pretaxcostusd")
    usd_cols = i_cost is not None
    if i_cost is None:
        i_cost = col("cost", "pretaxcost")
    i_qty, i_date = col("usagequantity"), col("usagedate")
    i_meter, i_loc, i_cur = col("meter"), col("resourcelocation"), col("currency")
    if i_cost is None or i_date is None:
        raise CostFetchError(STATUS_ERROR, "Cost Management response has no cost or date column")
    merged = {}
    for row in props.get("rows", []):
        if not usd_cols and i_cur is not None and str(row[i_cur]).upper() != "USD":
            raise CostFetchError(STATUS_ERROR, f"billed in {row[i_cur]} and no USD column is available for this subscription")
        d = _azure_date(row[i_date])
        if d is None:
            continue
        try:
            amt = float(row[i_cost] or 0)
        except (TypeError, ValueError):
            continue
        qty = None
        if i_qty is not None:
            try:
                qty = float(row[i_qty])
            except (TypeError, ValueError):
                qty = None
        region = str(row[i_loc]).strip() if i_loc is not None and row[i_loc] else ""
        sku = str(row[i_meter]).strip() if i_meter is not None and row[i_meter] else "Azure Monitor"
        key = (region, sku, d)
        cur = merged.setdefault(key, {"billed_account": subscription_id, "region": region, "sku": sku, "usage_date": d,
                                      "amount_usd": 0.0, "quantity": None, "unit": None})
        cur["amount_usd"] += amt
        if qty is not None:
            cur["quantity"] = (cur["quantity"] or 0.0) + qty
    return list(merged.values())


def _azure_body(cost_column, start, end):
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": f"{start.isoformat()}T00:00:00Z", "to": f"{end.isoformat()}T23:59:59Z"},
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": cost_column, "function": "Sum"},
                            "totalQuantity": {"name": "UsageQuantity", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "Meter"}, {"type": "Dimension", "name": "ResourceLocation"}],
            "filter": {"dimensions": {"name": "MeterCategory", "operator": "In", "values": ["Azure Monitor"]}},
        },
    }


def fetch_azure(account, secret, today=None, days=BACKFILL_DAYS, http=None, token=None, sleep=time.sleep):
    """One subscription's Azure Monitor charges. Returns (rows, requests, check_cost_usd). `http` and `token` are
    injectable for tests; in production the token comes from the service principal CloudOps already stores."""
    today = today or datetime.now(timezone.utc).date()
    sub = account.get("subscription_id") or account.get("account_id")
    if not sub:
        raise CostFetchError(STATUS_NOT_CONFIGURED, "the account has no subscription id")
    if token is None:
        from azure.identity import ClientSecretCredential
        token = ClientSecretCredential(tenant_id=account["tenant_id"], client_id=account["client_id"],
                                       client_secret=secret).get_token("https://management.azure.com/.default").token
    if http is None:
        import requests as http
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = (f"https://management.azure.com/subscriptions/{sub}/providers/Microsoft.CostManagement/query"
           f"?api-version={AZURE_API_VERSION}")
    start = today - timedelta(days=days)
    requests_made = 0
    last_400 = None
    for cost_column in ("CostUSD", "PreTaxCostUSD", "PreTaxCost"):          # not every offer type has every column
        body = _azure_body(cost_column, start, today)
        next_url, pages, rows_payload = url, 0, []
        while next_url and pages < AZURE_MAX_PAGES:
            resp = None
            for attempt in range(4):                                           # 429: honour Retry-After, a few times
                resp = http.post(next_url, headers=headers, json=body, timeout=60)
                requests_made += 1
                if resp.status_code != 429:
                    break
                try:
                    wait = min(30.0, float(resp.headers.get("Retry-After", 5)))
                except (TypeError, ValueError):
                    wait = 5.0
                sleep(wait)
            if resp.status_code in (401, 403):
                raise CostFetchError(STATUS_PERMISSION, "needs the role 'Cost Management Reader' on the subscription")
            if resp.status_code == 400:
                last_400 = (resp.text or "")[:200]
                rows_payload = None
                break
            if resp.status_code != 200:
                raise CostFetchError(STATUS_ERROR, f"Cost Management returned HTTP {resp.status_code}: {(resp.text or '')[:160]}")
            data = resp.json()
            rows_payload.append(data)
            next_url = (data.get("properties") or {}).get("nextLink")
            pages += 1
        if rows_payload is None:
            continue                                                            # this column name is not valid here: try the next
        out = []
        for data in rows_payload:
            out.extend(parse_azure_query(data, sub))
        return out, requests_made, 0.0
    raise CostFetchError(STATUS_ERROR, f"Cost Management rejected every cost column: {last_400}")


# ── GCP ──────────────────────────────────────────────────────────────────────
_TABLE_RE = re.compile(r"^[A-Za-z0-9_\-:.]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
BQ_TIB = float(2 ** 40)


def gcp_export_table():
    t = os.getenv("GCP_BILLING_EXPORT_TABLE", "").strip().strip("`")
    return t if _TABLE_RE.match(t) else ""


def gcp_query(table):
    """Standard-usage-cost export -> one row per day, region and SKU, in USD, credits (free tier included) netted off.
    The table name cannot be a query parameter, so it is validated against a strict pattern before it is interpolated."""
    if not _TABLE_RE.match(table or ""):
        raise CostFetchError(STATUS_NOT_CONFIGURED, "GCP_BILLING_EXPORT_TABLE must look like project.dataset.table")
    return f"""
SELECT DATE(usage_start_time) AS usage_date,
       IFNULL(location.region, IFNULL(location.location, 'global')) AS region,
       sku.description AS sku,
       SUM((cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0))
           / IF(IFNULL(currency_conversion_rate, 0) = 0, 1, currency_conversion_rate)) AS amount_usd,
       SUM(usage.amount_in_pricing_units) AS quantity,
       ANY_VALUE(usage.pricing_unit) AS unit
FROM `{table}`
WHERE service.description = 'Cloud Monitoring'
  AND project.id = @project
  AND usage_start_time >= TIMESTAMP(@start)
  AND usage_start_time < TIMESTAMP(@end)
GROUP BY usage_date, region, sku
"""


def map_gcp_rows(rows, project_id):
    out = []
    for r in rows:
        d = r["usage_date"]
        if isinstance(d, datetime):
            d = d.date()
        if not isinstance(d, date):
            d = _azure_date(d)
        if d is None:
            continue
        q = r.get("quantity")
        out.append({"billed_account": project_id, "region": str(r.get("region") or "global"), "sku": str(r.get("sku") or ""),
                    "usage_date": d, "amount_usd": float(r.get("amount_usd") or 0),
                    "quantity": float(q) if q is not None else None, "unit": (str(r["unit"])[:40] if r.get("unit") else None)})
    return out


def fetch_gcp(account, sa_key_json, today=None, days=BACKFILL_DAYS, client=None):
    """One project's Cloud Monitoring charges from the BigQuery billing export. Returns (rows, requests, check_cost_usd).
    `client` is injectable for tests (needs .run(sql, params) -> (rows, bytes_billed))."""
    table = gcp_export_table()
    if not table:
        raise CostFetchError(STATUS_NOT_CONFIGURED,
                             "set GCP_BILLING_EXPORT_TABLE=project.dataset.table (Billing > Billing export to BigQuery must be on)")
    project = account.get("project_id") or account.get("account_id")
    if not project:
        raise CostFetchError(STATUS_NOT_CONFIGURED, "the account has no project id")
    today = today or datetime.now(timezone.utc).date()
    params = {"project": project, "start": (today - timedelta(days=days)).isoformat(),
              "end": (today + timedelta(days=1)).isoformat()}
    sql = gcp_query(table)
    if client is None:
        client = _BigQueryRunner(account, sa_key_json)
    rows, bytes_billed = client.run(sql, params)
    price = float(os.getenv("BQ_PRICE_PER_TIB", "6.25"))
    return map_gcp_rows(rows, project), 1, round((bytes_billed or 0) / BQ_TIB * price, 6)


class _BigQueryRunner:
    """Thin wrapper so the query can be faked in tests. Scope is 'bigquery' (running a query job needs more than the
    read-only cloud-platform scope the discovery code uses); the service account's IAM roles still limit it to reading."""

    def __init__(self, account, sa_key_json):
        import json
        from google.cloud import bigquery
        from google.oauth2 import service_account
        info = json.loads(sa_key_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/bigquery"])
        self._bq = bigquery
        job_project = os.getenv("GCP_BILLING_JOB_PROJECT", "").strip() or account.get("project_id") or info.get("project_id")
        self._client = bigquery.Client(project=job_project, credentials=creds)

    def run(self, sql, params):
        cfg = self._bq.QueryJobConfig(query_parameters=[self._bq.ScalarQueryParameter(k, "STRING", v) for k, v in params.items()])
        job = self._client.query(sql, job_config=cfg)
        rows = [dict(r.items()) for r in job.result()]
        return rows, job.total_bytes_billed


def classify_error(exc):
    """Any exception -> (status, short message). Never raises."""
    if isinstance(exc, CostFetchError):
        return exc.status, str(exc)[:400]
    name = type(exc).__name__
    text = f"{name}: {exc}"[:400]
    if name in ("Forbidden", "PermissionDenied", "Unauthorized"):
        return STATUS_PERMISSION, text
    if name in ("NotFound",):
        return STATUS_NOT_CONFIGURED, f"billing export table or dataset not found ({exc})"[:400]
    return STATUS_ERROR, text


# ── persistence and scheduling ───────────────────────────────────────────────
def _db():
    from app.db import get_connection
    return get_connection()


def _accounts(provider):
    conn = _db()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT id, account_name, account_id, tenant_id, subscription_id, client_id, project_id, default_region "
                    "FROM aws_accounts WHERE status = 'active' AND provider = %s", (provider,))
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def billed_account_of(provider, account):
    if provider == "azure":
        return account.get("subscription_id") or account.get("account_id")
    return account.get("project_id") or account.get("account_id")


def _last_attempt(provider, account_pk):
    conn = _db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT UNIX_TIMESTAMP(MAX(fetched_at)) FROM cloud_cost_fetch_log WHERE provider = %s AND aws_account_id = %s",
                    (provider, account_pk))
        r = cur.fetchone()
        return float(r[0]) if r and r[0] is not None else None
    except Exception:
        return None
    finally:
        cur.close()
        conn.close()


def _backfilled(provider, account_pk):
    conn = cur = None
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT MAX(window_days) FROM cloud_cost_fetch_log WHERE provider = %s AND aws_account_id = %s AND status = 'ok'",
                    (provider, account_pk))
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


def _save(provider, account, status, requests_made, rows, error, check_cost, window_days):
    conn = _db()
    cur = conn.cursor()
    try:
        for r in rows:
            cur.execute(
                "INSERT INTO cloud_cost_actuals (provider, billed_account, region, sku, usage_date, amount_usd, quantity, unit, fetched_via) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE amount_usd=VALUES(amount_usd), quantity=VALUES(quantity), "
                "unit=VALUES(unit), fetched_via=VALUES(fetched_via), fetched_at=CURRENT_TIMESTAMP",
                (provider, str(r["billed_account"])[:100], r["region"][:60], r["sku"][:160], r["usage_date"],
                 round(r["amount_usd"], 6), r.get("quantity"), r.get("unit"), account["id"]))
        cur.execute("INSERT INTO cloud_cost_fetch_log (provider, aws_account_id, account_name, status, requests, rows_written, "
                    "check_cost_usd, error, window_days) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (provider, account["id"], account.get("account_name"), status, requests_made, len(rows), check_cost,
                     (error or None) and error[:500], window_days if status == STATUS_OK else None))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def refresh_account(provider, account):
    """Fetch and store one Azure subscription / GCP project. Never raises; returns {account, provider, status, ...}."""
    status, requests_made, rows, error, check_cost = STATUS_OK, 0, [], None, 0.0
    window = REFRESH_DAYS
    try:
        from app.credentials import load_credential
        secret = load_credential(account["id"])
        if not secret:
            raise CostFetchError(STATUS_NOT_CONFIGURED, "no stored credential for this account")
        window = REFRESH_DAYS if _backfilled(provider, account["id"]) else BACKFILL_DAYS
        fetch = fetch_azure if provider == "azure" else fetch_gcp
        rows, requests_made, check_cost = fetch(account, secret, days=window)
    except Exception as e:
        status, error = classify_error(e)
        logger.warning(f"[cloud-cost] {provider} {account.get('account_name')}: {status}: {error}")
    try:
        _save(provider, account, status, requests_made, rows, error, check_cost, window)
    except Exception:
        logger.exception("[cloud-cost] could not store result")
    return {"provider": provider, "account": account.get("account_name"), "status": status, "requests": requests_made,
            "rows": len(rows), "error": error}


def refresh_due(force=False, now=None):
    """Refresh every active Azure/GCP account whose last attempt is older than REFRESH_HOURS (force: cooldown only)."""
    if not enabled():
        return []
    if not _refresh_lock.acquire(blocking=False):
        return []
    try:
        now = time.time() if now is None else now
        min_age = (MANUAL_COOLDOWN_MINUTES * 60) if force else (REFRESH_HOURS * 3600)
        results = []
        for provider in PROVIDERS:
            for acct in _accounts(provider):
                last = _last_attempt(provider, acct["id"])
                if last is not None and now - last < min_age:
                    continue
                results.append(refresh_account(provider, acct))
        return results
    except Exception:
        logger.exception("[cloud-cost] refresh failed")
        return []
    finally:
        _refresh_lock.release()


def maybe_refresh_async():
    """Scheduler hook: cheap when nothing is due, never blocks the polling loop."""
    global _last_check
    if not enabled() or _refresh_lock.locked() or time.time() - _last_check < _CHECK_EVERY_SECONDS:
        return
    _last_check = time.time()
    threading.Thread(target=refresh_due, name="cloud-cost-refresh", daemon=True).start()
