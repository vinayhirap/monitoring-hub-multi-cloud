# app/account_data.py
"""
Account removal safeguards (audit C7 / D5).

Removing a monitored account permanently deletes its alerts, incidents, resources, baselines, findings and so on
(that is deliberate: see the long comment in api/admin/accounts.py delete_account about stale data resurfacing when
the same cloud account is onboarded again). Two things were missing around that:

  count_account_data()  how much is about to be destroyed -> recorded in the audit entry, so "Account removed"
                        says what was lost instead of just the name.
  build_export()        a JSON snapshot (alerts, incidents, resources) the operator can download BEFORE confirming.
                        It contains no credentials: the aws_accounts row is reduced to name/ids/provider/region.

Pure functions over a DB cursor, so they are testable without the web stack.
"""
import datetime
import json

# Tables counted before removal. Names are constants (never user input), so f-string interpolation is safe.
DATA_TABLES = (
    "alerts", "resources", "incidents", "security_findings", "synthetic_checks",
    "slo_definitions", "maintenance_windows", "metric_baseline", "escalation_policies",
)

EXPORT_ALERT_LIMIT = 20000
EXPORT_INCIDENT_LIMIT = 5000
EXPORT_RESOURCE_LIMIT = 5000


def count_account_data(cursor, account_id: int) -> dict:
    """{table: row count} for the tables this removal will empty. A table that errors is skipped, never fatal."""
    counts = {}
    for table in DATA_TABLES:
        try:
            cursor.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE aws_account_id = %s", (account_id,))
            row = cursor.fetchone() or {}
            counts[table] = int(row.get("n") or 0)
        except Exception:
            continue
    return counts


def format_counts(counts: dict) -> str:
    parts = [f"{t}={n}" for t, n in counts.items() if n]
    return ", ".join(parts) if parts else "no stored data"


def _jsonable(value):
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, (set, tuple)):
        return list(value)
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _rows(cursor, sql, params, limit):
    cursor.execute(sql + f" LIMIT {int(limit) + 1}", params)
    rows = cursor.fetchall() or []
    truncated = len(rows) > limit
    rows = rows[:limit]
    return [{k: _jsonable(v) for k, v in r.items()} for r in rows], truncated


def build_export(cursor, account_id: int) -> dict:
    cursor.execute(
        "SELECT id, account_name, account_id, provider, default_region, status "
        "FROM aws_accounts WHERE id = %s", (account_id,))
    account = cursor.fetchone()
    if not account:
        return {}
    alerts, a_trunc = _rows(
        cursor,
        "SELECT id, resource_id, metric_name, severity, status, current_value, threshold, breach_value, "
        "breach_threshold, triggered_at, resolved_at, last_seen_at, acked_by, acked_at, resolution_reason, "
        "environment FROM alerts WHERE aws_account_id = %s ORDER BY id DESC",
        (account_id,), EXPORT_ALERT_LIMIT)
    incidents, i_trunc = _rows(
        cursor,
        "SELECT id, title, severity, status, primary_resource_id, probable_cause, started_at, resolved_at "
        "FROM incidents WHERE aws_account_id = %s ORDER BY id DESC",
        (account_id,), EXPORT_INCIDENT_LIMIT)
    resources, r_trunc = _rows(
        cursor,
        "SELECT resource_type, resource_id, name, region, last_seen_at "
        "FROM resources WHERE aws_account_id = %s ORDER BY resource_type, resource_id",
        (account_id,), EXPORT_RESOURCE_LIMIT)
    return {
        "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "note": "History snapshot taken before removal. Contains no credentials. Metric time-series are not included.",
        "account": {k: _jsonable(v) for k, v in account.items()},
        "truncated": {"alerts": a_trunc, "incidents": i_trunc, "resources": r_trunc},
        "alerts": alerts,
        "incidents": incidents,
        "resources": resources,
    }
