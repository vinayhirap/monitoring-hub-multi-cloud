# app/collector/threshold_tuning.py
"""
Auto-tuning for chronically-miscalibrated static thresholds
(2026-09-14) -- fixes the exact pattern seen in production: EC2 NetIn/
NetOut alerts firing repeatedly on Aurionpro-Dev-Finops /
Aurionpro-Finops at a threshold of 1,000,000, while their actual normal
traffic runs 1.3M-4.3M -- every evaluation cycle re-breaches the same
static number forever, not because anything is wrong, just because the
threshold was never right for these resources' real traffic level.
explain_alert() on these already correctly reports LOW/MEDIUM
confidence and "no related AWS activity... found" every time, because
there genuinely is no incident to find -- just a stale static number.

REAL PROBLEM: a static threshold (thresholds.warning_value/
critical_value) is ONE number shared by every resource of a given
type+metric in an account. If a resource's genuinely normal operating
range has grown past it (or it was set as a generic default that was
never right for this workload), that resource alerts on every single
cycle, forever, with zero real anomaly behind it.

FIX: this app already has a statistically-grounded, per-RESOURCE
alternative -- sigma-clipped baselines + confidence-blended dynamic
thresholds (app/collector/baseline.py, alert_evaluator.py's
_dynamic_bounds). The only reason it isn't already protecting these
alerts is that the threshold row's use_dynamic flag is off. This module
detects when a STATIC threshold is chronically, confidently exceeded by
a resource's own normal (sigma-clipped) baseline -- its typical
operating range, not an occasional spike -- and switches that threshold
row to dynamic mode automatically, so every resource of that type gets
a band computed from ITS OWN history going forward, instead of one
shared number that's wrong for at least some of them.

This can only make evaluation MORE accurate, never silently hide a real
problem: dynamic mode still alerts on genuine deviation from a
resource's own normal (mean +/- k*stddev) -- it just stops treating
"this resource's normal traffic level" as a permanent alert condition.
A resource of the same type whose baseline genuinely is low gets a
correspondingly tight dynamic band and keeps alerting on its own real
anomalies exactly as before.

TRANSPARENCY: every change is written to audit_logs (actor
"system:threshold_tuning") AND logged as an op_event -- nothing happens
silently. An admin can see exactly which threshold changed, when, and
why, in the existing Audit Log / Operational Events views.

CADENCE: "low" tier (15 min, see scheduler.py), same slot as
baseline.py -- deliberately recomputed each cycle rather than a
one-time migration, since a threshold that's fine today can drift into
being chronically wrong months from now as traffic grows, and should
self-correct the same way, continuously.
"""
import logging
from app.db import get_connection

logger = logging.getLogger(__name__)

# A static threshold is only auto-switched once there's real
# statistical confidence behind the decision -- same bar
# alert_evaluator.py's own confidence-blending uses (CONFIDENT_SAMPLES)
# for the reverse situation (trusting a dynamic band over a static one).
MIN_CONFIDENT_SAMPLES = 20

# Of a threshold row's resources with a confident baseline, at least
# this fraction must have their TYPICAL (baseline mean) reading already
# past the critical line for the threshold to be flagged as chronically
# miscalibrated. A single unusually loud resource among many normal
# ones should not flip the whole threshold row to dynamic -- this
# requires the static number to be wrong for MOST of what it governs,
# not an outlier case dynamic mode would have caught anyway.
CHRONIC_BREACH_FRACTION = 0.6

# Below this many resources with a confident baseline, there isn't
# enough of a population to call something "most resources," so no
# decision is made either way this cycle.
MIN_RESOURCES_FOR_DECISION = 2


def auto_tune_static_thresholds() -> int:
    """
    Scans every STATIC (use_dynamic=0), ENABLED threshold row. For each,
    checks whether a confident majority of the resources it governs
    have a baseline (mean) already past the threshold's own critical
    line -- i.e. this is those resources' normal operating range, not
    an anomaly. If so, switches that threshold row to use_dynamic=1 and
    records why. Returns the number of threshold rows switched this run.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    switched = 0
    try:
        cursor.execute("""
            SELECT t.id, t.aws_account_id, t.resource_type, t.metric_id,
                   t.warning_value, t.critical_value, t.comparison,
                   mc.metric_name
            FROM thresholds t
            JOIN metric_catalog mc ON mc.id = t.metric_id
            WHERE t.enabled = 1 AND t.use_dynamic = 0
        """)
        static_thresholds = cursor.fetchall()

        for th in static_thresholds:
            cursor.execute("""
                SELECT b.resource_id, AVG(b.mean_value) AS typical_value,
                       SUM(b.sample_count) AS total_samples
                FROM metric_baseline b
                JOIN resources r ON r.resource_id = b.resource_id
                WHERE r.aws_account_id = %s AND r.resource_type = %s
                  AND b.metric_name = %s
                GROUP BY b.resource_id
                HAVING total_samples >= %s
            """, (th["aws_account_id"], th["resource_type"], th["metric_name"], MIN_CONFIDENT_SAMPLES))
            resource_baselines = cursor.fetchall()

            if len(resource_baselines) < MIN_RESOURCES_FOR_DECISION:
                continue

            if th["comparison"] in (">", ">="):
                breaching = [r for r in resource_baselines if r["typical_value"] > th["critical_value"]]
            else:
                breaching = [r for r in resource_baselines if r["typical_value"] < th["critical_value"]]

            fraction = len(breaching) / len(resource_baselines)
            if fraction < CHRONIC_BREACH_FRACTION:
                continue

            cursor.execute("UPDATE thresholds SET use_dynamic = 1 WHERE id = %s", (th["id"],))

            example = breaching[0]
            note = (
                f"Auto-switched {th['metric_name']} threshold for {th['resource_type']} "
                f"(account {th['aws_account_id']}) from static to dynamic: "
                f"{len(breaching)}/{len(resource_baselines)} resources have a normal operating "
                f"range past the configured critical value of {th['critical_value']} "
                f"(e.g. {example['resource_id']} typically runs around {round(example['typical_value'], 1)}). "
                f"This was producing repeated alerts with no genuine cause -- switched to a "
                f"per-resource dynamic band based on each resource's own history."
            )

            try:
                from app.audit import write_audit
                write_audit(
                    actor="system:threshold_tuning",
                    action="auto_enable_dynamic_threshold",
                    detail=note,
                    payload={
                        "threshold_id": th["id"], "metric_name": th["metric_name"],
                        "resource_type": th["resource_type"], "aws_account_id": th["aws_account_id"],
                        "breaching_resources": len(breaching), "total_resources": len(resource_baselines),
                    },
                )
            except Exception as e:
                logger.warning(f"[threshold_tuning] audit write failed (non-fatal): {e}")

            try:
                from app.collector.op_log import log_event
                log_event("threshold_auto_tuned", note, severity="INFO",
                          account_id=th["aws_account_id"], detail={"threshold_id": th["id"]})
            except Exception as e:
                logger.warning(f"[threshold_tuning] op_event write failed (non-fatal): {e}")

            logger.info(f"[threshold_tuning] {note}")
            switched += 1

        conn.commit()
        return switched
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
