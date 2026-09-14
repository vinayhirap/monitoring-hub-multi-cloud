# app/collector/threshold_tuning.py
"""
Auto-tuning for chronically-miscalibrated static thresholds
(2026-09-14, revised same day after production diagnosis) -- fixes the
exact pattern seen in production: EC2 NetIn/NetOut alerts firing
repeatedly on Aurionpro-Dev-Finops / Aurionpro-Finops at a threshold of
1,000,000, while their actual normal traffic runs 1.3M-4.3M -- every
evaluation cycle re-breaches the same static number forever, not
because anything is wrong, just because the threshold was never right
for these resources' real traffic level. explain_alert() on these
already correctly reports LOW/MEDIUM confidence and "no related AWS
activity... found" every time, because there genuinely is no incident
to find -- just a stale static number.

REVISION HISTORY: the original version of this module only switched a
threshold to dynamic when a MAJORITY (CHRONIC_BREACH_FRACTION, 60%) of
an account's resources of that type were confidently over the static
line. In production, that majority bar never triggered for the exact
case this module was built to fix: only 2 of ~19 EC2 instances in the
account ran genuinely high traffic (Aurionpro-Dev-Finops,
Aurionpro-Finops) while the other 17 were normal -- so the 60% bar was
never met, and those two alerts stayed active and re-triggering for
over a WEEK (one since 2026-09-04) despite this module being live.
Diagnosed directly from the alerts table: both had a confidently-
baselined, chronically-over-critical typical value the whole time, but
were a minority, not a majority, of their resource_type's population.

The majority requirement was unnecessarily conservative: switching a
threshold row to dynamic never hurts the OTHER resources under it --
each one gets its own personalized band from its own baseline either
way, so a normal, quiet resource is completely unaffected by a loud
neighbor's threshold going dynamic. There was no real reason to make a
single chronic offender wait for its peers to also become loud before
getting fixed. See CHRONIC_ALERT_AGE_HOURS below for the new,
additional path that catches exactly this case.

REAL PROBLEM: a static threshold (thresholds.warning_value/
critical_value) is ONE number shared by every resource of a given
type+metric in an account. If a resource's genuinely normal operating
range has grown past it (or it was set as a generic default that was
never right for this workload), that resource alerts on every single
cycle, forever, with zero real anomaly behind it -- regardless of how
many (or how few) OTHER resources of the same type share that problem.

FIX: this app already has a statistically-grounded, per-RESOURCE
alternative -- sigma-clipped baselines + confidence-blended dynamic
thresholds (app/collector/baseline.py, alert_evaluator.py's
_dynamic_bounds). The only reason it isn't already protecting these
alerts is that the threshold row's use_dynamic flag is off. This module
switches a STATIC threshold row to dynamic (use_dynamic=1) when EITHER:

  1. MAJORITY PATH (unchanged): a confident majority
     (CHRONIC_BREACH_FRACTION) of the resources it governs have a
     baseline already past the critical line -- the static number is
     wrong for most of what it governs.
  2. CHRONIC-SINGLE-RESOURCE PATH (NEW): at least one resource has BOTH
     a confident baseline past the critical line AND a currently-active
     alert on that exact metric that has been continuously breaching
     for at least CHRONIC_ALERT_AGE_HOURS -- real, sustained evidence
     this is that resource's normal operating range, not a fresh
     incident still being investigated. Doesn't need any of its peers
     to also be loud.

This can only make evaluation MORE accurate, never silently hide a real
problem: dynamic mode still alerts on genuine deviation from a
resource's own normal (mean +/- k*stddev) -- it just stops treating
"this resource's normal traffic level" as a permanent alert condition.
A resource of the same type whose baseline genuinely is low gets a
correspondingly tight dynamic band and keeps alerting on its own real
anomalies exactly as before.

TRANSPARENCY: every change is written to audit_logs (actor
"system:threshold_tuning") AND logged as an op_event -- nothing happens
silently. An admin can see exactly which threshold changed, when, why,
and which trigger path fired, in the existing Audit Log / Operational
Events views.

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

# MAJORITY PATH: of a threshold row's resources with a confident
# baseline, at least this fraction must have their TYPICAL (baseline
# mean) reading already past the critical line.
CHRONIC_BREACH_FRACTION = 0.6

# Below this many resources with a confident baseline, there isn't
# enough of a population to call something "most resources" for the
# majority path -- the chronic-single-resource path below is unaffected
# by this and can still fire with just one confidently-baselined
# resource.
MIN_RESOURCES_FOR_DECISION = 2

# CHRONIC-SINGLE-RESOURCE PATH: an active alert on the exact same
# metric that has been continuously breaching for at least this long is
# real, sustained evidence -- not a fresh breach still worth waiting out
# to see if it resolves on its own. 6 hours is long enough to rule out
# "temporary real spike, will clear naturally soon" while still being
# far short of the week-plus this was actually left unfixed for in
# production before this path existed.
CHRONIC_ALERT_AGE_HOURS = 6


def _has_chronic_active_alert(cursor, resource_id, metric_name):
    """True if there's a currently-active alert on this exact
    resource+metric that has been breaching continuously for at least
    CHRONIC_ALERT_AGE_HOURS -- real evidence, not just a confident
    baseline in isolation (a resource could have a high baseline mean
    without necessarily having an alert active on it right now, e.g. if
    the static threshold sits just above its baseline most of the time
    and only occasionally, briefly crosses it -- that case should NOT
    auto-switch on the single-resource path; only a resource actually,
    currently, continuously stuck alerting qualifies)."""
    cursor.execute("""
        SELECT id FROM alerts
        WHERE resource_id = %s AND metric_name = %s AND status = 'active'
          AND triggered_at <= DATE_SUB(NOW(), INTERVAL %s HOUR)
        LIMIT 1
    """, (resource_id, metric_name, CHRONIC_ALERT_AGE_HOURS))
    return cursor.fetchone() is not None


def auto_tune_static_thresholds() -> int:
    """
    Scans every STATIC (use_dynamic=0), ENABLED threshold row and
    switches it to use_dynamic=1 when either the majority path or the
    chronic-single-resource path (see module docstring) justifies it.
    Records why via audit_logs + op_events. Returns the number of
    threshold rows switched this run.
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

            if not resource_baselines:
                continue

            if th["comparison"] in (">", ">="):
                breaching = [r for r in resource_baselines if r["typical_value"] > th["critical_value"]]
            else:
                breaching = [r for r in resource_baselines if r["typical_value"] < th["critical_value"]]

            if not breaching:
                continue

            fraction = len(breaching) / len(resource_baselines)
            majority_path = (len(resource_baselines) >= MIN_RESOURCES_FOR_DECISION
                              and fraction >= CHRONIC_BREACH_FRACTION)

            chronic_example = None
            if not majority_path:
                for candidate in breaching:
                    if _has_chronic_active_alert(cursor, candidate["resource_id"], th["metric_name"]):
                        chronic_example = candidate
                        break

            if not majority_path and chronic_example is None:
                continue

            cursor.execute("UPDATE thresholds SET use_dynamic = 1 WHERE id = %s", (th["id"],))

            example = chronic_example or breaching[0]
            if majority_path:
                trigger_desc = (
                    f"{len(breaching)}/{len(resource_baselines)} resources have a normal operating "
                    f"range past the configured critical value"
                )
            else:
                trigger_desc = (
                    f"{example['resource_id']} alone has been continuously alerting on this metric "
                    f"for {CHRONIC_ALERT_AGE_HOURS}+ hours with a confidently-baselined normal range "
                    f"past the configured critical value (no majority of peer resources needed)"
                )
            note = (
                f"Auto-switched {th['metric_name']} threshold for {th['resource_type']} "
                f"(account {th['aws_account_id']}) from static to dynamic: {trigger_desc} "
                f"of {th['critical_value']} (e.g. {example['resource_id']} typically runs around "
                f"{round(example['typical_value'], 1)}). This was producing repeated alerts with no "
                f"genuine cause -- switched to a per-resource dynamic band based on each resource's "
                f"own history."
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
                        "trigger_path": "majority" if majority_path else "chronic_single_resource",
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
