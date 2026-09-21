# app/collector/threshold_tuning.py
"""
Auto-tuning for chronically-miscalibrated static thresholds
(2026-09-14, three revisions same day, each from a live production
diagnosis).

REVISION 3 (this version) -- WARNING-ONLY CHRONIC ALERTS WERE INVISIBLE:
diagnosed live after Revisions 1+2 correctly did NOT switch
Aurionpro-Finops's NetworkOut threshold, yet its `Net Out` alert had
been open continuously since 2026-09-13 (well past CHRONIC_ALERT_AGE_HOURS).
Root cause: this account's `critical_value` had separately been raised
to 5,000,000 (to quiet CRITICAL-level noise), but `warning_value` was
never touched and stayed at 1,000,000. The resource's baseline mean
(~1.78M, 1625 confident samples) sits comfortably UNDER the 5M critical
line -- so majority/chronic-mean/chronic-noise, which ALL compared only
against critical_value, correctly saw nothing wrong -- while the same
alert kept re-triggering at WARNING severity every single evaluation
cycle against the untouched 1M line, forever, with no automatic path
that would ever catch it.

FIX: every breach comparison in this module (majority/chronic-mean/
chronic-noise) now checks against `warning_value`, not `critical_value`.
This is a strict broadening, not a behavior change for existing cases:
warning_value is always the closer-to-normal, first-crossed line (see
alert_evaluator.py's _dynamic_bounds, which already assumes this -- its
dynamic warning band is a *tighter* 0.66*k fraction of the same
critical-side k*stddev), so anything that used to qualify by crossing
critical_value still qualifies now (crossing critical implies crossing
warning first). The only NEW resources this can affect are ones like
Aurionpro-Finops's NetworkOut -- chronically breaching WARNING while
staying under CRITICAL -- which is exactly the gap this revision closes.
The existing confidence gates (MIN_CONFIDENT_SAMPLES, CHRONIC_BREACH_FRACTION,
CHRONIC_ALERT_AGE_HOURS, MIN_RESOURCES_FOR_DECISION) are all unchanged --
broadening WHICH line counts as "breaching" doesn't loosen how much
evidence is required before switching.

REVISION 2 -- FLAPPING: diagnosed live in production
after Revision 1 correctly did NOT switch Aurionpro-Finops's
NetworkOut threshold. That resource's baseline showed a TYPICAL value
of 1.78M against a 5M critical line (genuinely healthy on average, with
1553 confident samples) -- yet it had an alert stuck "active" for 30+
straight hours. The resource isn't chronically over the line; it's
FLAPPING: noisy enough that it keeps spiking above 5M and dropping back
down, and alert_evaluator.py's hysteresis (which requires enough
consecutive HEALTHY readings before resolving) never gets a long enough
clean streak to actually clear the alert. Revision 1's chronic-single-
resource path only compared the raw MEAN to the critical line, so it
correctly left this alone -- but "correctly left alone" here still
meant a 30-hour-and-counting stuck alert with no real per-mean
miscalibration to blame it on.

FIX (NOISE_CROSSES_LINE path): a resource's own baseline
STDDEV, not just its mean, is now also checked. If mean + k*stddev
(the same upper edge dynamic mode would actually compute --
k = the threshold's own dynamic_k, defaulting to NOISE_K) reaches past
the (as of Revision 3) warning line even though the mean itself doesn't,
that is real evidence this resource's normal variability legitimately
brushes the static line -- exactly the flapping pattern. Dynamic mode's
per-hour-of-day, per-day-of-week band (see baseline.py) handles this far
better than one flat 24/7 number: it widens or narrows to match each
time slot's actual observed noise, instead of treating every hour as
equally noisy.

REVISION 1 -- MAJORITY-ONLY WAS TOO NARROW: the original version only
switched a threshold when a MAJORITY (CHRONIC_BREACH_FRACTION, 60%) of
an account's resources of that type were confidently over the static
line. In production, only 2 of ~19 EC2 instances ran genuinely high
traffic, so the 60% bar was never met and those two alerts stayed
active for over a week. Revision 1 added the CHRONIC-SINGLE-RESOURCE
path: one confidently-mean-breaching resource with a real, sustained
(6h+) active alert is sufficient on its own -- switching a threshold to
dynamic never hurts the OTHER resources under it, since each gets its
own personalized band regardless.

REAL PROBLEM (all three revisions address different facets of this): a
static threshold (thresholds.warning_value/critical_value) is TWO
numbers shared by every resource of a given type+metric in an account.
Either number can be wrong for a resource in three distinct ways: (a)
the resource's typical level has simply outgrown it (Revision 1's
case), (b) the resource is naturally noisy/bursty and the static line
sits inside that normal noise band rather than above it (Revision 2's
case), or (c) only ONE of the two configured lines was ever corrected,
leaving the other to keep firing forever even though the resource is
statistically fine relative to it (Revision 3's case). All three
produce the same symptom -- an alert that won't stop firing/
re-triggering with no genuine incident behind it -- but need different
detection logic.

FIX: this app already has a statistically-grounded, per-RESOURCE
alternative -- sigma-clipped baselines + confidence-blended dynamic
thresholds (app/collector/baseline.py, alert_evaluator.py's
_dynamic_bounds). The only reason it isn't already protecting these
alerts is that the threshold row's use_dynamic flag is off. This module
switches a STATIC threshold row to dynamic (use_dynamic=1) when ANY of:

  1. MAJORITY PATH: a confident majority (CHRONIC_BREACH_FRACTION) of
     the resources it governs have a baseline MEAN already past the
     warning line.
  2. MANUALLY-CONFIRMED PATH (2026-09-14): a human has
     directly marked MIN_FALSE_POSITIVE_MARKS+ of a resource's past
     alerts on this exact metric as false positives (see
     PATCH /alerts/{id}/false-positive in app/api/alerts.py) -- the
     strongest, fastest evidence of the four; doesn't need
     CHRONIC_ALERT_AGE_HOURS or even a current mean/noise breach.
  3. CHRONIC-MEAN PATH: at least one resource has a confident baseline
     MEAN past the warning line AND a currently-active alert on that
     exact metric that has been continuously breaching for at least
     CHRONIC_ALERT_AGE_HOURS.
  4. CHRONIC-NOISE PATH: at least one resource has a confident
     baseline whose MEAN + k*STDDEV crosses the warning line (even
     though the mean alone doesn't) AND a currently-active alert on
     that exact metric that has been continuously breaching for at
     least CHRONIC_ALERT_AGE_HOURS -- the flapping case.

None of these paths can silently hide a real problem: dynamic mode
still alerts on genuine deviation from a resource's own normal
(mean +/- k*stddev) -- it just stops treating "this resource's normal
traffic level, including its normal noise" as a permanent alert
condition. A resource of the same type whose baseline genuinely is low
AND stable gets a correspondingly tight dynamic band and keeps alerting
on its own real anomalies exactly as before.

TRANSPARENCY: every change is written to audit_logs (actor
"system:threshold_tuning") AND logged as an op_event, including which
of the four paths fired -- nothing happens silently.

CADENCE: "low" tier (15 min, see scheduler.py), same slot as
baseline.py -- deliberately recomputed each cycle, since a threshold
that's fine today can drift into being chronically wrong (by mean,
noise, or a half-updated warning/critical pair) months from now, and
should self-correct continuously.
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
# majority path -- the single-resource paths below are unaffected by
# this and can still fire with just one confidently-baselined resource.
MIN_RESOURCES_FOR_DECISION = 2

# CHRONIC-MEAN / CHRONIC-NOISE PATHS: an active alert on the exact same
# metric that has been continuously breaching for at least this long is
# real, sustained evidence -- not a fresh breach still worth waiting out
# to see if it resolves on its own.
CHRONIC_ALERT_AGE_HOURS = 6

# CHRONIC-NOISE PATH: sigma multiplier used to test whether a
# resource's own normal VARIABILITY (not just its mean) would already
# cross the warning line -- i.e. whether dynamic mode's own band
# (mean +/- k*stddev) reaches past the static number even though the
# mean alone doesn't. Matches alert_evaluator.py's own default dynamic_k
# for any threshold that hasn't set a custom one, so this asks exactly
# the question "would switching to dynamic actually change anything for
# this resource" using the same math dynamic mode itself would use.
NOISE_K = 3.0

# MANUALLY-CONFIRMED PATH: how many of a resource+metric's PAST alerts
# (see db/migrations/027_alert_false_positive_marking.sql) must have
# been directly marked "not genuine" by a human via
# PATCH /alerts/{id}/false-positive before that alone is sufficient
# evidence to switch the threshold -- no need to wait for
# CHRONIC_ALERT_AGE_HOURS, and no need for the resource to even be
# statistically breaching by mean or noise right now. A person
# confirming an alert wasn't real, twice, is stronger and faster
# evidence than either automatic statistical path alone.
MIN_FALSE_POSITIVE_MARKS = 2


def _has_chronic_active_alert(cursor, aws_account_id, resource_id, metric_name):
    """True if there's a currently-active alert on this exact
    resource+metric that has been breaching continuously for at least
    CHRONIC_ALERT_AGE_HOURS -- real evidence, not just a confident
    baseline in isolation."""
    cursor.execute("""
        SELECT id FROM alerts
        WHERE aws_account_id = %s AND resource_id = %s AND metric_name = %s
          AND status IN ('active', 'acknowledged')
          AND triggered_at <= DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s HOUR)
        LIMIT 1
    """, (aws_account_id, resource_id, metric_name, CHRONIC_ALERT_AGE_HOURS))
    return cursor.fetchone() is not None


def _noise_band_crosses_line(resource, th):
    """True if this resource's own normal variability (mean +/-
    k*stddev) reaches past the warning line even though its mean
    alone doesn't -- the flapping signature: a metric that's healthy on
    average but noisy enough to keep brushing a static line placed
    inside its normal range rather than above it.

    Checks warning_value, not critical_value (Revision 3, 2026-09-14):
    warning is always the closer/first-crossed line, so this is a
    strict broadening -- anything whose noise band used to cross
    critical still crosses warning too."""
    k = th.get("dynamic_k") or NOISE_K
    stddev = resource.get("typical_stddev") or 0
    if th["comparison"] in (">", ">="):
        return (resource["typical_value"] + k * stddev) > th["warning_value"]
    return (resource["typical_value"] - k * stddev) < th["warning_value"]


def _false_positive_mark_count(cursor, aws_account_id, resource_id, metric_name):
    """How many of this resource+metric's alerts, ever, have been
    manually marked false positive by a human (any status -- past
    resolved ones count too, not just the current active one, since
    the marking is about whether the THRESHOLD is wrong, not whether
    any one specific alert instance is still open)."""
    cursor.execute("""
        SELECT COUNT(*) AS cnt FROM alerts
        WHERE aws_account_id = %s AND resource_id = %s AND metric_name = %s
          AND marked_false_positive = 1
    """, (aws_account_id, resource_id, metric_name))
    row = cursor.fetchone()
    return row["cnt"] if row else 0


def count_likely_flapping_alerts(aws_account_ids=None) -> int:
    """
    One efficient bulk query (not N per-resource lookups) counting
    currently-active alerts whose resource is genuinely flapping --
    same definition as this module's own chronic-noise path and
    app/collector/rca.py's _check_flapping(): the resource's baseline
    MEAN is healthy, but mean +/- k*stddev already crosses the STATIC
    critical line. Used by app/api/incidents.py's fleet-summary
    endpoint to surface a fleet-wide "how much of what's currently
    alerting is probably just noise, not a genuine issue" count --
    a number that should trend toward zero over time as
    auto_tune_static_thresholds() converts these thresholds to dynamic.

    Does NOT require the CHRONIC_ALERT_AGE_HOURS bar
    _has_chronic_active_alert() checks -- that gate exists in
    auto_tune_static_thresholds() to avoid switching a threshold on a
    single brand-new alert that might resolve on its own in minutes.
    This count is a read-only fleet-health signal, not a threshold-
    mutating decision, so a fresher, more responsive count (any
    currently-active alert matching the pattern) is more useful here
    than an artificially delayed one.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        where_clause = ""
        params = []
        if aws_account_ids is not None:
            if not aws_account_ids:
                return 0
            placeholders = ",".join(["%s"] * len(aws_account_ids))
            where_clause = f" AND a.aws_account_id IN ({placeholders})"
            params = list(aws_account_ids)

        from app import alert_rules
        cursor.execute(f"""
            SELECT COUNT(*) AS flapping_count
            FROM alerts a
            JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
            JOIN aws_accounts acc ON acc.id = a.aws_account_id AND acc.status = 'active'
            JOIN thresholds t ON t.aws_account_id = a.aws_account_id
                              AND t.resource_type = r.resource_type AND t.use_dynamic = 0
                              AND NOT (t.warning_value = 1000000 AND t.critical_value = 5000000 AND t.comparison = '>')
            JOIN metric_catalog mc ON mc.id = t.metric_id AND mc.metric_name = a.metric_name
            JOIN (
                SELECT aws_account_id, resource_id, metric_name,
                       AVG(mean_value) AS typical_value, AVG(stddev_value) AS typical_stddev,
                       SUM(sample_count) AS total_samples
                FROM metric_baseline
                GROUP BY aws_account_id, resource_id, metric_name
            ) b ON b.aws_account_id = a.aws_account_id
               AND b.resource_id = a.resource_id AND b.metric_name = a.metric_name
            WHERE {alert_rules.firing_where()} AND b.total_samples >= %s{where_clause}
              AND (
                  (t.comparison IN ('>', '>=')
                    AND b.typical_value <= t.warning_value
                    AND (b.typical_value + COALESCE(t.dynamic_k, %s) * b.typical_stddev) > t.warning_value)
                  OR
                  (t.comparison NOT IN ('>', '>=')
                    AND b.typical_value >= t.warning_value
                    AND (b.typical_value - COALESCE(t.dynamic_k, %s) * b.typical_stddev) < t.warning_value)
              )
        """, [MIN_CONFIDENT_SAMPLES] + params + [NOISE_K, NOISE_K])
        row = cursor.fetchone()
        return row["flapping_count"] or 0
    finally:
        cursor.close()
        conn.close()


def auto_tune_static_thresholds() -> int:
    """
    Scans every STATIC (use_dynamic=0), ENABLED threshold row and
    switches it to use_dynamic=1 when the majority path, the chronic-
    mean path, or the chronic-noise path (see module docstring)
    justifies it. Records why via audit_logs + op_events, including
    which path fired. Returns the number of threshold rows switched.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    switched = 0
    try:
        cursor.execute("""
            SELECT t.id, t.aws_account_id, t.resource_type, t.metric_id,
                   t.warning_value, t.critical_value, t.comparison, t.dynamic_k,
                   mc.metric_name, a.account_name
            FROM thresholds t
            JOIN metric_catalog mc ON mc.id = t.metric_id
            LEFT JOIN aws_accounts a ON a.id = t.aws_account_id
            WHERE t.enabled = 1 AND t.use_dynamic = 0
              -- placeholder volume defaults are already anomaly-only in the
              -- evaluator (threshold_defaults.PLACEHOLDER_THRESHOLD)
              AND NOT (t.warning_value = 1000000 AND t.critical_value = 5000000 AND t.comparison = '>')
        """)
        static_thresholds = cursor.fetchall()

        for th in static_thresholds:
            cursor.execute("""
                SELECT b.resource_id, AVG(b.mean_value) AS typical_value,
                       AVG(b.stddev_value) AS typical_stddev,
                       SUM(b.sample_count) AS total_samples
                FROM metric_baseline b
                JOIN resources r ON r.resource_id = b.resource_id AND r.aws_account_id = b.aws_account_id
                WHERE b.aws_account_id = %s AND r.resource_type = %s
                  AND b.metric_name = %s
                GROUP BY b.resource_id
                HAVING total_samples >= %s
            """, (th["aws_account_id"], th["resource_type"], th["metric_name"], MIN_CONFIDENT_SAMPLES))
            resource_baselines = cursor.fetchall()

            if not resource_baselines:
                continue

            # Compares against warning_value, not critical_value
            # (Revision 3, 2026-09-14): warning is always the closer/
            # first-crossed line, so this is a strict broadening -- a
            # resource whose mean crosses critical necessarily crosses
            # warning too, so nothing that qualified before stops
            # qualifying now. It also catches the case that motivated
            # this revision: chronically breaching warning while
            # staying under critical (real production example:
            # Aurionpro-Finops's NetworkOut, critical raised to 5M but
            # warning left at 1M -- see module docstring).
            if th["comparison"] in (">", ">="):
                mean_breaching = [r for r in resource_baselines if r["typical_value"] > th["warning_value"]]
            else:
                mean_breaching = [r for r in resource_baselines if r["typical_value"] < th["warning_value"]]

            fraction = len(mean_breaching) / len(resource_baselines)
            majority_path = (len(resource_baselines) >= MIN_RESOURCES_FOR_DECISION
                              and fraction >= CHRONIC_BREACH_FRACTION)

            chronic_example = None
            chronic_path = None
            if not majority_path:
                for candidate in resource_baselines:
                    if _false_positive_mark_count(cursor, th["aws_account_id"], candidate["resource_id"], th["metric_name"]) >= MIN_FALSE_POSITIVE_MARKS:
                        chronic_example, chronic_path = candidate, "manually_confirmed"
                        break
                if chronic_example is None:
                    for candidate in mean_breaching:
                        if _has_chronic_active_alert(cursor, th["aws_account_id"], candidate["resource_id"], th["metric_name"]):
                            chronic_example, chronic_path = candidate, "chronic_mean"
                            break
                if chronic_example is None:
                    noisy_candidates = [r for r in resource_baselines
                                         if r not in mean_breaching and _noise_band_crosses_line(r, th)]
                    for candidate in noisy_candidates:
                        if _has_chronic_active_alert(cursor, th["aws_account_id"], candidate["resource_id"], th["metric_name"]):
                            chronic_example, chronic_path = candidate, "chronic_noise"
                            break

            if not majority_path and chronic_example is None:
                continue

            cursor.execute("UPDATE thresholds SET use_dynamic = 1 WHERE id = %s", (th["id"],))

            example = chronic_example or mean_breaching[0]
            if majority_path:
                trigger_path, trigger_desc = "majority", (
                    f"{len(mean_breaching)}/{len(resource_baselines)} resources have a normal operating "
                    f"range past the configured warning value"
                )
            elif chronic_path == "manually_confirmed":
                trigger_desc = (
                    f"a person has directly marked {MIN_FALSE_POSITIVE_MARKS}+ past alerts on "
                    f"{example['resource_id']} for this metric as false positives"
                )
                trigger_path = "manually_confirmed"
            elif chronic_path == "chronic_mean":
                trigger_desc = (
                    f"{example['resource_id']} alone has been continuously alerting on this metric "
                    f"for {CHRONIC_ALERT_AGE_HOURS}+ hours with a confidently-baselined normal range "
                    f"past the configured warning value (no majority of peer resources needed)"
                )
                trigger_path = "chronic_mean"
            else:
                trigger_desc = (
                    f"{example['resource_id']}'s average is healthy, but it has been continuously "
                    f"alerting on this metric for {CHRONIC_ALERT_AGE_HOURS}+ hours -- its normal "
                    f"variability alone already crosses the configured warning value"
                )
                trigger_path = "chronic_noise"
            account_label = th["account_name"] or f"account {th['aws_account_id']}"
            note = (
                f"Auto-switched {th['metric_name']} threshold for {th['resource_type']} "
                f"({account_label}) from static to dynamic: {trigger_desc} "
                f"of {th['warning_value']} (critical is {th['critical_value']}; e.g. "
                f"{example['resource_id']} typically runs around "
                f"{round(example['typical_value'], 1)}). This was producing repeated alerts with no "
                f"genuine cause -- switched to a per-resource dynamic band based on each resource's "
                f"own history."
            )

            try:
                from app.audit import write_audit
                write_audit(
                    actor="system:threshold_tuning",
                    action="auto_enable_dynamic_threshold",
                    payload={
                        "detail": note,
                        "threshold_id": th["id"], "metric_name": th["metric_name"],
                        "resource_type": th["resource_type"], "aws_account_id": th["aws_account_id"],
                        "trigger_path": trigger_path,
                        "breaching_resources": len(mean_breaching), "total_resources": len(resource_baselines),
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
