# app/collector/correlate.py
"""
Topology-based alert correlation -- AIOps roadmap Phase 1 (2026-09-14).

Groups currently-active `alerts` rows into `incidents` when they share
a `resource_relationships` edge (auto or manual, either direction)
within a short time window, instead of leaving every breaching
resource+metric as its own disconnected row. This extends the alert-
grouping already shipped (alerts.group_key collapses repeated breaches
of the SAME resource+metric into one row); this collapses breaches
ACROSS different-but-topologically-related resources into one incident
-- e.g. an ALB breach and its target EC2 instances' breaches becoming
one incident instead of three unrelated-looking alerts.

Deliberately NOT every active alert becomes an incident -- a standalone
breach with no topologically-connected active alert stays a plain
`alerts` row, unchanged from today's behavior. Incidents are additive,
not a replacement for the existing alert list.

Runs on the "low" tier (15 min, see scheduler.py) -- the correlation
window below is wider than the poll cadence, so 15-min freshness is
sufficient to catch a cascading failure without needing to run on
every 5-min alert-evaluation cycle.
"""
import logging
from app.db import get_connection

logger = logging.getLogger(__name__)

# Two active alerts are treated as one incident if they started within
# this many minutes of each other AND their resources share a
# resource_relationships edge. Wide enough to catch a cascade (ALB
# breaches, then its EC2 targets breach a few minutes later) without
# merging two genuinely unrelated incidents that happen to land in the
# same 15-minute poll window.
CORRELATION_WINDOW_MINUTES = 30


def correlate_alerts_into_incidents():
    """
    Attaches loose active alerts to an existing open incident where a
    topological connection + time-window match exists, or seeds a new
    incident from a pair of newly-connected loose alerts. Also
    auto-resolves incidents whose member alerts have all resolved.
    Returns (incidents_created, alerts_attached).
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    incidents_created = 0
    alerts_attached = 0
    _new_incident_ids = []
    try:
        cursor.execute("""
            SELECT a.id, a.resource_id, a.severity, a.triggered_at AS created_at,
                   a.aws_account_id
            FROM alerts a
            LEFT JOIN incident_alerts ia ON ia.alert_id = a.id
            LEFT JOIN incidents i ON i.id = ia.incident_id AND i.status = 'active'
            WHERE a.status = 'active' AND i.id IS NULL
        """)
        loose_alerts = cursor.fetchall()

        for alert in loose_alerts:
            # 1. Can this alert join an EXISTING open incident?
            cursor.execute("""
                SELECT DISTINCT i.id
                FROM incidents i
                JOIN incident_alerts ia ON ia.incident_id = i.id
                JOIN alerts a2 ON a2.id = ia.alert_id
                JOIN resource_relationships rel
                    ON (rel.source_resource_id = a2.resource_id AND rel.target_resource_id = %s)
                    OR (rel.target_resource_id = a2.resource_id AND rel.source_resource_id = %s)
                WHERE i.status = 'active'
                  AND ABS(TIMESTAMPDIFF(MINUTE, i.started_at, %s)) <= %s
                LIMIT 1
            """, (alert["resource_id"], alert["resource_id"],
                  alert["created_at"], CORRELATION_WINDOW_MINUTES))
            existing = cursor.fetchone()

            if existing:
                incident_id = existing["id"]
                cursor.execute(
                    "INSERT IGNORE INTO incident_alerts (incident_id, alert_id) VALUES (%s, %s)",
                    (incident_id, alert["id"]),
                )
                if cursor.rowcount:
                    alerts_attached += 1
                cursor.execute("""
                    UPDATE incidents
                    SET last_seen_at = NOW(),
                        severity = IF(%s = 'CRITICAL', 'CRITICAL', severity)
                    WHERE id = %s
                """, (alert["severity"], incident_id))
                continue

            # 2. No open incident to join -- does this alert have a
            #    topologically-connected, also-loose active alert to
            #    seed a NEW incident with?
            cursor.execute("""
                SELECT a2.id AS other_alert_id
                FROM alerts a2
                LEFT JOIN incident_alerts ia2 ON ia2.alert_id = a2.id
                JOIN resource_relationships rel
                    ON (rel.source_resource_id = a2.resource_id AND rel.target_resource_id = %s)
                    OR (rel.target_resource_id = a2.resource_id AND rel.source_resource_id = %s)
                WHERE a2.status = 'active'
                  AND a2.id != %s
                  AND ia2.alert_id IS NULL
                  AND ABS(TIMESTAMPDIFF(MINUTE, a2.triggered_at, %s)) <= %s
                LIMIT 1
            """, (alert["resource_id"], alert["resource_id"], alert["id"],
                  alert["created_at"], CORRELATION_WINDOW_MINUTES))
            partner = cursor.fetchone()
            if not partner:
                continue  # a standalone breach -- correctly stays a plain alert

            cursor.execute("""
                INSERT INTO incidents
                    (aws_account_id, title, severity, status, started_at, last_seen_at)
                VALUES (%s, %s, %s, 'active', %s, NOW())
            """, (
                alert["aws_account_id"],
                f"Correlated breach on {alert['resource_id']} and related resource(s)",
                alert["severity"],
                alert["created_at"],
            ))
            incident_id = cursor.lastrowid
            incidents_created += 1

            cursor.execute("INSERT IGNORE INTO incident_alerts (incident_id, alert_id) VALUES (%s, %s)",
                            (incident_id, alert["id"]))
            cursor.execute("INSERT IGNORE INTO incident_alerts (incident_id, alert_id) VALUES (%s, %s)",
                            (incident_id, partner["other_alert_id"]))
            alerts_attached += 2
            _new_incident_ids.append(incident_id)

        # Auto-resolve incidents whose member alerts have ALL resolved.
        cursor.execute("""
            UPDATE incidents i
            SET status = 'resolved', resolved_at = NOW()
            WHERE i.status = 'active'
              AND NOT EXISTS (
                  SELECT 1 FROM incident_alerts ia
                  JOIN alerts a ON a.id = ia.alert_id
                  WHERE ia.incident_id = i.id AND a.status = 'active'
              )
        """)

        conn.commit()
        if incidents_created or alerts_attached:
            logger.info(f"[correlate] {incidents_created} incident(s) created, "
                        f"{alerts_attached} alert(s) attached this cycle")

        # Rank a probable root cause for every newly-created incident so
        # the incidents list already has one to show, not just on first
        # detail-view open. Uses its own connection (see rca.py) --
        # deliberately not folded into this function's transaction, so
        # an RCA-ranking failure for one incident can't roll back the
        # correlation work above that already succeeded.
        for incident_id in _new_incident_ids:
            try:
                from app.collector.rca import rank_probable_cause
                rank_probable_cause(incident_id)
            except Exception as e:
                logger.warning(f"[correlate] RCA ranking failed for incident {incident_id}: {e}")

        return incidents_created, alerts_attached
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
