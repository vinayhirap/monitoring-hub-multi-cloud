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


def resolve_row(row):
    """One resources row (dict) -> classifier Resolution."""
    return ec.resolve_environment(
        override=row.get("env_override"),
        tags=row.get("tags"),
        name=row.get("name"),
        resource_id=row.get("resource_id"),
        account_default=row.get("default_env"),
    )


def _load(cursor, account_id=None, resource_pks=None):
    sql = _BASE_SELECT.format(types=", ".join(["%s"] * len(NON_ALERTABLE_TYPES)))
    params = [STALE_RESOURCE_HOURS, *NON_ALERTABLE_TYPES]
    if account_id is not None:
        sql += " AND r.aws_account_id = %s"
        params.append(account_id)
    if resource_pks:
        sql += " AND r.id IN (" + ", ".join(["%s"] * len(resource_pks)) + ")"
        params.extend(resource_pks)
    cursor.execute(sql, params)
    return cursor.fetchall()


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
            rows = _load(cursor, account_id, resource_pks)
        finally:
            cursor.close()
        conn.commit()      # release the read snapshot before any write

        updates = []
        for row in rows:
            res = resolve_row(row)
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
