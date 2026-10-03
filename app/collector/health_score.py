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
import random
import time
from app.db import get_connection
from app import alert_rules

logger = logging.getLogger(__name__)

CRITICAL_PENALTY = 40
WARNING_PENALTY = 15
MAX_ALERT_PENALTY = 80
BLAST_RADIUS_PENALTY_PER_DEPENDENT = 1
MAX_BLAST_RADIUS_PENALTY = 20


# MySQL 1213 = deadlock victim (InnoDB already rolled the transaction back),
# 1205 = lock wait timeout. Both are safe to retry from scratch: the whole
# recompute is one transaction and a pure function of the alerts table.
_RETRYABLE_ERRNOS = {1213, 1205}
_MAX_ATTEMPTS = 4


def _is_retryable(exc: Exception) -> bool:
    return getattr(exc, "errno", None) in _RETRYABLE_ERRNOS


def recompute_health_scores() -> int:
    """
    Recomputes every currently-breaching resource's health score and
    upserts into resource_health; deletes rows for resources that have
    recovered (no active alerts left). Returns the number of resources
    scored this run.

    Audit A2: this used to surface "1213 Deadlock found" as a failed run
    (stale scores until the next cycle). It now retries the whole
    transaction with jittered backoff on 1213/1205 and only raises if all
    attempts fail.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return _recompute_health_scores_once()
        except Exception as exc:
            if not _is_retryable(exc) or attempt == _MAX_ATTEMPTS:
                raise
            delay = 0.2 * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
            logger.warning(
                f"[health_score] errno {getattr(exc, 'errno', '?')} on attempt "
                f"{attempt}/{_MAX_ATTEMPTS}; retrying in {delay:.2f}s"
            )
            time.sleep(delay)
    return 0  # unreachable; keeps type-checkers quiet


def _recompute_health_scores_once() -> int:
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
        # Deterministic key order: every writer takes row locks in the same
        # sequence, which removes the lock-order inversion behind 1213.
        breaching = sorted(
            cursor.fetchall(),
            key=lambda r: (r["aws_account_id"], r["resource_id"]),
        )

        scored = 0
        for row in breaching:
            alert_penalty = min(
                MAX_ALERT_PENALTY,
                row["critical_count"] * CRITICAL_PENALTY + row["warning_count"] * WARNING_PENALTY,
            )

            cursor.execute("""
                SELECT COUNT(DISTINCT target_resource_id) AS fan_out
                FROM resource_relationships
                WHERE aws_account_id = %s AND source_resource_id = %s
            """, (row["aws_account_id"], row["resource_id"]))
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
        #
        # Done as plain SELECT + DELETE-by-primary-key instead of a
        # DELETE ... LEFT JOIN over alerts/resources/aws_accounts: a
        # multi-table DELETE takes shared locks on every joined table, which
        # contended with the alert evaluator's writes (audit A2 deadlock).
        # `breaching` is already the firing set (same predicates), so a
        # health row is stale exactly when its key is not in it.
        firing_keys = {(r["aws_account_id"], r["resource_id"]) for r in breaching}
        cursor.execute("SELECT aws_account_id, resource_id FROM resource_health")
        stale = sorted(
            (r["aws_account_id"], r["resource_id"])
            for r in cursor.fetchall()
            if (r["aws_account_id"], r["resource_id"]) not in firing_keys
        )
        for acct_id, res_id in stale:
            cursor.execute(
                "DELETE FROM resource_health WHERE aws_account_id = %s AND resource_id = %s",
                (acct_id, res_id),
            )

        conn.commit()
        logger.info(f"[health_score] scored {scored} breaching resource(s)")
        return scored
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
