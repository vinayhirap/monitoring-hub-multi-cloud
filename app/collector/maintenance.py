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


def _affected_resource_ids(cursor, root_resource_id: str, silence_downstream: bool) -> set:
    """Returns the full set of resource_ids a maintenance window on
    root_resource_id should silence: just the root if
    silence_downstream is False, or the root PLUS every resource that
    (transitively) depends on it via resource_relationships if True.
    See migration 036's docstring for the direction convention
    (source_resource_id depends on target_resource_id -- same
    convention app/collector/rca.py's in_degree already relies on)."""
    if not silence_downstream:
        return {root_resource_id}

    cursor.execute("""
        WITH RECURSIVE downstream (resource_id, depth) AS (
            SELECT %s, 0
            UNION ALL
            SELECT rr.source_resource_id, d.depth + 1
            FROM resource_relationships rr
            JOIN downstream d ON rr.target_resource_id = d.resource_id
            WHERE d.depth < %s
        )
        SELECT DISTINCT resource_id FROM downstream
    """, (root_resource_id, MAX_CASCADE_DEPTH))
    return {row["resource_id"] for row in cursor.fetchall()}


def sync_maintenance_silencing() -> dict:
    """
    Reconciles alerts.silenced against every CURRENTLY active
    maintenance window. Returns {"silenced": n, "unsilenced": n}.

    Two passes:
      1. For each active window, compute its affected resource set
         (see _affected_resource_ids) and silence any active,
         not-yet-silenced alert on those resources, tagging
         silenced_reason with the window's own reason text.
      2. Un-silence any currently-silenced alert whose resource is NOT
         covered by ANY currently-active window -- this is what makes
         silencing automatically lift the moment a window ends (or is
         deleted), with no separate "end maintenance" action needed.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    silenced_count = 0
    try:
        cursor.execute("""
            SELECT id, resource_id, reason, silence_downstream
            FROM maintenance_windows
            WHERE starts_at <= NOW() AND ends_at >= NOW()
        """)
        active_windows = cursor.fetchall()

        all_covered_resource_ids = set()
        for window in active_windows:
            affected = _affected_resource_ids(cursor, window["resource_id"], bool(window["silence_downstream"]))
            all_covered_resource_ids |= affected
            if not affected:
                continue
            placeholders = ", ".join(["%s"] * len(affected))
            cursor.execute(f"""
                UPDATE alerts
                SET silenced = 1, silenced_reason = %s
                WHERE resource_id IN ({placeholders})
                  AND status = 'active' AND silenced = 0
            """, (f"Maintenance window: {window['reason']}", *affected))
            silenced_count += cursor.rowcount

        if all_covered_resource_ids:
            placeholders = ", ".join(["%s"] * len(all_covered_resource_ids))
            cursor.execute(f"""
                UPDATE alerts
                SET silenced = 0, silenced_reason = NULL
                WHERE silenced = 1 AND resource_id NOT IN ({placeholders})
            """, tuple(all_covered_resource_ids))
        else:
            # No active windows at all right now -- un-silence everything.
            cursor.execute("UPDATE alerts SET silenced = 0, silenced_reason = NULL WHERE silenced = 1")
        unsilenced_count = cursor.rowcount

        conn.commit()
        if silenced_count or unsilenced_count:
            logger.info(f"[maintenance] silenced {silenced_count}, un-silenced {unsilenced_count} alert(s) "
                        f"across {len(active_windows)} active window(s)")
        return {"silenced": silenced_count, "unsilenced": unsilenced_count}
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
