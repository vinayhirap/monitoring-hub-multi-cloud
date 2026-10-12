# app/collector/env_sync.py
"""
Persists app/environment_classifier.resolve_environment() onto resources
(env_resolved / env_source / env_reason, migration 083).

Used by:
  * the discovery loop (scheduler.py) -- right after each discovery run, so a
    resource whose tags or name changed is re-classified, and a brand-new
    resource lands in the Needs-classification queue without waiting for an
    alert;
  * the classification API -- immediately after a manual override or an
    account-default change;
  * apply_environment_backfill.py -- the dry-run-first backfill.

Idempotent: a row is written only when its (env, source, reason) differ from
what is already stored, so re-running is a no-op and cheap.

INHERITANCE. A resource with no environment evidence of its own follows the resource it
is attached to (resource_relationships 'attached_to': EC2 -> its EBS volumes). Two
passes, one hop: every resource is first resolved on its own evidence, then children are
resolved against their parents' pass-1 result, so a value is never passed down a chain.
Only 'attached_to' is followed. 'routes_to' / 'invokes' / 'origin' are "calls" edges, not
"belongs to", and a load balancer fronting targets says nothing about their environment.
Classifying a parent re-evaluates its children in the same call.

dry_run=True computes everything and writes NOTHING.

The resolution itself is pure and lives in app/environment_classifier.py;
this module only loads inputs, compares and writes.
"""
import logging

from app.db import get_connection
from app import environment_classifier as ec

logger = logging.getLogger(__name__)

# Same freshness rule the metric collector uses for "still exists".
STALE_RESOURCE_HOURS = 48
# ENIs publish no metrics and are never alerted on; keep them out of the queue.
NON_ALERTABLE_TYPES = ("eni",)
_CHUNK = 500
INHERIT_EDGE_TYPES = ("attached_to",)

_BASE_SELECT = """
    SELECT r.id, r.aws_account_id, r.resource_id, r.resource_type, r.name, r.tags,
           r.env_override, r.env_resolved, r.env_source, r.env_reason,
           aa.default_env
    FROM resources r
    JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
    WHERE (r.instance_state IS NULL OR r.instance_state != 'terminated')
      AND (r.last_seen_at IS NULL OR r.last_seen_at >= DATE_SUB(NOW(), INTERVAL %s HOUR))
      AND r.resource_type NOT IN ({types})
"""


def resolve_row(row, related=None):
    """One resources row (dict) -> classifier Resolution."""
    return ec.resolve_environment(
        override=row.get("env_override"),
        tags=row.get("tags"),
        name=row.get("name"),
        resource_id=row.get("resource_id"),
        account_default=row.get("default_env"),
        related=related,
    )


def _load(cursor, account_ids=None):
    sql = _BASE_SELECT.format(types=", ".join(["%s"] * len(NON_ALERTABLE_TYPES)))
    params = [STALE_RESOURCE_HOURS, *NON_ALERTABLE_TYPES]
    if account_ids is not None:
        if not account_ids:
            return []
        sql += " AND r.aws_account_id IN (" + ", ".join(["%s"] * len(account_ids)) + ")"
        params.extend(account_ids)
    cursor.execute(sql, params)
    return cursor.fetchall()


def _load_edges(cursor, account_ids=None):
    """-> [(aws_account_id, parent_resource_id, child_resource_id)]"""
    sql = ("SELECT aws_account_id, source_resource_id, target_resource_id FROM resource_relationships "
           "WHERE relationship_type IN (" + ", ".join(["%s"] * len(INHERIT_EDGE_TYPES)) + ")")
    params = list(INHERIT_EDGE_TYPES)
    if account_ids is not None:
        if not account_ids:
            return []
        sql += " AND aws_account_id IN (" + ", ".join(["%s"] * len(account_ids)) + ")"
        params.extend(account_ids)
    cursor.execute(sql, params)
    return [(r["aws_account_id"], r["source_resource_id"], r["target_resource_id"])
            for r in cursor.fetchall()]


def classify_rows(rows, edges):
    """Pure two-pass classification. -> {row id: Resolution}"""
    by_key = {(r["aws_account_id"], r["resource_id"]): r for r in rows}
    base = {r["id"]: resolve_row(r) for r in rows}
    parents_of = {}
    for acct, parent, child in edges:
        p, c = by_key.get((acct, parent)), by_key.get((acct, child))
        if p is not None and c is not None and p["id"] != c["id"]:
            parents_of.setdefault(c["id"], []).append(p)
    out = {}
    for r in rows:
        parents = parents_of.get(r["id"])
        if not parents:
            out[r["id"]] = base[r["id"]]
            continue
        related = [(base[p["id"]], f"{p['resource_type']} {p.get('name') or p['resource_id']}")
                   for p in parents]
        out[r["id"]] = resolve_row(r, related=related)
    return out, parents_of


def _tally(counter, key):
    counter[key] = counter.get(key, 0) + 1


def sync_resource_environments(account_id=None, resource_pks=None, dry_run=False):
    """Classify and persist. Returns a report dict:

        total            resources considered
        changed          rows whose stored classification differs from the new one
        applied          rows actually written (0 when dry_run)
        by_source        {source: count}   NEW classification, every considered row
        by_env           {env: count}
        changed_by_source / changed_by_env   the same, restricted to changed rows
        newly_unknown    rows that are now unknown and were not before

    resource_pks limits what is WRITTEN to those resources plus the resources attached to
    them (their children); the whole of each touched account is still loaded so a child can
    see its parent.
    """
    report = {
        "dry_run": bool(dry_run), "total": 0, "changed": 0, "applied": 0,
        "by_source": {}, "by_env": {}, "changed_by_source": {}, "changed_by_env": {},
        "newly_unknown": 0,
    }
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        try:
            account_ids = None
            if account_id is not None:
                account_ids = [account_id]
            elif resource_pks:
                cursor.execute("SELECT DISTINCT aws_account_id FROM resources WHERE id IN ("
                               + ", ".join(["%s"] * len(resource_pks)) + ")", list(resource_pks))
                account_ids = [r["aws_account_id"] for r in cursor.fetchall()]
            rows = _load(cursor, account_ids)
            edges = _load_edges(cursor, account_ids)
        finally:
            cursor.close()
        conn.commit()      # release the read snapshot before any write

        resolved, parents_of = classify_rows(rows, edges)

        scope = None
        if resource_pks:
            scope = set(resource_pks)
            for child_id, parents in parents_of.items():
                if any(p["id"] in scope for p in parents):
                    scope.add(child_id)

        updates = []
        for row in rows:
            if scope is not None and row["id"] not in scope:
                continue
            res = resolved[row["id"]]
            report["total"] += 1
            _tally(report["by_source"], res.source)
            _tally(report["by_env"], res.env)
            same = (row.get("env_resolved") == res.env
                    and row.get("env_source") == res.source
                    and (row.get("env_reason") or "") == res.reason)
            if same:
                continue
            report["changed"] += 1
            _tally(report["changed_by_source"], res.source)
            _tally(report["changed_by_env"], res.env)
            if res.env == ec.UNKNOWN and row.get("env_resolved") != ec.UNKNOWN:
                report["newly_unknown"] += 1
            updates.append((res.env, res.source, res.reason, row["id"]))

        if dry_run or not updates:
            return report

        wcur = conn.cursor()
        try:
            for i in range(0, len(updates), _CHUNK):
                chunk = updates[i:i + _CHUNK]
                wcur.executemany(
                    "UPDATE resources SET env_resolved = %s, env_source = %s, env_reason = %s "
                    "WHERE id = %s", chunk)
                conn.commit()
                report["applied"] += len(chunk)
        except Exception:
            conn.rollback()
            raise
        finally:
            wcur.close()
        return report
    finally:
        conn.close()


def sync_after_discovery():
    """Discovery-loop hook. Never raises: a classification problem must not
    disturb discovery. Skipped silently if migration 083 has not been applied."""
    try:
        rep = sync_resource_environments()
        if rep["applied"]:
            logger.info(f"[env-sync] classified {rep['applied']} of {rep['total']} resources "
                        f"(by source: {rep['by_source']})")
    except Exception as e:
        if getattr(e, "errno", None) == 1054:       # unknown column: 083 not applied yet
            logger.debug("[env-sync] migration 083 not applied yet -- skipping")
        else:
            logger.warning(f"[env-sync] failed (non-fatal): {e}")
