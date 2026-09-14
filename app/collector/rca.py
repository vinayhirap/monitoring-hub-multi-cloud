# app/collector/rca.py
"""
Root-cause analysis -- AIOps roadmap Phase 1 (2026-09-14), deepened and
made customer-facing (2026-09-14, later same day).

TWO ENTRY POINTS NOW:

  rank_probable_cause(incident_id) -- unchanged from the original
  version: analyzes a topology-correlated INCIDENT (2+ alerts already
  grouped by correlate.py). Internal use only -- feeds
  incidents.primary_resource_id/probable_cause, consumed by the
  (hidden-from-end-users) Incidents page for admin/internal use.

  explain_alert(alert_id) -- NEW. Works for ANY single alert, whether
  or not it's part of a multi-alert incident, because most alerts a
  real customer sees are standalone. Surfaced directly on the existing,
  customer-facing Alerts page (app/api/alerts.py's
  GET /alerts/{id}/explain), in plain English -- no "topology
  in-degree" style internal jargon, no assumption the reader has code
  or ops access. This is the deep-RCA surface that matters for a
  product actual customers use.

Both share the same underlying signal-gathering (_gather_signals):
  1. cloud_events (REAL AWS CloudTrail activity, via
     app/aws/cloudtrail_collector.py -- deliberately NOT op_events or
     audit_logs, neither of which records anything that happened on
     the AWS resources themselves) in a window before the breach,
     touching the resource or something one topology hop upstream of
     it -- surfaced as a PROBABLE trigger, never stated as confirmed.
  2. audit_logs (this app's OWN config changes -- e.g. someone loosened
     a threshold right before this alert) in the same window, since a
     misconfiguration can look identical to a real incident.
  3. Topology position (how many other resources depend on this one),
     translated into plain language ("N other resources rely on this
     one") rather than graph-theory terms.

explain_alert() additionally adds, which rank_probable_cause() does
not need for its internal use:
  4. Trend context -- was this a sudden spike in the last 15 minutes,
     or a gradual climb over the last couple of hours? (see
     _trend_context) -- genuinely useful context a customer can act on
     ("this has been climbing for 2 hours" vs "this just spiked") that
     a bare threshold-breach notification never gives.
  5. Whether this alert is already correlated with others into an
     incident (without ever using the word "incident" in the
     customer-facing summary) -- "this is happening alongside N other
     alerts right now" is meaningful to a customer even if they never
     see the internal Incidents page those alerts are grouped in.
  6. A confidence label (high/medium/low) based on how many
     corroborating signals were actually found -- so the reader can
     tell "a probable trigger was found" from "nothing was found,
     take this with a grain of salt" at a glance.
"""
import logging
from app.db import get_connection

logger = logging.getLogger(__name__)

TRIGGER_LOOKBACK_MINUTES = 20
TREND_LOOKBACK_HOURS = 2
TREND_RECENT_WINDOW_MINUTES = 15
# If the slope over just the last TREND_RECENT_WINDOW_MINUTES is more
# than this many times steeper than the slope over the whole
# TREND_LOOKBACK_HOURS window, the breach reads as a sudden spike
# rather than a gradual climb.
SUDDEN_SPIKE_SLOPE_RATIO = 3.0


def _gather_signals(cursor, resource_id, around_time, lookback_minutes=TRIGGER_LOOKBACK_MINUTES):
    """Shared by both entry points: topology in-degree, real AWS
    CloudTrail activity, and this app's own config-change audit trail
    around a given resource + point in time. See module docstring."""
    cursor.execute("""
        SELECT COUNT(DISTINCT source_resource_id) AS in_degree
        FROM resource_relationships
        WHERE target_resource_id = %s
    """, (resource_id,))
    in_degree = cursor.fetchone()["in_degree"] or 0

    # JSON_SEARCH (not JSON_CONTAINS, which does not accept a wildcard
    # path) finds the resource id string anywhere inside
    # cloud_events.resource_ids' array of {"type":..., "id":...} objects.
    cursor.execute("""
        SELECT ce.event_name, ce.event_source, ce.username, ce.event_time,
               ce.resource_ids
        FROM cloud_events ce
        WHERE ce.event_time BETWEEN DATE_SUB(%s, INTERVAL %s MINUTE) AND %s
          AND (
              JSON_SEARCH(ce.resource_ids, 'one', %s) IS NOT NULL
              OR EXISTS (
                  SELECT 1 FROM resource_relationships rel
                  WHERE rel.target_resource_id = %s
                    AND JSON_SEARCH(ce.resource_ids, 'one', rel.source_resource_id) IS NOT NULL
              )
          )
        ORDER BY ce.event_time DESC
        LIMIT 10
    """, (around_time, lookback_minutes, around_time, resource_id, resource_id))
    cloud_events = cursor.fetchall()

    # audit_logs.payload is a free-form JSON blob -- best-effort
    # substring match, not a structured join; a false miss here just
    # means no config-change context is shown, never a false alarm.
    resource_like_pattern = f"%{resource_id}%"
    cursor.execute("""
        SELECT actor, action, payload, created_at
        FROM audit_logs
        WHERE created_at BETWEEN DATE_SUB(%s, INTERVAL %s MINUTE) AND %s
          AND payload IS NOT NULL
          AND JSON_SEARCH(payload, 'one', %s) IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 10
    """, (around_time, lookback_minutes, around_time, resource_like_pattern))
    config_changes = cursor.fetchall()

    return in_degree, cloud_events, config_changes


def _trend_context(cursor, resource_id, metric_name, breach_time):
    """Characterizes the metric's own behavior in the hours leading up
    to the breach -- sudden spike vs gradual climb vs flat-then-breach.
    Plain linear-regression slope comparison (numpy), same tool as
    app/collector/trend.py, applied over a short pre-breach window
    instead of a long capacity-forecast window."""
    import numpy as np

    cursor.execute("""
        SELECT UNIX_TIMESTAMP(h.metric_timestamp) AS ts, h.metric_value
        FROM metric_history h
        JOIN resources r ON r.id = h.resource_id
        WHERE r.resource_id = %s AND h.metric_name = %s
          AND h.metric_timestamp BETWEEN DATE_SUB(%s, INTERVAL %s HOUR) AND %s
          AND h.metric_value IS NOT NULL
        ORDER BY h.metric_timestamp ASC
    """, (resource_id, metric_name, breach_time, TREND_LOOKBACK_HOURS, breach_time))
    points = cursor.fetchall()

    if len(points) < 5:
        return {
            "pattern": "insufficient_data",
            "description": "Not enough recent history to tell whether this was a sudden change or a gradual trend.",
        }

    ts = np.array([p["ts"] for p in points], dtype=float)
    vals = np.array([p["metric_value"] for p in points], dtype=float)
    ts_minutes = (ts - ts.min()) / 60.0

    whole_slope = float(np.polyfit(ts_minutes, vals, 1)[0])

    recent_mask = ts_minutes >= (ts_minutes.max() - TREND_RECENT_WINDOW_MINUTES)
    if recent_mask.sum() >= 3:
        recent_slope = float(np.polyfit(ts_minutes[recent_mask], vals[recent_mask], 1)[0])
    else:
        recent_slope = whole_slope

    ratio = abs(recent_slope) / (abs(whole_slope) + 1e-9)

    if ratio > SUDDEN_SPIKE_SLOPE_RATIO:
        return {
            "pattern": "sudden_spike",
            "description": (
                f"This jumped sharply in the last {TREND_RECENT_WINDOW_MINUTES} minutes before the "
                f"alert, rather than building up gradually."
            ),
        }
    if abs(whole_slope) > 1e-9:
        direction = "climbing" if whole_slope > 0 else "declining"
        return {
            "pattern": "gradual_trend",
            "description": f"This had been steadily {direction} over the last {TREND_LOOKBACK_HOURS} hours before the alert.",
        }
    return {
        "pattern": "flat_then_breach",
        "description": "This was stable beforehand and then crossed the threshold without a clear build-up.",
    }


def rank_probable_cause(incident_id: int):
    """
    INTERNAL USE -- analyzes a topology-correlated incident (2+ member
    alerts). Returns a dict describing the probable root cause, or None
    if the incident has no member alerts. Also persists the result onto
    incidents.primary_resource_id/probable_cause. See module docstring
    for why customer-facing RCA should go through explain_alert()
    instead.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT a.id, a.resource_id, a.metric_name, a.triggered_at AS created_at, a.severity
            FROM incident_alerts ia
            JOIN alerts a ON a.id = ia.alert_id
            WHERE ia.incident_id = %s
            ORDER BY a.triggered_at ASC
        """, (incident_id,))
        alerts = cursor.fetchall()
        if not alerts:
            return None

        earliest = alerts[0]
        candidate_resource = earliest["resource_id"]

        in_degree, cloud_trigger_events, config_changes = _gather_signals(
            cursor, candidate_resource, earliest["created_at"]
        )

        reason_parts = [
            f"Earliest breach in this incident: {earliest['metric_name']} on "
            f"{candidate_resource} at {earliest['created_at']}."
        ]
        if in_degree:
            reason_parts.append(f"{in_degree} other resource(s) depend on it in the topology graph.")
        if cloud_trigger_events:
            top = cloud_trigger_events[0]
            reason_parts.append(
                f"Possible trigger: {top['event_name']} by {top['username'] or 'unknown'} "
                f"at {top['event_time']} (probable, not confirmed)."
            )
        if config_changes:
            reason_parts.append(
                "A monitoring-hub config change was also made in this window -- "
                "worth checking whether this is a real incident or a threshold/config edit."
            )

        result = {
            "resource_id": candidate_resource,
            "in_degree": in_degree,
            "reason": " ".join(reason_parts),
            "cloud_events": cloud_trigger_events,
            "config_changes": config_changes,
        }

        cursor.execute("""
            UPDATE incidents
            SET primary_resource_id = %s, probable_cause = %s
            WHERE id = %s
        """, (candidate_resource, result["reason"], incident_id))
        conn.commit()
        return result
    finally:
        cursor.close()
        conn.close()


def explain_alert(alert_id: int):
    """
    CUSTOMER-FACING -- plain-English root-cause explanation for a
    SINGLE alert, whether or not it's part of a correlated incident.
    Returns None if the alert doesn't exist. See module docstring.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT id, resource_id, metric_name, severity, triggered_at,
                   current_value, threshold
            FROM alerts WHERE id = %s
        """, (alert_id,))
        alert = cursor.fetchone()
        if not alert:
            return None

        resource_id = alert["resource_id"]
        breach_time = alert["triggered_at"]

        in_degree, cloud_events, config_changes = _gather_signals(cursor, resource_id, breach_time)
        trend = _trend_context(cursor, resource_id, alert["metric_name"], breach_time)

        cursor.execute("""
            SELECT ia.incident_id, COUNT(*) AS other_count
            FROM incident_alerts ia
            WHERE ia.incident_id = (
                SELECT incident_id FROM incident_alerts WHERE alert_id = %s LIMIT 1
            )
            AND ia.alert_id != %s
            GROUP BY ia.incident_id
        """, (alert_id, alert_id))
        related = cursor.fetchone()

        signal_count = sum([
            bool(cloud_events), bool(config_changes), in_degree > 0, related is not None,
        ])
        confidence = "high" if signal_count >= 2 else ("medium" if signal_count == 1 else "low")

        summary_parts = []
        if cloud_events:
            top = cloud_events[0]
            summary_parts.append(
                f"A change was made on AWS shortly before this alert \u2014 {top['event_name']} "
                f"by {top['username'] or 'an unknown user'} at {top['event_time']}. "
                f"This is the most likely trigger, though not confirmed."
            )
        summary_parts.append(trend["description"])
        if in_degree:
            summary_parts.append(
                f"{in_degree} other resource(s) rely on this one, so the impact may be wider than this single alert."
            )
        if related:
            summary_parts.append(
                f"This is happening alongside {related['other_count']} related alert(s) around the "
                f"same time \u2014 likely part of the same underlying issue."
            )
        if config_changes:
            summary_parts.append(
                "Note: a monitoring configuration change was also made in this window \u2014 worth "
                "double-checking this isn't a false alarm from a threshold edit."
            )
        if not cloud_events and not config_changes and not in_degree and not related:
            summary_parts.append(
                "No related AWS activity, configuration change, or dependent resource was found "
                "in the surrounding window \u2014 this may be an isolated fluctuation."
            )

        return {
            "alert_id": alert_id,
            "resource_id": resource_id,
            "confidence": confidence,
            "summary": " ".join(summary_parts),
            "trend": trend,
            "probable_trigger": cloud_events[0] if cloud_events else None,
            "related_alert_count": related["other_count"] if related else 0,
        }
    finally:
        cursor.close()
        conn.close()
