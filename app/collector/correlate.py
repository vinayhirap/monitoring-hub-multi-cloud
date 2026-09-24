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
from app import alert_rules

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
        # "Loose" = active, user-visible, and not already a member of ANY
        # active incident. The old LEFT JOIN form returned an alert once
        # per incident_alerts row, so an alert in both a resolved and an
        # active incident still looked loose and could seed a second,
        # duplicate active incident. Hidden internal metrics
        # (multivariate_anomaly) never seed user-facing incidents.
        cursor.execute(f"""
            SELECT a.id, a.resource_id, a.severity, a.triggered_at AS created_at,
                   a.aws_account_id
            FROM alerts a
            WHERE a.status = 'active'
              AND a.aws_account_id IS NOT NULL
              AND {alert_rules.base_where("a")}
              AND NOT EXISTS (
                  SELECT 1 FROM incident_alerts ia
                  JOIN incidents i ON i.id = ia.incident_id AND i.status = 'active'
                  WHERE ia.alert_id = a.id
              )
        """)
        loose_alerts = cursor.fetchall()

        for alert in loose_alerts:
            # 1. Can this alert join an EXISTING open incident?
            # Tenant isolation (audit b08): every join is pinned to the
            # alert's own account. resource_ids are not unique across
            # accounts (GCP instance names, RDS identifiers, IAM names),
            # so matching on resource_id alone merged another account's
            # alerts into this account's incident -- visible to anyone
            # who can see this account's incidents.
            cursor.execute("""
                SELECT DISTINCT i.id
                FROM incidents i
                JOIN incident_alerts ia ON ia.incident_id = i.id
                JOIN alerts a2 ON a2.id = ia.alert_id AND a2.aws_account_id = %s
                JOIN resource_relationships rel
                    ON rel.aws_account_id = %s
                   AND ((rel.source_resource_id = a2.resource_id AND rel.target_resource_id = %s)
                     OR (rel.target_resource_id = a2.resource_id AND rel.source_resource_id = %s))
                WHERE i.status = 'active'
                  AND i.aws_account_id = %s
                  AND ABS(TIMESTAMPDIFF(MINUTE, i.started_at, %s)) <= %s
                LIMIT 1
            """, (alert["aws_account_id"], alert["aws_account_id"],
                  alert["resource_id"], alert["resource_id"], alert["aws_account_id"],
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
                        severity = IF(UPPER(%s) = 'CRITICAL', 'CRITICAL', severity)
                    WHERE id = %s
                """, (alert["severity"], incident_id))
                continue

            # 2. No open incident to join -- does this alert have a
            #    topologically-connected, also-loose active alert to
            #    seed a NEW incident with?
            cursor.execute(f"""
                SELECT a2.id AS other_alert_id, a2.severity AS other_severity
                FROM alerts a2
                LEFT JOIN incident_alerts ia2 ON ia2.alert_id = a2.id
                JOIN resource_relationships rel
                    ON rel.aws_account_id = a2.aws_account_id
                   AND ((rel.source_resource_id = a2.resource_id AND rel.target_resource_id = %s)
                     OR (rel.target_resource_id = a2.resource_id AND rel.source_resource_id = %s))
                WHERE a2.status = 'active'
                  AND {alert_rules.base_where("a2")}
                  AND a2.aws_account_id = %s
                  AND a2.id != %s
                  AND ia2.alert_id IS NULL
                  AND ABS(TIMESTAMPDIFF(MINUTE, a2.triggered_at, %s)) <= %s
                LIMIT 1
            """, (alert["resource_id"], alert["resource_id"], alert["aws_account_id"], alert["id"],
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
                f"Correlated breach on {alert['resource_id']} and related resource(s)"[:255],
                # Worst of the two seeding alerts (was: the first alert's
                # severity only, so a CRITICAL partner made a WARNING incident).
                "CRITICAL" if "CRITICAL" in (str(alert["severity"]).upper(),
                                             str(partner.get("other_severity") or "").upper())
                else alert["severity"],
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
        # 'acknowledged' is still OPEN (a human took ownership, lifecycle
        # migration 051) -- counting only 'active' resolved an incident the
        # moment its members were acked, while the alerts were still live.
        cursor.execute("""
            UPDATE incidents i
            SET status = 'resolved', resolved_at = NOW()
            WHERE i.status = 'active'
              AND NOT EXISTS (
                  SELECT 1 FROM incident_alerts ia
                  JOIN alerts a ON a.id = ia.alert_id
                  WHERE ia.incident_id = i.id AND a.status IN ('active', 'acknowledged')
              )
        """)

        conn.commit()
        if incidents_created or alerts_attached:
            logger.info(f"[correlate] {incidents_created} incident(s) created, "
                        f"{alerts_attached} alert(s) attached this cycle")

    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    # Rank a probable root cause for every newly-created incident so
    # the incidents list already has one to show, not just on first
    # detail-view open. Runs AFTER this function's connection is
    # released (rank_probable_cause opens its own) -- previously two
    # pooled connections were held per new incident -- and outside the
    # correlation transaction, so an RCA-ranking failure for one incident
    # can't roll back the correlation work that already succeeded.
    for incident_id in _new_incident_ids:
        try:
            from app.collector.rca import rank_probable_cause
            rank_probable_cause(incident_id)
        except Exception as e:
            logger.warning(f"[correlate] RCA ranking failed for incident {incident_id}: {e}")

    return incidents_created, alerts_attached
