# app/collector/rca.py
"""
Probable-root-cause ranking for an incident -- AIOps roadmap Phase 1
(2026-09-14).

Deterministic graph-walk + timing correlation, NOT a black-box model --
the same "deterministic causal analysis over a topology graph"
description Dynatrace gives its own Davis AI engine, at the scope this
app's data actually supports:

  1. Among the incident's member alerts, the EARLIEST one is the
     leading root-cause candidate -- in a cascading failure (ALB
     breaches, then its EC2 targets breach), the first domino is
     usually the real cause, not the last thing to visibly break.
  2. That resource's topology in-degree (how many other resources point
     AT it) is reported as supporting context -- a widely-depended-on
     resource breaking first is a stronger signal than a leaf node
     breaking first.
  3. cloud_events (REAL AWS CloudTrail activity -- see
     app/aws/cloudtrail_collector.py; this is deliberately NOT
     op_events or audit_logs, neither of which records anything that
     happened on the AWS resources themselves) in a short window before
     that earliest alert, touching the candidate resource or something
     one topology hop upstream of it, are surfaced as the PROBABLE
     trigger -- reported as probable, never confirmed, matching this
     app's own RCA-writing convention (see monitoring_hub_mumbai_rca.md)
     of stating verified facts and labeling inference as inference.
  4. audit_logs (this app's OWN config changes -- e.g. someone loosened
     a threshold right before this "incident") are checked too, since a
     misconfiguration can look identical to a real one.

Called ON-DEMAND when an incident is created or its detail view is
opened (app/api/incidents.py) -- there is nothing to analyze before an
incident exists, so this is not a scheduled job.
"""
import logging
from app.db import get_connection

logger = logging.getLogger(__name__)

TRIGGER_LOOKBACK_MINUTES = 20


def rank_probable_cause(incident_id: int):
    """
    Returns a dict describing the probable root cause of the given
    incident, or None if the incident has no member alerts. Also
    persists the result onto incidents.primary_resource_id /
    probable_cause so the incidents list view doesn't need to
    recompute this for every row.
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

        cursor.execute("""
            SELECT COUNT(DISTINCT source_resource_id) AS in_degree
            FROM resource_relationships
            WHERE target_resource_id = %s
        """, (candidate_resource,))
        in_degree = cursor.fetchone()["in_degree"] or 0

        # Real AWS activity on the candidate resource itself, or on
        # anything one topology hop upstream of it, in the window just
        # before the earliest alert. JSON_SEARCH (not JSON_CONTAINS,
        # which does not accept a wildcard path) finds the resource id
        # string anywhere inside cloud_events.resource_ids' array of
        # {"type":..., "id":...} objects.
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
        """, (earliest["created_at"], TRIGGER_LOOKBACK_MINUTES, earliest["created_at"],
              candidate_resource, candidate_resource))
        cloud_trigger_events = cursor.fetchall()

        # This app's OWN config-change trail (audit_logs.payload is a
        # free-form JSON blob -- e.g. {"detail": "...", "role": "..."}
        # -- so this is a best-effort substring match, not a structured
        # join; a false miss here just means no config-change context is
        # shown, never a false alarm).
        resource_like_pattern = f"%{candidate_resource}%"
        cursor.execute("""
            SELECT actor, action, payload, created_at
            FROM audit_logs
            WHERE created_at BETWEEN DATE_SUB(%s, INTERVAL %s MINUTE) AND %s
              AND payload IS NOT NULL
              AND JSON_SEARCH(payload, 'one', %s) IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 10
        """, (earliest["created_at"], TRIGGER_LOOKBACK_MINUTES, earliest["created_at"],
              resource_like_pattern))
        config_changes = cursor.fetchall()

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
