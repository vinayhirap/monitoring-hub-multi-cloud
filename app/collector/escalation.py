# app/collector/escalation.py
"""
Escalation policy evaluation — roadmap phase 9 (2026-09-13).

Runs each standard-tier cycle (wired into app/collector/scheduler.py,
right after evaluate_alerts() so it always sees this cycle's freshest
alert state). For every active, unacknowledged, not-yet-escalated alert
older than its matching policy's SLA, reassigns it to the policy's
target org_group and records that it happened.

See db/migrations/023_escalation_policies.sql's docstring for the
current limitation: _notify_escalation() below is a stub (op_event +
audit_log only) until SMTP is wired (item #3, deferred). Escalating
without a notification channel still has real value today — it changes
what the Alerts UI shows ("escalated to L2-platform 12 min ago") — but
nobody currently gets pinged about it.
"""
import logging
from app.db import get_connection

logger = logging.getLogger(__name__)


def _notify_escalation(alert_id, group_id, group_name, severity, metric_name, resource_id):
    """
    STUB. Once SMTP is wired (item #3), this is where to send "Alert
    #{alert_id} escalated to {group_name}" to that group's members
    (join org_groups -> user_group_memberships -> users -> email).
    Today it only produces a structured op_event/audit_log entry, so the
    escalation is at least findable and auditable even with no outbound
    notification yet.
    """
    from app.collector.op_log import log_event
    log_event(
        "alert_escalated",
        f"Alert {alert_id} ({severity} {metric_name} on {resource_id}) escalated to group '{group_name}' "
        f"— NOTE: no notification sent, SMTP not yet wired (see escalation.py docstring)",
        severity="WARNING",
        resource_id=resource_id,
    )


def evaluate_escalations() -> int:
    """Returns the number of alerts escalated this cycle."""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Account-specific policy takes priority over a global (NULL
        # aws_account_id) fallback for the same severity — ORDER BY puts
        # the account-specific match first, LIMIT 1 per alert picks it.
        cursor.execute("""
            SELECT
                a.id AS alert_id, a.resource_id, a.metric_name, a.severity,
                a.triggered_at, r.aws_account_id,
                ep.id AS policy_id, ep.ack_sla_minutes, ep.escalate_to_group_id,
                g.name AS group_name
            FROM alerts a
            JOIN resources r ON r.resource_id = a.resource_id
            JOIN escalation_policies ep
                 ON ep.severity = a.severity
                AND ep.enabled = 1
                AND (ep.aws_account_id = r.aws_account_id OR ep.aws_account_id IS NULL)
            JOIN org_groups g ON g.id = ep.escalate_to_group_id
            WHERE a.status = 'active'
              AND a.acked = 0
              AND a.escalated_at IS NULL
              AND a.triggered_at <= DATE_SUB(NOW(), INTERVAL ep.ack_sla_minutes MINUTE)
            ORDER BY a.id, (ep.aws_account_id IS NULL) ASC
        """)
        rows = cursor.fetchall()

        # One escalation per alert even though the query can return two
        # policy matches (account-specific + global) for the same alert —
        # keep the first (account-specific, thanks to the ORDER BY above).
        seen_alert_ids = set()
        escalated = 0
        for row in rows:
            if row["alert_id"] in seen_alert_ids:
                continue
            seen_alert_ids.add(row["alert_id"])

            cursor.execute("""
                UPDATE alerts
                SET escalated_at = NOW(), escalated_to_group_id = %s
                WHERE id = %s AND escalated_at IS NULL
            """, (row["escalate_to_group_id"], row["alert_id"]))
            if cursor.rowcount:
                escalated += 1
                _notify_escalation(
                    row["alert_id"], row["escalate_to_group_id"], row["group_name"],
                    row["severity"], row["metric_name"], row["resource_id"],
                )
        conn.commit()
        return escalated
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
