# app/collector/escalation.py
"""
Escalation policy evaluation — roadmap phase 9 (2026-09-13, wired to
real email 2026-09-14).

Runs each standard-tier cycle (wired into app/collector/scheduler.py,
right after evaluate_alerts() so it always sees this cycle's freshest
alert state). For every active, unacknowledged, not-yet-escalated alert
older than its matching policy's SLA, reassigns it to the policy's
target org_group and records that it happened.

NOTIFICATION: app/email/mailer.py already exists and is fully
functional (stdlib smtplib, no new dependency) -- it's just never been
used for escalations, only for password-reset and welcome emails. This
was pure gap-closing: _notify_escalation() now looks up the target
group's members (user_group_memberships -> users.email) and actually
emails them, instead of writing an op_event that says "no notification
sent, SMTP not yet wired." Escalating without a notification channel
still had some value (the Alerts UI shows "escalated to L2-platform
12 min ago"), but nobody was actually being pinged about it.

Degrades gracefully, same as every other mailer.py caller in this app:
if SMTP_HOST isn't configured, or a group has no members with an email
address on file, this logs exactly that (via op_event, so it's visible
in the existing Operational Events view) and moves on -- it never
raises, and never blocks the escalation itself (the alert is still
reassigned to the target group and shown as escalated in the UI even
if zero emails could be sent).
"""
import logging
from app.db import get_connection

logger = logging.getLogger(__name__)


def _notify_escalation(alert_id, group_id, group_name, severity, metric_name, resource_id):
    """
    Emails every member of the target org_group (user_group_memberships
    -> users.email) that this alert has been escalated to them. Uses
    its own short-lived connection (not the caller's open transaction/
    cursor in evaluate_escalations()) so a slow/failed email lookup or
    send can never roll back the escalation reassignment itself -- the
    UPDATE ... SET escalated_at already committed by the time this
    runs. Always logs a structured op_event summarizing what happened
    (sent to N recipients / SMTP not configured / group has no emailed
    members), so escalation delivery is auditable in Operational
    Events either way.
    """
    from app.collector.op_log import log_event
    from app.email import mailer

    if not mailer.is_configured():
        log_event(
            "alert_escalated",
            f"Alert {alert_id} ({severity} {metric_name} on {resource_id}) escalated to group "
            f"'{group_name}' -- SMTP not configured (SMTP_HOST unset), no email sent.",
            severity="WARNING",
            resource_id=resource_id,
        )
        return

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT DISTINCT u.email
            FROM user_group_memberships ugm
            JOIN users u ON u.id = ugm.user_id
            WHERE ugm.group_id = %s AND u.email IS NOT NULL AND u.email != ''
        """, (group_id,))
        recipients = [row["email"] for row in cursor.fetchall()]
    finally:
        cursor.close()
        conn.close()

    if not recipients:
        log_event(
            "alert_escalated",
            f"Alert {alert_id} ({severity} {metric_name} on {resource_id}) escalated to group "
            f"'{group_name}' -- no members of that group have an email address on file, no email sent.",
            severity="WARNING",
            resource_id=resource_id,
        )
        return

    app_url = mailer.get_public_app_url()
    subject = f"[CloudOps] {severity} alert escalated to {group_name}"
    body = (
        f"An alert has been escalated to your group, {group_name}, because it wasn't "
        f"acknowledged within its policy's SLA.\n\n"
        f"  Severity: {severity}\n"
        f"  Metric:   {metric_name}\n"
        f"  Resource: {resource_id}\n\n"
        f"View and acknowledge it here: {app_url}/alerts\n"
    )

    sent_count = 0
    for to_addr in recipients:
        if mailer.send_email(to_addr, subject, body):
            sent_count += 1

    log_event(
        "alert_escalated",
        f"Alert {alert_id} ({severity} {metric_name} on {resource_id}) escalated to group "
        f"'{group_name}' -- emailed {sent_count}/{len(recipients)} member(s).",
        severity="INFO" if sent_count == len(recipients) else "WARNING",
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
              -- AND a.silenced = 0: maintenance windows (2026-09-14,
              -- see app/collector/maintenance.py) mark an alert
              -- silenced=1 while it's covered by an active maintenance
              -- window -- the alert row still exists and still feeds
              -- correlate.py/health_score.py/rca.py, only the page/
              -- email this function sends is skipped for it.
              AND a.silenced = 0
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
