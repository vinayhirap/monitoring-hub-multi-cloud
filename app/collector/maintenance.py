# app/collector/maintenance.py
"""
Maintenance windows with topology-aware silencing (2026-09-14). See
db/migrations/036_maintenance_windows.sql's module docstring for the
full design -- this file is the background reconciliation job that
keeps alerts.silenced in sync with which maintenance windows are
CURRENTLY active (not just scheduled/past).

Runs in scheduler.py's "critical" tier (2-min cadence, alongside
synthetic checks) -- silencing needs to activate/deactivate promptly
at a window's exact start/end time, not lag 15-60 minutes behind like
the slower AIOps background jobs. Cheap either way: this only ever
queries windows where starts_at <= NOW() <= ends_at, which is normally
a tiny number of rows.
"""
import logging

from app.db import get_connection

logger = logging.getLogger(__name__)

# Safety cap on recursive downstream-dependency depth -- guards against
# a cyclic resource_relationships graph (shouldn't exist, since the
# graph models real infra dependencies, but this is cheap insurance
# against ever hanging on a WITH RECURSIVE that never terminates).
MAX_CASCADE_DEPTH = 10


def _affected_resource_ids(cursor, root_resource_id: str, silence_downstream: bool,
                           aws_account_id: int = None) -> set:
    """Returns the full set of resource_ids a maintenance window on
    root_resource_id should silence: just the root if
    silence_downstream is False, or the root PLUS every resource that
    (transitively) depends on it via resource_relationships if True.
    See migration 036's docstring for the direction convention
    (source_resource_id depends on target_resource_id -- same
    convention app/collector/rca.py's in_degree already relies on).

    AUDIT(b06):
      * The walk is confined to the window's own account. resource_id is
        only unique per account, so the unscoped walk followed OTHER
        tenants' edges through a colliding id and pulled their resource
        ids into this window's (account, resource) silence set.
      * The anchor row now comes from resource_relationships itself, not a
        bare `SELECT %s` literal. MySQL types a recursive CTE's columns
        from the anchor only, so the column was sized to the ROOT id's
        length and any longer dependent id raised "Data too long" in
        strict mode -- aborting the whole sync (and alert_evaluator's
        born-silenced lookup) every cycle."""
    if not silence_downstream:
        return {root_resource_id}

    if aws_account_id is None:
        anchor_where, step_where = "rr0.target_resource_id = %s", "d.depth < %s"
        params = (root_resource_id, MAX_CASCADE_DEPTH)
    else:
        anchor_where = "rr0.target_resource_id = %s AND rr0.aws_account_id = %s"
        step_where = "rr.aws_account_id = %s AND d.depth < %s"
        params = (root_resource_id, aws_account_id, aws_account_id, MAX_CASCADE_DEPTH)
    cursor.execute(f"""
        WITH RECURSIVE downstream (resource_id, depth) AS (
            SELECT DISTINCT rr0.target_resource_id, 0
            FROM resource_relationships rr0
            WHERE {anchor_where}
            UNION ALL
            SELECT rr.source_resource_id, d.depth + 1
            FROM resource_relationships rr
            JOIN downstream d ON rr.target_resource_id = d.resource_id
            WHERE {step_where}
        )
        SELECT DISTINCT resource_id FROM downstream
    """, params)
    return {root_resource_id} | {row["resource_id"] for row in cursor.fetchall()}


def active_silenced_map(cursor) -> dict:
    """
    {(aws_account_id, resource_id): reason} for every resource covered by a
    maintenance window that is active RIGHT NOW. Used by alert_evaluator.py
    so an alert that BREACHES during a window is born silenced (no toast, no
    page, not counted) instead of being created loud and only silenced up to
    two minutes later by sync_maintenance_silencing().

    Account-scoped: a window on account A must never silence a same-named
    resource_id in account B (resource ids are only unique per account --
    migrations 045-048).
    """
    cursor.execute("""
        SELECT id, aws_account_id, resource_id, reason, silence_downstream
        FROM maintenance_windows
        WHERE starts_at <= UTC_TIMESTAMP() AND ends_at >= UTC_TIMESTAMP()
    """)
    # AUDIT(b06): UTC_TIMESTAMP(), not session-zone NOW() -- window times
    # are stored as UTC by app/api/maintenance.py. Reason is truncated to
    # alerts.silenced_reason's VARCHAR(500): "Maintenance window: " + a
    # 500-char reason otherwise raised "Data too long" and aborted the sync.
    out = {}
    for window in cursor.fetchall():
        affected = _affected_resource_ids(cursor, window["resource_id"], bool(window["silence_downstream"]),
                                          aws_account_id=window["aws_account_id"])
        for rid in affected:
            out.setdefault((window["aws_account_id"], rid),
                           f"Maintenance window: {window['reason']}"[:500])
    return out


def sync_maintenance_silencing() -> dict:
    """
    Reconciles alerts.silenced against every CURRENTLY active maintenance
    window. Returns {"silenced": n, "unsilenced": n}.

    2026-09-20: now ACCOUNT-scoped. It used to match on resource_id alone,
    so a window on one account could silence (and later un-silence) a
    same-named resource's alerts in a DIFFERENT account.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    silenced_count = 0
    unsilenced_count = 0
    try:
        covered = active_silenced_map(cursor)

        # 1. silence open, not-yet-silenced alerts on covered resources
        by_reason = {}
        for (acct, rid), reason in covered.items():
            by_reason.setdefault((acct, reason), []).append(rid)
        for (acct, reason), rids in by_reason.items():
            placeholders = ", ".join(["%s"] * len(rids))
            cursor.execute(f"""
                UPDATE alerts
                SET silenced = 1, silenced_reason = %s
                WHERE aws_account_id = %s AND resource_id IN ({placeholders})
                  AND status IN ('active', 'acknowledged') AND silenced = 0
            """, (reason, acct, *rids))
            silenced_count += cursor.rowcount

        # 2. un-silence anything no longer covered by an active window
        cursor.execute("""
            SELECT id, aws_account_id, resource_id FROM alerts
            WHERE silenced = 1 AND status IN ('active', 'acknowledged')
        """)
        stale_ids = [row["id"] for row in cursor.fetchall()
                     if (row["aws_account_id"], row["resource_id"]) not in covered]
        for i in range(0, len(stale_ids), 500):
            chunk = stale_ids[i:i + 500]
            placeholders = ", ".join(["%s"] * len(chunk))
            cursor.execute(
                f"UPDATE alerts SET silenced = 0, silenced_reason = NULL WHERE id IN ({placeholders})",
                tuple(chunk),
            )
            unsilenced_count += cursor.rowcount

        conn.commit()
        if silenced_count or unsilenced_count:
            logger.info(f"[maintenance] silenced {silenced_count}, un-silenced {unsilenced_count} alert(s) "
                        f"across {len(set(k[0] for k in covered))} account(s)")
        return {"silenced": silenced_count, "unsilenced": unsilenced_count}
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
