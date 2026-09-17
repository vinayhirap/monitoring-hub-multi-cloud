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
   same resource_type WHEN that resource_id is an ARN or an
   AWS-generated opaque ID (EC2 instance IDs, volume IDs, ENI IDs,
   etc.) -- those are unique within AWS by construction, so a match
   across accounts is definitive proof some code path resolved the
   wrong account's credentials for one of them.

   This does NOT hold for the human/CloudFormation-assigned NAMES this
   app also stores as resource_id for most extended-tier services --
   CloudWatch Logs group names, Backup plan names, API Gateway names,
   DynamoDB table names, etc. (see app/collector/discovery/extended.py's
   module docstring: resource_id is deliberately set to "whatever value
   that service's CloudWatch dimension needs", which for most of the 33
   extended services is a name, not an AWS-assigned ID). Two accounts
   built from the same landing-zone template WILL legitimately share
   these -- confirmed 2026-09-17 for 'logs'/'backup' resources shared
   between accounts 7 and 10 (System, Default, cid-DataExportCreator):
   each account had its own distinct `resources` row, both freshly
   polled, no flipping -- exactly the correctly-isolated state
   045/046 were built to produce, not a bug. The original version of
   this check treated ALL resource_ids as globally unique and would
   have alerted on this forever. It now only evaluates resource_ids
   matching _GLOBALLY_UNIQUE_ID_PATTERN (an ARN or an AWS-generated
   hex-suffixed ID) so it keeps catching the real failure mode without
   permanently crying wolf on shared naming templates.

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

# (table, expected unique key name, expected column tuple). Every one of
# these tables stores a raw AWS resource_id STRING (unique only WITHIN
# one account, never globally -- e.g. "System", a stock CloudWatch Logs
# group name), so its identity-defining unique key MUST include
# aws_account_id or two accounts sharing a resource name silently merge
# into one row (2026-09-16 AuroGov Mumbai/U4RAD incident: resources'
# uniq_resource key omitted it; 046 found the same shape in alert_pending
# and metric_baseline). This check does NOT rely on a collision ever
# actually being visible in the data -- unlike
# find_cross_account_resource_collisions() below, which structurally
# CANNOT catch this bug shape: a too-narrow unique key prevents the
# second account's row from ever being *inserted* in the first place, so
# there is never a duplicate pair sitting in the table to notice. This
# check instead asserts the key's own column composition, every cycle,
# so a future migration/hotfix that accidentally drops back to an
# unscoped key is caught immediately rather than requiring another
# multi-hour "why does account X have no resources" investigation.
# Matches an ARN ("arn:aws:...") or an AWS-generated opaque ID: a short
# lowercase-alnum prefix, a hyphen, then 8+ hex chars (i-0abc123def456789,
# vol-0abc..., eni-0abc..., ami-0abc..., snap-0abc..., sg-, vpc-, subnet-,
# fs-, nat-, igw-, rtb-, vgw-, tgw-, pcx-, eipalloc-, etc.). Deliberately
# does NOT match plain names (System, Default, my-table, cid-Something) --
# those are only unique within one account and are expected to repeat
# across accounts that share a naming template. See this module's
# find_cross_account_resource_collisions() docstring for why this
# distinction matters.
_GLOBALLY_UNIQUE_ID_PATTERN = r'^(arn:|[a-z0-9]{1,8}-[0-9a-f]{8,})'

_EXPECTED_SCOPED_UNIQUE_KEYS = {
    "resources": ("uniq_resource_identity", {"aws_account_id", "resource_type", "resource_id"}),
    "alert_pending": ("uq_pending_account_resource_metric", {"aws_account_id", "resource_id", "metric_name"}),
    "metric_baseline": ("uniq_account_baseline_bucket",
                         {"aws_account_id", "resource_id", "metric_name", "hour_of_day", "day_of_week"}),
}


def find_cross_account_resource_collisions(cursor) -> list[dict]:
    """
    Returns [{resource_type, resource_id, account_ids: [..]}] for every
    ARN or AWS-generated-ID resource_id (see _GLOBALLY_UNIQUE_ID_PATTERN)
    currently attached to more than one active aws_account_id. Plain
    names are deliberately excluded -- they're expected to repeat across
    accounts. Empty list = healthy (the expected, overwhelmingly common
    case).
    """
    cursor.execute(
        """
        SELECT r.resource_type, r.resource_id,
               GROUP_CONCAT(DISTINCT r.aws_account_id ORDER BY r.aws_account_id) AS account_ids
        FROM resources r
        JOIN aws_accounts a ON a.id = r.aws_account_id
        WHERE a.status = 'active'
          AND r.resource_id REGEXP %s
        GROUP BY r.resource_type, r.resource_id
        HAVING COUNT(DISTINCT r.aws_account_id) > 1
        """,
        (_GLOBALLY_UNIQUE_ID_PATTERN,),
    )
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


def check_unscoped_identity_keys(cursor) -> list[dict]:
    """
    Returns [{table, expected_key, issue}] for every table in
    _EXPECTED_SCOPED_UNIQUE_KEYS whose account-scoped unique key is
    missing, or whose columns don't match what's expected. Empty list =
    healthy. See this module's docstring for why this check exists
    alongside (not instead of) find_cross_account_resource_collisions().
    """
    drifted = []
    for table, (expected_key_name, expected_cols) in _EXPECTED_SCOPED_UNIQUE_KEYS.items():
        cursor.execute(
            "SELECT COUNT(*) AS c FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (table,),
        )
        if not cursor.fetchone()["c"]:
            continue  # table doesn't exist yet -- not this check's job

        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s",
            (table, expected_key_name),
        )
        actual_cols = {row["COLUMN_NAME"] for row in cursor.fetchall()}

        if not actual_cols:
            drifted.append({
                "table": table, "expected_key": expected_key_name,
                "issue": f"key is missing entirely (expected columns {sorted(expected_cols)})",
            })
        elif actual_cols != expected_cols:
            drifted.append({
                "table": table, "expected_key": expected_key_name,
                "issue": f"key exists but covers {sorted(actual_cols)}, expected {sorted(expected_cols)}",
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

    result = {"collisions": [], "width_drift": [], "unscoped_keys": []}
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            result["collisions"] = find_cross_account_resource_collisions(cursor)
            result["width_drift"] = check_resource_id_column_widths(cursor)
            result["unscoped_keys"] = check_unscoped_identity_keys(cursor)
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
            f"{len(result['collisions'])} resource(s) with a globally-unique ARN/AWS-generated ID "
            f"are attached to more than one active AWS account -- this is only possible if a "
            f"credential-resolution bug queried the wrong account for one of them (see the "
            f"2026-09-16 U4RAD/AuroGov Mumbai incident). Plain-name resource_ids are excluded from "
            f"this check since they're expected to repeat across accounts sharing a naming template:\n"
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

    if result["unscoped_keys"]:
        lines = [
            f"  - {d['table']} ({d['expected_key']}): {d['issue']}"
            for d in result["unscoped_keys"]
        ]
        msg = (
            f"{len(result['unscoped_keys'])} table(s) have an identity unique key that "
            f"is missing account scoping -- two different accounts sharing a resource "
            f"name (e.g. the stock 'System' CloudWatch Logs group) can silently merge "
            f"into one row with NO error and NO duplicate ever visible in the data "
            f"(see the 2026-09-16 AuroGov Mumbai/U4RAD incident and its 045/046 fixes):\n"
            + "\n".join(lines)
        )
        log_event("unscoped_identity_key_drift", msg, severity="CRITICAL")
        _notify_admins("[CloudOps] Cross-account data isolation at risk: unscoped identity key", msg)

    return result
