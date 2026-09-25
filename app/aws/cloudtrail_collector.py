# app/aws/cloudtrail_collector.py
"""
Free, ReadOnlyAccess-compatible cloud-resource-level RCA data source:
AWS CloudTrail's LookupEvents API. AIOps roadmap Phase 1 (2026-09-14).

WHY THIS EXISTS (and why op_events/audit_logs are NOT this)
-------------------------------------------------------------
op_events (app/collector/op_log.py) records THIS APP's own operational
health (collector cycle failures, alert-eval errors). audit_logs
(app/audit.py) records actions taken INSIDE this app (threshold edits,
user management, console-link opens). Neither tells you anything about
what actually happened ON the AWS resources themselves -- a security-
group rule change, an instance stop, an IAM policy edit -- which is
what real root-cause analysis needs ("this alert started 4 minutes
after someone changed this security group"). CloudTrail is AWS's own
record of exactly that.

COST: $0. cloudtrail:LookupEvents is included in the AWS-managed
ReadOnlyAccess policy this app already assumes, needs no trail and no
S3 bucket to be configured, and AWS always retains the last 90 days of
management events per account for free, queryable via this API.

WHAT THIS COLLECTS
--------------------
Every LookupEvents record includes a Resources[] list (resource type +
ID/ARN touched by that API call). This module keeps only events whose
Resources[] overlaps a resource this app already tracks in `resources`
-- it does NOT mirror the account's entire CloudTrail history, only the
slice relevant to monitored resources, keeping `cloud_events` small and
fast to query. It also requests WriteOnly events only (LookupAttributes
ReadOnly=false) at the API level, so Describe/List/Get read-only calls
are never fetched or stored -- they'd be pure noise for RCA purposes.

CADENCE: "low" tier (15 min, see scheduler.py) -- config-change
correlation doesn't need faster-than-15-min freshness, and this API
costs nothing regardless, so cadence here is a freshness/collector-load
choice, not a billing one.

SCOPE: AWS only in this phase (the account has full AWS ReadOnly
access). Azure Activity Log / GCP Cloud Audit Logs are the equivalent
data sources for those providers and would follow the identical
pattern once read access to those logs is confirmed -- not built here
to avoid claiming coverage this phase doesn't have.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from app.db import get_connection
from app.aws.sts import get_boto3_session

logger = logging.getLogger(__name__)

# Wider than the 15-min poll interval so a single throttled/failed poll
# doesn't leave a permanent gap -- overlapping windows self-heal on the
# next cycle. Safe to re-fetch the same window repeatedly: deduplicated
# on (aws_account_id, event_id) via INSERT IGNORE at write time.
LOOKBACK_MINUTES = 30

# Caps API + storage cost for a single very noisy account so it can't
# starve the poll cycle (or the DB write volume) for every other
# account in the same run.
MAX_EVENTS_PER_ACCOUNT_PER_POLL = 200

# Trim CloudTrailEvent's raw JSON string before storing -- kept only as
# reference context for the RCA detail view, not re-parsed by this app,
# so an unbounded multi-KB payload isn't worth storing in full.
MAX_RAW_EVENT_CHARS = 8000


def _lookup_events(session, start_time, end_time):
    """Yields raw CloudTrail event dicts for one account, write-only
    management events only, capped at MAX_EVENTS_PER_ACCOUNT_PER_POLL."""
    client = session.client("cloudtrail")
    paginator = client.get_paginator("lookup_events")
    seen = 0
    for page in paginator.paginate(
        StartTime=start_time,
        EndTime=end_time,
        LookupAttributes=[{"AttributeKey": "ReadOnly", "AttributeValue": "false"}],
        PaginationConfig={"PageSize": 50},
    ):
        for event in page.get("Events", []):
            yield event
            seen += 1
            if seen >= MAX_EVENTS_PER_ACCOUNT_PER_POLL:
                return


def _tracked_resource_ids(cursor, aws_account_id):
    cursor.execute(
        "SELECT resource_id FROM resources WHERE aws_account_id = %s",
        (aws_account_id,),
    )
    return {row["resource_id"] for row in cursor.fetchall()}


def _matches_tracked_resource(event, tracked_ids):
    """CloudTrail's Resources[].ResourceName is a bare ID (i-xxxx) for
    some services and a full ARN for others -- match either form
    against resources.resource_id, which stores whichever form that
    resource type's own discovery collector uses (same ARN-vs-bare-ID
    inconsistency documented in 016_widen_resource_id.sql)."""
    for res in event.get("Resources") or []:
        name = res.get("ResourceName")
        if name and name in tracked_ids:
            return True
    return False


def poll_cloud_events() -> int:
    """
    Polls CloudTrail LookupEvents for every active AWS account, keeps
    only events touching a currently-tracked resource, and writes new
    ones into `cloud_events`. Returns the number of new rows written.
    One account's failure (e.g. missing cloudtrail:LookupEvents on an
    unusually narrow role) is logged as a non-fatal op_event and does
    not block the other accounts in the same poll.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    written = 0
    try:
        cursor.execute("""
            SELECT id, account_name, account_id, role_arn, auth_mode,
                   external_id, default_region
            FROM aws_accounts
            WHERE status = 'active'
        """)
        accounts = cursor.fetchall()

        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=LOOKBACK_MINUTES)

        for account in accounts:
            tracked_ids = _tracked_resource_ids(cursor, account["id"])
            if not tracked_ids:
                continue  # nothing discovered yet for this account -- skip the API call entirely

            try:
                session = get_boto3_session(account)
                for event in _lookup_events(session, start_time, end_time):
                    if not _matches_tracked_resource(event, tracked_ids):
                        continue

                    resource_ids = [
                        {"type": r.get("ResourceType"), "id": r.get("ResourceName")}
                        for r in (event.get("Resources") or [])
                    ]
                    raw = event.get("CloudTrailEvent", "") or ""
                    if len(raw) > MAX_RAW_EVENT_CHARS:
                        raw = raw[:MAX_RAW_EVENT_CHARS]

                    cursor.execute("""
                        INSERT IGNORE INTO cloud_events
                            (aws_account_id, event_id, event_name, event_source,
                             event_time, username, aws_region, resource_ids, raw_event)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        account["id"],
                        event.get("EventId"),
                        event.get("EventName"),
                        event.get("EventSource"),
                        event.get("EventTime"),
                        event.get("Username"),
                        account.get("default_region"),
                        json.dumps(resource_ids),
                        raw,
                    ))
                    written += cursor.rowcount
            except Exception as e:
                from app.collector.op_log import log_event
                log_event(
                    "cloudtrail_poll_failed",
                    f"CloudTrail poll failed for account {account['account_name']}: {e}",
                    severity="WARNING",
                    detail={"aws_account_id": account["id"]},
                )
                continue

        conn.commit()
        logger.info(f"[cloudtrail] wrote {written} new cloud event(s) across {len(accounts)} account(s)")
        return written
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


# RETENTION GAP (D02 audit, 2026-09): cloud_events is populated every
# "low" tier tick (15 min, see CADENCE above) for every active AWS
# account, forever -- there was no retention/prune job for it anywhere
# in the codebase (grepped app/ for DELETE FROM cloud_events: only hit
# was the one-time cleanup in admin/accounts.py's delete_account, which
# only fires when an account is removed entirely). Every other
# comparably-shaped append-only table populated by the low-tier
# scheduler (metric_history, op_events, synthetic_check_results) has
# its own prune_*() wired into scheduler.py's low tier; this one
# didn't. 90 days (longer than the other tables' 30-day default)
# matches this module's own docstring: AWS's CloudTrail LookupEvents
# API only ever returns 90 days of management events, so keeping rows
# older than that provides no way to backfill/re-verify from AWS
# either way, while RCA lookback sometimes benefits from the longer
# window these events exist for in the first place.
def prune_cloud_events(retain_days: int = 90) -> int:
    """Deletes cloud_events older than retain_days. Called from
    scheduler.py's low tier, same pattern as
    synthetic.prune_synthetic_results()."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM cloud_events WHERE event_time < DATE_SUB(NOW(), INTERVAL %s DAY)",
            (retain_days,),
        )
        deleted = cursor.rowcount
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
