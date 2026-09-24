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
from app import alert_rules as _alert_rules

logger = logging.getLogger(__name__)


def _notify_escalation(alert_id, group_id, group_name, severity, metric_name, resource_id,
                       aws_account_id=None):
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
        # Deactivated users are never paged (users.active, migration 052).
        cursor.execute("""
            SELECT DISTINCT u.id, u.role, u.email
            FROM user_group_memberships ugm
            JOIN users u ON u.id = ugm.user_id
            WHERE ugm.group_id = %s AND u.email IS NOT NULL AND u.email != ''
              AND COALESCE(u.active, 1) = 1
        """, (group_id,))
        members = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    # Tenant isolation: the email names the resource/metric/severity, so
    # only group members who may see this alert's account receive it --
    # group membership alone is not an account grant.
    recipients = []
    for m in members:
        if aws_account_id is None:
            recipients.append(m["email"]); continue
        try:
            from app.auth.authorization import get_accessible_account_ids
            accessible = get_accessible_account_ids({"id": m["id"], "role": m["role"]})
        except Exception as e:
            logger.warning(f"[escalation] scope lookup failed for user {m['id']}: {e} -- not emailing them")
            continue
        if accessible is None or aws_account_id in accessible:
            recipients.append(m["email"])
    recipients = list(dict.fromkeys(recipients))

    if not recipients:
        log_event(
            "alert_escalated",
            f"Alert {alert_id} ({severity} {metric_name} on {resource_id}) escalated to group "
            f"'{group_name}' -- no active member of that group with access to this account has an "
            f"email address on file, no email sent.",
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
        try:
            if mailer.send_email(to_addr, subject, body):
                sent_count += 1
        except Exception as e:
            logger.warning(f"[escalation] email to a member of '{group_name}' failed: {e}")

    log_event(
        "alert_escalated",
        f"Alert {alert_id} ({severity} {metric_name} on {resource_id}) escalated to group "
        f"'{group_name}' -- emailed {sent_count}/{len(recipients)} member(s).",
        severity="INFO" if sent_count == len(recipients) else "WARNING",
        resource_id=resource_id,
    )



def evaluate_escalations() -> int:
    """Returns the number of alerts escalated this cycle."""
    claimed_rows = []
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Account-specific policy takes priority over a global (NULL
        # aws_account_id) fallback for the same severity — ORDER BY puts
        # the account-specific match first, LIMIT 1 per alert picks it.
        cursor.execute("""
            SELECT
                a.id AS alert_id, a.resource_id, a.metric_name, a.severity,
                a.triggered_at, a.aws_account_id,
                ep.id AS policy_id, ep.ack_sla_minutes, ep.escalate_to_group_id,
                g.name AS group_name
            FROM alerts a
            JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
            JOIN aws_accounts acc ON acc.id = a.aws_account_id AND acc.status = 'active'
            JOIN escalation_policies ep
                 ON ep.severity = a.severity
                AND ep.enabled = 1
                AND (ep.aws_account_id = a.aws_account_id OR ep.aws_account_id IS NULL)
            JOIN org_groups g ON g.id = ep.escalate_to_group_id
            -- Page ONLY for an alert that is genuinely live (2026-09-20):
            -- the canonical FIRING state = active + fresh + not silenced
            -- (maintenance) + not muted, and not a hidden internal metric.
            -- Previously stale, muted and hidden alerts were paged too.
            WHERE """ + _alert_rules.firing_where() + """
              AND """ + _alert_rules.base_where() + """
              AND a.acked = 0
              AND a.escalated_at IS NULL
              AND a.triggered_at <= DATE_SUB(UTC_TIMESTAMP(), INTERVAL ep.ack_sla_minutes MINUTE)
            ORDER BY a.id, (ep.aws_account_id IS NULL) ASC
        """)
        rows = cursor.fetchall()

        # One escalation per alert even though the query can return two
        # policy matches (account-specific + global) for the same alert —
        # keep the first (account-specific, thanks to the ORDER BY above).
        seen_alert_ids = set()
        for row in rows:
            if row["alert_id"] in seen_alert_ids:
                continue
            seen_alert_ids.add(row["alert_id"])

            cursor.execute("""
                UPDATE alerts
                SET escalated_at = UTC_TIMESTAMP(), escalated_to_group_id = %s
                WHERE id = %s AND escalated_at IS NULL
            """, (row["escalate_to_group_id"], row["alert_id"]))
            claimed = cursor.rowcount
            # Commit BEFORE emailing (audit b08). Previously every email
            # went out inside one open transaction committed only after
            # the whole loop: a later failure rolled back escalated_at
            # for alerts whose emails were already sent, so the next
            # cycle re-escalated and re-emailed them, and row locks were
            # held across slow SMTP calls.
            conn.commit()
            if claimed:
                claimed_rows.append(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    # Emails are sent AFTER the pooled connection is released (audit b08
    # follow-up): SMTP can take seconds per recipient, and holding an idle
    # pooled connection across every send starved the pool during an
    # escalation storm. The claims above are already committed, so a
    # crash here never re-escalates.
    for row in claimed_rows:
        try:
            _notify_escalation(
                row["alert_id"], row["escalate_to_group_id"], row["group_name"],
                row["severity"], row["metric_name"], row["resource_id"],
                row["aws_account_id"],
            )
        except Exception:
            logger.exception(f"[escalation] notification failed for alert {row['alert_id']} "
                             f"(escalation itself is recorded)")
    return len(claimed_rows)
