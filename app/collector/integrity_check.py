# app/collector/integrity_check.py
"""
Structural safety net for the 2026-09-16 U4RAD/AuroGov Mumbai incident
class of bug -- added AFTER four independent code paths were each found
to have their own broken, role_arn-only credential resolution that
silently fell back to ambient (wrong-account) credentials for
static-key accounts, plus a fifth, unrelated data-truncation bug that
surfaced at the same time.

Patching each occurrence as it was found is not a permanent fix -- the
next one could be in code not yet written. This module instead detects
the SYMPTOM directly, independent of root cause, so any future
regression (a new collector, a copy-pasted credential branch, a schema
drift) gets caught automatically within one discovery cycle instead of
requiring a screenshot and a multi-hour manual investigation.

Two checks:

1. Cross-account resource collisions. Two different active AWS
   accounts can NEVER legitimately share the same resource_id for the
   same resource_type -- EC2 instance IDs, ARNs, etc. are unique
   within AWS by construction. If this table ever shows the same
   (resource_type, resource_id) under two different aws_account_id
   values, that is definitive proof some code path resolved the wrong
   account's credentials for one of them -- exactly what happened here,
   just now caught by asserting the invariant instead of a human
   noticing the dashboard looks wrong.

2. resource_id-shaped column widths. The other half of this incident
   (migration 044) was alerts.resource_id silently sitting at
   VARCHAR(50) while every table that stores the same *kind* of value
   had already standardized on VARCHAR(512) (to match
   resources.resource_id, widened in migration 016). A live ALTER TABLE
   run by hand (or a migration applied to only one server) is exactly
   the kind of drift that caused the original 2026-08-26 AuroGov Mumbai
   RCA too. This check re-verifies the known set of resource_id-shaped
   columns every cycle so drift is caught immediately instead of
   waiting for the next long ARN to hit it.

Wired into app.collector.discovery.runner.run_discovery(), so it runs
automatically on the existing 15-minute discovery cadence for every
account -- no separate scheduling, no new cron job, nothing an operator
has to remember to run.
"""
import logging

from app.db import get_connection

logger = logging.getLogger(__name__)

# (table, column): expected VARCHAR width. Every column here is meant to
# hold the same kind of value -- an AWS resource_id/ARN/name -- and per
# the comments left across migrations 016/021/022/034/036/044, they are
# all supposed to be kept in sync at 512. Add a new entry here whenever
# a new table adopts the same convention.
_EXPECTED_RESOURCE_ID_WIDTHS = {
    ("resources", "resource_id"): 512,
    ("alerts", "resource_id"): 512,
    ("op_events", "resource_id"): 512,
    ("alert_pending", "resource_id"): 500,  # migration 012's own stated width
    ("resource_relationships", "source_resource_id"): 512,
    ("resource_relationships", "target_resource_id"): 512,
}


def find_cross_account_resource_collisions(cursor) -> list[dict]:
    """
    Returns [{resource_type, resource_id, account_ids: [..]}] for every
    (resource_type, resource_id) pair currently attached to more than
    one active aws_account_id. Empty list = healthy (the expected,
    overwhelmingly common case).
    """
    cursor.execute("""
        SELECT r.resource_type, r.resource_id,
               GROUP_CONCAT(DISTINCT r.aws_account_id ORDER BY r.aws_account_id) AS account_ids
        FROM resources r
        JOIN aws_accounts a ON a.id = r.aws_account_id
        WHERE a.status = 'active'
        GROUP BY r.resource_type, r.resource_id
        HAVING COUNT(DISTINCT r.aws_account_id) > 1
    """)
    rows = cursor.fetchall()
    return [
        {
            "resource_type": row["resource_type"],
            "resource_id": row["resource_id"],
            "account_ids": [int(x) for x in row["account_ids"].split(",")],
        }
        for row in rows
    ]


def check_resource_id_column_widths(cursor) -> list[dict]:
    """
    Returns [{table, column, expected, actual}] for every column in
    _EXPECTED_RESOURCE_ID_WIDTHS whose live VARCHAR width doesn't match
    what it's supposed to be. Empty list = healthy.
    """
    drifted = []
    for (table, column), expected in _EXPECTED_RESOURCE_ID_WIDTHS.items():
        cursor.execute(
            "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
            (table, column),
        )
        row = cursor.fetchone()
        if not row or row.get("CHARACTER_MAXIMUM_LENGTH") is None:
            continue  # table/column doesn't exist yet (e.g. not migrated) -- not this check's job
        actual = row["CHARACTER_MAXIMUM_LENGTH"]
        if actual != expected:
            drifted.append({
                "table": table, "column": column,
                "expected": expected, "actual": actual,
            })
    return drifted


def _notify_admins(subject: str, body: str) -> None:
    """
    Emails every admin-role user with an email on file, same
    degrade-gracefully pattern as app.collector.escalation._notify_escalation
    (SMTP not configured / no recipients -> log and return, never raise).
    A cross-account data-integrity failure or a schema-width drift is a
    platform-health issue, not any one team's alert, so this notifies
    admins broadly rather than looking up an org_group.
    """
    from app.email import mailer

    if not mailer.is_configured():
        logger.warning(f"integrity_check: {subject} -- SMTP not configured, no email sent")
        return

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT DISTINCT email FROM users
            WHERE role = 'admin' AND email IS NOT NULL AND email != ''
        """)
        recipients = [row["email"] for row in cursor.fetchall()]
    finally:
        cursor.close()
        conn.close()

    if not recipients:
        logger.warning(f"integrity_check: {subject} -- no admin users have an email on file, no email sent")
        return

    sent = sum(1 for to_addr in recipients if mailer.send_email(to_addr, subject, body))
    logger.info(f"integrity_check: {subject} -- emailed {sent}/{len(recipients)} admin(s)")


def run_integrity_check() -> dict:
    """
    Runs both checks and, for anything found, writes a CRITICAL op_event
    (visible in Operational Events) and emails admins. Called at the end
    of every discovery cycle. Never raises -- a failure in this check
    must never take down discovery itself.
    """
    from app.collector.op_log import log_event

    result = {"collisions": [], "width_drift": []}
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            result["collisions"] = find_cross_account_resource_collisions(cursor)
            result["width_drift"] = check_resource_id_column_widths(cursor)
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        logger.error(f"integrity_check: failed to run: {e}")
        return result

    if result["collisions"]:
        lines = [
            f"  - {c['resource_type']} {c['resource_id']} attached to accounts {c['account_ids']}"
            for c in result["collisions"]
        ]
        msg = (
            f"{len(result['collisions'])} resource(s) are attached to more than one active "
            f"AWS account -- this is only possible if a credential-resolution bug queried the "
            f"wrong account for one of them (see the 2026-09-16 U4RAD/AuroGov Mumbai incident):\n"
            + "\n".join(lines)
        )
        log_event("cross_account_resource_collision", msg, severity="ERROR")
        _notify_admins("[CloudOps] Cross-account resource collision detected", msg)

    if result["width_drift"]:
        lines = [
            f"  - {d['table']}.{d['column']}: expected VARCHAR({d['expected']}), is VARCHAR({d['actual']})"
            for d in result["width_drift"]
        ]
        msg = (
            f"{len(result['width_drift'])} resource_id-shaped column(s) have drifted from their "
            f"expected width -- this can silently break writes the next time a long ARN/name hits "
            f"them (see migration 044_widen_alerts_resource_id.sql for precedent):\n"
            + "\n".join(lines)
        )
        log_event("resource_id_column_width_drift", msg, severity="WARNING")
        _notify_admins("[CloudOps] Schema drift: resource_id column width", msg)

    return result
