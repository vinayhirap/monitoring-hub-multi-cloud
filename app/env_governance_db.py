# app/env_governance_db.py
"""
Queries behind the environment governance features (workstream C). Everything here is READ-ONLY except
take_snapshot(), which upserts the daily compliance snapshot (migration 099). The API (app/api/environments.py)
does the permission and account-scope checks and passes the allowed account ids in; None means "all accounts".

  has_warning_column()        is migration 098 applied (cached once it is)
  preview_default()           what changing an account default would do; writes nothing
  attach_suggestions()        suggested environments for rows of the Needs-classification queue; applies nothing
  resource_history()          who changed a resource's environment, from audit_logs and the stored columns
  compliance_report/trend     tag state and decision source per account and resource type, and the daily trend
  take_snapshot()             the once-a-day upsert behind the trend
  tagging_gap_rows()          resources missing standard tags, for the CSV export

Imports of the sync module are lazy: this module must stay importable when the sync module is stubbed in tests.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta

from app import environment_classifier as ec
from app import environment_governance as eg
from app.db import get_connection

logger = logging.getLogger(__name__)

HISTORY_LOOKBACK_DAYS = 365
HISTORY_AUDIT_ROWS = 500
GAP_ROW_CAP = 50000
CLASSIFY_ACTION = "Environment classification changed"
DEFAULT_ACTION = "Account default environment changed"
RECENT_ONBOARD_DAYS = 30

_warn_cache = {"ok": False, "checked": 0.0}


def has_warning_column():
    """True once resources.env_warning exists. A positive answer is cached for the life of the process; a negative
    one is re-checked after 60 s, so applying migration 098 after a restart is picked up without another restart."""
    if _warn_cache["ok"]:
        return True
    if time.time() - _warn_cache["checked"] < 60:
        return False
    _warn_cache["checked"] = time.time()
    try:
        conn = get_connection()
        try:
            cur = conn.cursor(dictionary=True)
            try:
                cur.execute("SELECT COUNT(*) AS n FROM information_schema.columns WHERE table_schema = DATABASE() "
                            "AND table_name = 'resources' AND column_name = 'env_warning'")
                _warn_cache["ok"] = int(cur.fetchone()["n"]) > 0
            finally:
                cur.close()
        finally:
            conn.close()
    except Exception:
        return False
    return _warn_cache["ok"]


def _live_where():
    from app.collector.env_sync import NON_ALERTABLE_TYPES, STALE_RESOURCE_HOURS
    sql = ("(r.instance_state IS NULL OR r.instance_state != 'terminated') "
           "AND (r.last_seen_at IS NULL OR r.last_seen_at >= DATE_SUB(NOW(), INTERVAL %s HOUR)) "
           "AND r.resource_type NOT IN ({t})").format(t=", ".join(["%s"] * len(NON_ALERTABLE_TYPES)))
    return sql, [STALE_RESOURCE_HOURS, *NON_ALERTABLE_TYPES]


def _scope(account_ids, column="r.aws_account_id"):
    if account_ids is None:
        return "", []
    if not account_ids:
        return " AND 1 = 0", []
    ids = sorted(account_ids)
    return f" AND {column} IN (" + ", ".join(["%s"] * len(ids)) + ")", ids


# ---- 0. account default preview ------------------------------------------------------------
def preview_default(account_id, new_default):
    """-> preview dict, or None when the account does not exist. Dry run of the SAME code the PUT runs, with the
    new default substituted, so the counts are what the write will then report."""
    from app.collector.env_sync import sync_resource_environments
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT id, account_name, default_env FROM aws_accounts WHERE id = %s", (account_id,))
            acc = cur.fetchone()
        finally:
            cur.close()
    finally:
        conn.close()
    if not acc:
        return None
    report = sync_resource_environments(account_id=account_id, dry_run=True,
                                        account_default_override=(account_id, new_default), collect_changes=True)
    flag_on = ec.flag_enabled()
    alerts = []
    if flag_on and report.get("changes"):
        conn = get_connection()
        try:
            cur = conn.cursor(dictionary=True)
            try:
                cur.execute("SELECT resource_id, severity FROM alerts WHERE aws_account_id = %s AND resolved_at IS NULL",
                            (account_id,))
                alerts = cur.fetchall()
            finally:
                cur.close()
        finally:
            conn.close()
    priority_on = os.getenv("PRIORITY_ENABLED", "false").strip().lower() in ("true", "1", "yes")
    out = eg.default_change_summary(report, report.get("changes", []), acc["default_env"], new_default, alerts,
                                    flag_on=flag_on, priority_on=priority_on)
    out.update({"account_id": acc["id"], "account_name": acc["account_name"], "unchanged": acc["default_env"] == new_default})
    return out


# ---- 3. suggestions ------------------------------------------------------------------------
def _sibling_pool(account_ids):
    """Resources already decided by their OWN evidence, per account: {account_id: SuggestionIndex}."""
    if not account_ids:
        return {}
    live, live_p = _live_where()
    marks = ", ".join(["%s"] * len(account_ids))
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(f"""
                SELECT r.aws_account_id AS account_id, r.resource_id, r.name, r.tags, r.env_resolved AS env,
                       r.env_source AS source
                FROM resources r JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
                WHERE {live} AND r.aws_account_id IN ({marks})
                  AND r.env_resolved IN ('prod', 'uat', 'dev') AND r.env_source IN ('manual', 'tag', 'name')
            """, live_p + list(account_ids))
            rows = cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["account_id"], []).append(r)
    return {a: ec.SuggestionIndex(grouped.get(a, [])) for a in account_ids}


def attach_suggestions(items, tags_by_pk):
    """Add a `suggestion` key (dict or None) to every queue row. `tags_by_pk` is {resource pk: raw tags}: the
    caller selected them server-side and never returns them. Only rows still waiting for an environment get
    one; anything else is None. Nothing is written."""
    waiting = [r for r in items if (r.get("source") in (None, "unknown"))]
    pools = _sibling_pool(sorted({r["account_id"] for r in waiting}))
    for r in items:
        r["suggestion"] = None
    for r in waiting:
        idx = pools.get(r["account_id"])
        sug = idx.suggest({"resource_id": r["resource_id"], "name": r.get("name"),
                           "resource_type": r.get("resource_type"), "tags": tags_by_pk.get(r["id"])}) if idx else None
        if sug:
            r["suggestion"] = {"env": sug.env, "confidence": sug.confidence, "basis": sug.basis,
                               "reason": sug.reason, "evidence": sug.evidence}


# ---- 4. change history ---------------------------------------------------------------------
def _iso(v):
    return v.isoformat() + "Z" if isinstance(v, datetime) else v


def history_events(pk, account_id, audit_rows):
    """Pure: audit rows -> (events newest first, incomplete flag).

    A classify audit entry stores only a SAMPLE of the resource ids (the first 100); a resource in a larger batch
    may be missing from it. `incomplete` is True when an entry that touched this account was truncated and did not
    list this resource, so the page can say the history may not be complete."""
    events, incomplete = [], False
    for row in audit_rows:
        p = row.get("payload")
        if isinstance(p, (str, bytes)):
            try:
                p = json.loads(p)
            except Exception:
                continue
        if not isinstance(p, dict):
            continue
        if row.get("action") == CLASSIFY_ACTION:
            ids = p.get("resource_ids") or []
            if pk in ids:
                prev = (p.get("previous") or {}).get(str(pk))
                events.append({"at": _iso(row.get("created_at")), "actor": row.get("actor"), "kind": "classified",
                               "to_env": p.get("env"), "from_env": prev[0] if prev else None,
                               "from_source": prev[1] if prev else None,
                               "via": p.get("via"), "summary": p.get("detail")})
            elif p.get("resource_ids_truncated") and account_id in (p.get("account_ids") or []):
                incomplete = True
        elif row.get("action") == DEFAULT_ACTION and p.get("account_id") == account_id:
            events.append({"at": _iso(row.get("created_at")), "actor": row.get("actor"), "kind": "default_changed",
                           "from_env": p.get("before"), "to_env": p.get("after"), "summary": p.get("detail")})
    events.sort(key=lambda e: e["at"] or "", reverse=True)
    return events, incomplete


def resource_history(pk, account_ids):
    """-> dict or None (not found / outside scope). Scope is enforced here too, not only in the route."""
    scope_sql, scope_p = _scope(account_ids)
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            extra = ", r.env_warning" if has_warning_column() else ""
            cur.execute(f"""SELECT r.id, r.aws_account_id, r.resource_type, r.resource_id, r.name, r.env_resolved,
                                   r.env_source, r.env_reason, r.env_override, r.env_override_by, r.env_override_at{extra}
                            FROM resources r WHERE r.id = %s{scope_sql}""", [pk, *scope_p])
            res = cur.fetchone()
            if not res:
                return None
            cur.execute("""SELECT actor, action, payload, created_at FROM audit_logs
                           WHERE action IN (%s, %s) AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                           ORDER BY created_at DESC LIMIT %s""",
                        (CLASSIFY_ACTION, DEFAULT_ACTION, HISTORY_LOOKBACK_DAYS, HISTORY_AUDIT_ROWS))
            audit = cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()
    events, incomplete = history_events(pk, res["aws_account_id"], audit)
    return {
        "resource": {"id": res["id"], "resource_id": res["resource_id"], "name": res["name"],
                     "resource_type": res["resource_type"]},
        "current": {"env": res["env_resolved"], "source": res["env_source"], "reason": res["env_reason"],
                    "override": res["env_override"], "override_by": res["env_override_by"],
                    "override_at": _iso(res["env_override_at"]), "warning": res.get("env_warning")},
        "events": events, "incomplete": incomplete,
        "audit_lookback_days": HISTORY_LOOKBACK_DAYS,
    }


# ---- 2. compliance -------------------------------------------------------------------------
def _live_rows(account_ids):
    live, live_p = _live_where()
    scope_sql, scope_p = _scope(account_ids)
    warn = "r.env_warning" if has_warning_column() else "NULL"
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(f"""
                SELECT r.aws_account_id AS account_id, aa.account_name, aa.provider, r.resource_type,
                       r.tag_state, r.env_source, {warn} AS warning
                FROM resources r JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
                WHERE {live}{scope_sql}
            """, live_p + scope_p)
            return cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()


def compliance_report(account_ids, account_id=None):
    rows = _live_rows(account_ids)
    if account_id is not None:
        rows = [r for r in rows if r["account_id"] == account_id]
    return eg.tally_compliance(rows)


def compliance_trend(account_ids, days=30, account_id=None):
    """Daily totals across the allowed accounts (optionally one). Dates with no snapshot are simply absent."""
    days = max(1, min(int(days), 365))
    scope_sql, scope_p = _scope(account_ids, "s.aws_account_id")
    where = f"s.snapshot_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY){scope_sql}"
    params = [days, *scope_p]
    if account_id is not None:
        where += " AND s.aws_account_id = %s"; params.append(account_id)
    cols = ["total", "tag_ok", "tag_nonstandard", "tag_invalid", "tag_missing", "tag_not_checked", "tag_pending",
            "src_manual", "src_tag", "src_name", "src_related", "src_account", "src_unknown", "src_pending", "warnings"]
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT s.snapshot_date, " + ", ".join(f"SUM(s.{c}) AS {c}" for c in cols) +
                        f" FROM environment_compliance_snapshots s WHERE {where} "
                        "GROUP BY s.snapshot_date ORDER BY s.snapshot_date", params)
            rows = cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = {c: int(r[c] or 0) for c in cols}
        checkable = d["tag_ok"] + d["tag_nonstandard"] + d["tag_invalid"] + d["tag_missing"]
        d.update(date=str(r["snapshot_date"]), checkable=checkable, ok_of_checkable_pct=eg.pct(d["tag_ok"], checkable))
        out.append(d)
    return out


def take_snapshot():
    """Upsert today's (UTC-server-date) snapshot for every active account and resource type. Returns rows written.
    Raises on a database problem; snapshot_after_discovery() is the never-raising wrapper."""
    live, live_p = _live_where()
    warn = "SUM(r.env_warning IS NOT NULL)" if has_warning_column() else "0"
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(f"""
                SELECT r.aws_account_id, r.resource_type, COUNT(*) AS total,
                  SUM(r.tag_state = 'ok') AS tag_ok, SUM(r.tag_state = 'nonstandard') AS tag_nonstandard,
                  SUM(r.tag_state = 'invalid') AS tag_invalid, SUM(r.tag_state = 'missing') AS tag_missing,
                  SUM(r.tag_state = 'not_checked') AS tag_not_checked, SUM(r.tag_state IS NULL) AS tag_pending,
                  SUM(r.env_source = 'manual') AS src_manual, SUM(r.env_source = 'tag') AS src_tag,
                  SUM(r.env_source = 'name') AS src_name, SUM(r.env_source = 'related') AS src_related,
                  SUM(r.env_source = 'account') AS src_account, SUM(r.env_source = 'unknown') AS src_unknown,
                  SUM(r.env_source IS NULL) AS src_pending, {warn} AS warnings
                FROM resources r JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
                WHERE {live}
                GROUP BY r.aws_account_id, r.resource_type
            """, live_p)
            rows = cur.fetchall()
        finally:
            cur.close()
        cols = ["total", "tag_ok", "tag_nonstandard", "tag_invalid", "tag_missing", "tag_not_checked", "tag_pending",
                "src_manual", "src_tag", "src_name", "src_related", "src_account", "src_unknown", "src_pending", "warnings"]
        wcur = conn.cursor()
        try:
            for r in rows:
                vals = [int(r[c] or 0) for c in cols]
                wcur.execute(
                    "INSERT INTO environment_compliance_snapshots (snapshot_date, aws_account_id, resource_type, "
                    + ", ".join(cols) + ") VALUES (CURDATE(), %s, %s, " + ", ".join(["%s"] * len(cols)) + ") "
                    "ON DUPLICATE KEY UPDATE " + ", ".join(f"{c} = VALUES({c})" for c in cols),
                    [r["aws_account_id"], r["resource_type"], *vals])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            wcur.close()
        return len(rows)
    finally:
        conn.close()


def snapshot_enabled():
    """ENV_COMPLIANCE_SNAPSHOT, default ON: it only writes the report table, nothing that reaches alerting."""
    return os.getenv("ENV_COMPLIANCE_SNAPSHOT", "true").strip().lower() in ("true", "1", "yes")


def snapshot_after_discovery():
    """Discovery-loop hook. Never raises; a missing table (migration 099 not applied) is skipped quietly."""
    if not snapshot_enabled():
        return 0
    try:
        return take_snapshot()
    except Exception as e:
        if getattr(e, "errno", None) in (1146, 1054):
            logger.debug("[env-compliance] migration 099 not applied yet -- skipping the snapshot")
        else:
            logger.warning(f"[env-compliance] snapshot failed (non-fatal): {e}")
        return 0


# ---- 6. tagging gaps -----------------------------------------------------------------------
GAP_HEADER = ["account", "provider", "resource_type", "resource_id", "name", "region", "environment",
              "environment_decided_by", "tag_state", "missing_tags"]


def tagging_gap_rows(account_ids, account_id=None, resource_type=None, essential_only=False):
    """-> (rows, truncated). Resources whose OWN tags are known (tag_state is not 'not_checked') and that lack at
    least one standard tag. `essential_only` limits the check to the four tags CloudOps itself uses."""
    live, live_p = _live_where()
    scope_sql, scope_p = _scope(account_ids)
    where = f"{live}{scope_sql} AND r.tag_state IS NOT NULL AND r.tag_state != 'not_checked'"
    params = live_p + scope_p
    if account_id is not None:
        where += " AND r.aws_account_id = %s"; params.append(account_id)
    if resource_type:
        where += " AND r.resource_type = %s"; params.append(resource_type)
    keys = ec.ESSENTIAL_TAGS if essential_only else ec.MANDATORY_TAGS
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            # tag-sync result (NULL unless TAG_SYNC_ENABLED and migration 095): for the types the tag sync covers the
            # resource's own tags live there, not in r.tags, so the gap list must read them from there
            try:
                from app.collector.tag_sync import synced_select
                synced_sql = synced_select(cur)
            except Exception:
                synced_sql = "NULL AS synced_tags"
            cur.execute(f"""
                SELECT aa.account_name, aa.provider, r.resource_type, r.resource_id, r.name, r.region,
                       r.env_resolved, r.env_source, r.tag_state, r.tags, {synced_sql}
                FROM resources r JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
                WHERE {where}
                ORDER BY aa.account_name, r.resource_type, COALESCE(r.name, r.resource_id)
            """, params)
            out, truncated = [], False
            for r in cur:
                own = ec.effective_tags(r["resource_type"], r["tags"], ec.parse_synced(r.get("synced_tags")))
                miss = ec.missing_tags(own, keys)
                if not miss:
                    continue
                if len(out) >= GAP_ROW_CAP:
                    truncated = True
                    break
                out.append([r["account_name"], r["provider"] or "aws", r["resource_type"], r["resource_id"], r["name"],
                            r["region"], r["env_resolved"], r["env_source"], r["tag_state"], "; ".join(miss)])
            return out, truncated
        finally:
            cur.close()
    finally:
        conn.close()
