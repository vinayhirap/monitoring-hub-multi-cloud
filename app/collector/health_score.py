# app/collector/health_score.py
"""
Resource health score -- AIOps roadmap Phase 1 (2026-09-14).

A single 0-100 number per resource, computed purely from data this app
already collects -- no new cloud API calls, no ML model needed for
this version: active-alert severity/count, and topology blast radius
(how many other resources depend on it) as a multiplier on existing
badness, not a standalone penalty.

100 = fully healthy (no row in resource_health at all -- see the
DELETE at the end of recompute_health_scores() for why absence, not a
stored 100, represents "healthy"). Deductions:
  - CRITICAL active alert:  -40 each
  - WARNING active alert:   -15 each
  - both capped at MAX_ALERT_PENALTY combined
  - topology blast radius:  -1 per downstream dependent, capped at
    MAX_BLAST_RADIUS_PENALTY -- only applied ON TOP of an existing
    alert penalty (fan-out alone isn't unhealthy; it's a severity
    multiplier for a resource that's already breaching).

Deliberately simple, explainable arithmetic, not a black-box model --
matches this app's existing preference for logic an operator can
audit line-by-line. See AI_ML_ROADMAP.md Phase 2 for where a learned
version could replace this once there's enough real incident history
(from the new `incidents` table) to validate one against.
"""
import logging
from app.db import get_connection
from app import alert_rules

logger = logging.getLogger(__name__)

CRITICAL_PENALTY = 40
WARNING_PENALTY = 15
MAX_ALERT_PENALTY = 80
BLAST_RADIUS_PENALTY_PER_DEPENDENT = 1
MAX_BLAST_RADIUS_PENALTY = 20


def recompute_health_scores() -> int:
    """
    Recomputes every currently-breaching resource's health score and
    upserts into resource_health; deletes rows for resources that have
    recovered (no active alerts left). Returns the number of resources
    scored this run.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Only genuinely FIRING alerts lower a score (alert_rules.py): stale,
        # acknowledged, muted and maintenance-silenced alerts don't, and
        # hidden internal metrics (multivariate_anomaly) don't either -- a
        # score must be explainable by alerts the user can actually see.
        cursor.execute(f"""
            SELECT r.resource_id, r.aws_account_id,
                   COALESCE(SUM(CASE WHEN UPPER(a.severity) = 'CRITICAL' THEN 1 ELSE 0 END), 0) AS critical_count,
                   COALESCE(SUM(CASE WHEN UPPER(a.severity) = 'WARNING'  THEN 1 ELSE 0 END), 0) AS warning_count
            FROM resources r
            JOIN aws_accounts acc ON acc.id = r.aws_account_id AND acc.status = 'active'
            JOIN alerts a ON a.aws_account_id = r.aws_account_id
                         AND a.resource_id = r.resource_id
            WHERE {alert_rules.firing_where()} AND {alert_rules.base_where()}
            GROUP BY r.resource_id, r.aws_account_id
            HAVING critical_count + warning_count > 0
        """)
        breaching = cursor.fetchall()

        scored = 0
        for row in breaching:
            alert_penalty = min(
                MAX_ALERT_PENALTY,
                row["critical_count"] * CRITICAL_PENALTY + row["warning_count"] * WARNING_PENALTY,
            )

            cursor.execute("""
                SELECT COUNT(DISTINCT target_resource_id) AS fan_out
                FROM resource_relationships
                WHERE source_resource_id = %s
            """, (row["resource_id"],))
            fan_out = cursor.fetchone()["fan_out"] or 0
            blast_penalty = min(MAX_BLAST_RADIUS_PENALTY, fan_out * BLAST_RADIUS_PENALTY_PER_DEPENDENT)

            score = max(0, 100 - alert_penalty - blast_penalty)

            cursor.execute("""
                INSERT INTO resource_health
                    (resource_id, aws_account_id, health_score, score_reason)
                VALUES (%s, %s, %s, JSON_OBJECT(
                    'critical_alerts', %s, 'warning_alerts', %s,
                    'alert_penalty', %s, 'blast_radius_fan_out', %s, 'blast_penalty', %s
                ))
                ON DUPLICATE KEY UPDATE
                    health_score   = VALUES(health_score),
                    score_reason   = VALUES(score_reason),
                    aws_account_id = VALUES(aws_account_id)
            """, (
                row["resource_id"], row["aws_account_id"], score,
                row["critical_count"], row["warning_count"],
                alert_penalty, fan_out, blast_penalty,
            ))
            scored += 1

        # Resources that recovered (no active alerts left): remove
        # their row entirely -- "no row" is read by the API as fully
        # healthy (100), so deleting correctly reflects recovery rather
        # than leaving a stale low score behind.
        cursor.execute(f"""
            DELETE rh FROM resource_health rh
            LEFT JOIN (
                SELECT DISTINCT a.aws_account_id, a.resource_id
                FROM alerts a
                JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
                JOIN aws_accounts acc ON acc.id = a.aws_account_id AND acc.status = 'active'
                WHERE {alert_rules.firing_where()} AND {alert_rules.base_where()}
                  AND UPPER(a.severity) IN ('CRITICAL', 'WARNING')
            ) f ON f.aws_account_id = rh.aws_account_id AND f.resource_id = rh.resource_id
            WHERE f.resource_id IS NULL
        """)

        conn.commit()
        logger.info(f"[health_score] scored {scored} breaching resource(s)")
        return scored
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
