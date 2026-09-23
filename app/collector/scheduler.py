# app/collector/scheduler.py
"""
Tiered scheduler — Phase 2 implementation.

  critical  — every 2 min  : RDS, ELB
  standard  — every 5 min  : EC2 CPU/Network + EBS + Lambda Errors
  low       — every 15 min : EC2 Disk, EC2 CWAgent mem/disk, Lambda
                              Invocations
  extended  — every 60 min : the extended-tier services minus the
                              always-empty group below

Split further 2026-09-12: a live 24h metric_history audit (AuroGov
Mumbai) found S3, CloudWatch Logs (DeliveryErrors), Backup (job-failure
counters), CloudFront (request-rate metrics), and WAFv2 (BlockedRequests)
returning ZERO datapoints regardless of poll frequency -- these publish
once/day or only on rare events, not on any short fixed interval, so
even the 60-min "extended" tier could only ever return empty for them.
Split onto a new "slow_extended" (24h) tier -- see extended.py's
SLOW_EXTENDED_SERVICES docstring for the full audit data and for why
SQS/Kinesis (also all-zero in that same audit) were deliberately NOT
included here (different root cause: no queue/stream activity at all,
not a publish-rate mismatch -- a slower poll doesn't fix that).

Split out 2026-09-12: extended-tier services previously rode the "low"
(15-min) tier purely because that's where EC2 disk/CWAgent already
lived, not because any of them need 15-min freshness. S3 storage
metrics publish once/day, ACM/KMS/EventBridge/Backup/Route53 barely
change minute to minute -- none of this is latency-sensitive the way
RDS/ALB are. Mirrors the reasoning already applied to Azure/GCP in
multicloud_scheduler.py (core vs extended cadence chosen by real
billing/publication behavior, not copied blindly from AWS's split).
Cuts extended-tier GetMetricData call volume by 4x (96 cycles/day -> 24)
for zero loss of real freshness, since AWS itself isn't publishing
these any faster than that anyway for most of these namespaces.

Note (post metric_audit.md §8/§10 fix, updated 2026-09-10): EC2 CPU/
Network and ELB were previously ALSO re-triggered on the "standard" tier
(dispatch condition was `tier in ("critical","standard")`), which meant
that on every cycle where "standard" happened to fire (every 5 min),
those metrics were requested via GetMetricData TWICE in immediate
succession -- once by "critical"'s own always-running 2-min loop, once
again by "standard". Fixed in app/collector/metrics/runner.py: elb
dispatches on tier == "critical" only (ALB's 1-min publish cadence
justifies the faster tier). EC2 CPU/Network was ALSO moved off critical
entirely, not just deduplicated -- live DEV data (this app's own new
basic-vs-detailed monitoring visibility log) confirmed the real EC2
fleet here is 100% on AWS basic monitoring (5-min publish, free), so
even a single, non-duplicated 2-min poll was still wasting ~60% of its
calls on data that hadn't changed. ec2_critical (name unchanged, task
moved) now dispatches on tier == "standard" only, matching AWS's actual
5-min publication cadence for this fleet -- a deliberate, data-confirmed
choice, not a blind default (a fleet on detailed/1-min monitoring should
NOT make this same move; see _log_monitoring_mode_mismatch()'s docstring
in metrics/runner.py). EBS had the analogous "standard"/"low" duplicate
issue (EBS publishes at 5-min resolution; the 15-min "low" re-poll could
only ever re-return data "standard" had already fetched); EBS is
dispatched on tier == "standard" only.

Fixed (2026-09-10, closing the item this docstring previously flagged as
known-but-unfixed): RDS's dispatch in metrics/runner.py had no tier gate
at all (`elif resource_type == "rds": tasks.append(...)`, unconditional)
-- since "critical" already runs every ~2 min unconditionally, RDS was
re-polled AGAIN, redundantly, in any cycle where "standard" or "low" also
coincided. Same class of bug as the ALB/EBS fixes above, just never
gated to begin with. Now gated to tier == "critical" only -- critical's
own ~2-min cadence already matches RDS's real 1-min publish rate well,
so revenue-critical coverage is unchanged; only the redundant extra
calls on coincident standard/low cycles are gone.

Alerts evaluated after every standard cycle.
Discovery runs every 15 min (aligned with low tier).
Partition management runs daily.

Cost impact (3 accounts, illustrative -- see real DEV numbers below):
  Before Phase 2:        510 metrics x 288 cycles/day = $44/mo
  After Phase 2:          ~220 avg x 240 cycles/day    = ~$15/mo  (66% reduction)
  After dedup fix: removed the ELB "standard"-tier duplicate call (~1 in 3
  cycles) and the EBS "low"-tier duplicate call (~2 in 3 fifteen-minute
  cycles).
  After EC2 tier move (this change): EC2 CPU/Network calls drop from
  720 cycles/day (2-min) to 288 cycles/day (5-min) -- a 60% cut in calls
  for that metric family specifically, with zero freshness loss for a
  basic-monitoring fleet. Real DEV numbers as of 2026-09-10 (1 account,
  "AuroGov Mumbai", confirmed via live discovery logs): 6 running EC2
  instances (100% basic monitoring), 0 RDS, 2 ELB, 3 Lambda, plus a long
  tail of extended-tier resources (45 S3 buckets, 102 CloudWatch Logs
  groups, 15 KMS keys, 11 EventBridge rules, 5 ACM certs, and more) --
  see monitoring-hub-metric-audit.md §3 for the per-metric formula this
  plugs into; exact extended-tier call volume still needs a "low" tier
  log check to count actually-enabled metrics per service.
"""
import time
import logging
import threading
from datetime import datetime
from app.db import get_connection

_stop_event = threading.Event()
logger      = logging.getLogger(__name__)

# ── Intervals (seconds) ───────────────────────────────────────
CRITICAL_INTERVAL  = 120     #  2 min — EC2 CPU, RDS, ELB
STANDARD_INTERVAL  = 300     #  5 min — + EBS, Lambda Errors
LOW_INTERVAL       = 900     # 15 min — EC2 Disk, Lambda Invocations
EXTENDED_INTERVAL  = 3600    # 60 min — extended-tier services except SLOW_EXTENDED_SERVICES
SLOW_EXTENDED_INTERVAL = 86400  # 24 h  — S3/CloudWatch Logs/Backup/CloudFront/WAFv2 (see extended.py)
DISCOVERY_INTERVAL = 900     # 15 min — aligned with low tier


def _get_active_accounts():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT id, account_name, account_id, role_arn, auth_mode,
                   external_id, default_region, provider
            FROM aws_accounts
            WHERE status = 'active'
        """)
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()



# VictoriaMetrics is intentionally stopped (see monitoring-hub-metric-audit.md
# §8 flaw #4). sync_metrics_from_vm() is now REDUNDANT, not just idle: Phase 1's
# GMD revival (apply_direct_gmd_metrics_revival.py, see the run_metrics_collection
# call below) already writes every AWS metric this app tracks straight into the
# same `metrics` table sync_metrics_from_vm() exists to populate FROM VM --
# evaluate_alerts() already has fresh data by the time this function would run.
# Disabled here at the call site only -- app/collector/metrics_vm_sync.py itself
# is untouched, per instruction, pending a later full VM-code cleanup pass once
# the direct-cloud path has been running in production long enough to trust
# fully. Toggle this back to True only if VM is intentionally restarted AND a
# real (not-yet-identified) reason to prefer VM-sourced data over the direct
# GMD data already in `metrics` is found.
_VM_SYNC_ENABLED = False


def run_once(tier="standard"):
    """Single collection + alert cycle for given tier."""
    from app.collector.metrics.runner  import run_metrics_collection
    from app.collector.alert_evaluator import evaluate_alerts
    from app.collector.metrics_writer  import prune_metric_history
    from app.collector.op_log          import log_event, prune_op_events

    accounts = _get_active_accounts()
    if not accounts:
        logger.warning("No active accounts")
        return

    # Re-enabled (see apply_direct_gmd_metrics_revival.py) -- this was
    # disabled, not deleted, during the VM/YACE cost-avoidance migration.
    # AWS billing for GetMetricData applies again; accepted deliberately.
    #
    # Wrapped (previously unwrapped) so a collection failure for this
    # tier is captured as a structured op_event (roadmap phase 5) instead
    # of only a stack trace in server logs -- this is exactly the kind of
    # cycle-level failure the 2026-08-26 RCA had to reconstruct from raw
    # logs after the fact. Re-raising after logging: this must not
    # silently swallow a real collection failure, only make it findable.
    try:
        run_metrics_collection(accounts, tier=tier)
    except Exception as e:
        log_event("collector_cycle_failed", f"run_metrics_collection failed for tier={tier}: {e}",
                   severity="ERROR", detail={"tier": tier, "account_count": len(accounts)})
        raise

    if tier == "low":
        # 30 days, not the function's own 7-day default -- dynamic
        # thresholds (app/collector/baseline.py) need enough history to
        # see weekly seasonality (Monday-morning batch jobs, weekend
        # dips). Bumping retention here, not the function default, keeps
        # any other caller's expectations unchanged.
        prune_metric_history(retain_days=30)
        prune_op_events(retain_days=30)
        try:
            from app.collector.synthetic import prune_synthetic_results
            prune_synthetic_results()
        except Exception as e:
            log_event("synthetic_prune_failed",
                      f"prune_synthetic_results failed (non-fatal): {e}", severity="WARNING")
        try:
            from app.collector.baseline import recompute_baselines
            recompute_baselines()
        except Exception as e:
            log_event("baseline_recompute_failed",
                      f"recompute_baselines failed (non-fatal, static thresholds still apply): {e}",
                      severity="WARNING")

        # AIOps roadmap #9 (2026-09-14): STL seasonal-decomposition
        # upgrade pass -- runs immediately after recompute_baselines()
        # above, reads the sigma-clipped buckets that call just wrote
        # and upgrades whichever ones pass its own safety gates to a
        # tighter STL-derived mean/stddev. Never runs instead of
        # recompute_baselines(), only after -- see
        # app/collector/baseline_stl.py's module docstring for why this
        # ordering makes it impossible for this pass to leave a bucket
        # worse than sigma-clip alone would have.
        try:
            from app.collector.baseline_stl import upgrade_baselines_with_stl
            upgrade_baselines_with_stl()
        except Exception as e:
            log_event("baseline_stl_upgrade_failed",
                      f"upgrade_baselines_with_stl failed (non-fatal, sigma-clipped "
                      f"baselines from recompute_baselines() still apply): {e}",
                      severity="WARNING")

        # Auto-tuning for chronically-miscalibrated static thresholds
        # (2026-09-14) -- runs right after recompute_baselines() above,
        # since it reads the metric_baseline rows that call just wrote.
        # See app/collector/threshold_tuning.py's own docstring for the
        # real production case this fixes (EC2 NetIn/NetOut alerting
        # repeatedly at a threshold below these resources' normal
        # traffic level).
        try:
            from app.collector.threshold_tuning import auto_tune_static_thresholds
            auto_tune_static_thresholds()
        except Exception as e:
            log_event("threshold_tuning_failed",
                      f"auto_tune_static_thresholds failed (non-fatal): {e}", severity="WARNING")

        # AIOps Phase 1 (2026-09-14): real AWS-resource-level RCA data
        # (CloudTrail, NOT this app's own op_events/audit_logs -- see
        # cloudtrail_collector.py's docstring for that distinction),
        # topology-based alert correlation into incidents, and resource
        # health scoring. All three are pure computation/free-tier API
        # calls against data already flowing through this tier -- same
        # cost profile as baseline.py above. Each wrapped independently
        # so one failing doesn't block the others or this tier's other
        # existing work.
        try:
            from app.aws.cloudtrail_collector import poll_cloud_events
            poll_cloud_events()
        except Exception as e:
            log_event("cloudtrail_poll_failed",
                      f"poll_cloud_events failed (non-fatal): {e}", severity="WARNING")

        # AIOps Phase 2 (2026-09-14): cross-metric (multivariate)
        # anomaly detection via IsolationForest -- catches a pattern
        # shift across several metrics at once that no single metric's
        # own threshold/baseline would flag. Writes a real `alerts` row
        # (metric_name='multivariate_anomaly'). Deliberately runs
        # BEFORE correlate_alerts_into_incidents/recompute_health_scores
        # below, so a newly-created anomaly alert is picked up by
        # topology correlation and health scoring in THIS cycle rather
        # than waiting for the next one. Free/local-compute only
        # (scikit-learn + pandas, no cloud API calls) -- see
        # AI_ML_ROADMAP.md Section 7.
        try:
            from app.collector.multivariate_anomaly import detect_multivariate_anomalies
            detect_multivariate_anomalies()
        except Exception as e:
            log_event("multivariate_anomaly_failed",
                      f"detect_multivariate_anomalies failed (non-fatal): {e}", severity="WARNING")
        try:
            from app.collector.correlate import correlate_alerts_into_incidents
            correlate_alerts_into_incidents()
        except Exception as e:
            log_event("incident_correlation_failed",
                      f"correlate_alerts_into_incidents failed (non-fatal): {e}", severity="WARNING")
        try:
            from app.collector.health_score import recompute_health_scores
            recompute_health_scores()
        except Exception as e:
            log_event("health_score_failed",
                      f"recompute_health_scores failed (non-fatal): {e}", severity="WARNING")

        # AIOps roadmap #13/#15 (2026-09-14): background LLM summary
        # cache population -- runs LAST in this tier, after
        # correlate/health_score above, so it reads this cycle's
        # freshest related-alert-count/health context via
        # rca.explain_alert(). Entirely no-op (zero API calls, zero DB
        # writes) unless LLM_SUMMARY_ENABLED=true (and, for the default
        # free local provider, Ollama is actually running --
        # see app/llm/summarizer.py's module
        # docstring. Never blocks a user-facing request either way: the
        # GET /alerts/{id}/explain endpoint only ever reads whatever
        # this job last wrote.
        try:
            from app.collector.llm_summarizer import refresh_llm_summaries
            refresh_llm_summaries()
        except Exception as e:
            log_event("llm_summary_refresh_failed",
                      f"refresh_llm_summaries failed (non-fatal, template summaries "
                      f"from rca.py still apply): {e}", severity="WARNING")

    # Evaluate alerts after every standard cycle
    if tier == "standard":
        if _VM_SYNC_ENABLED:
            from app.collector.metrics_vm_sync import sync_metrics_from_vm
            sync_metrics_from_vm()
        try:
            evaluate_alerts()
        except Exception as e:
            log_event("alert_eval_failed", f"evaluate_alerts failed: {e}", severity="ERROR")
            raise
        # Health scores are a pure function of FIRING alerts; recompute right
        # after evaluation (not only on the 15-min low tier) so a score can
        # never outlive the alert that caused it by up to 15 minutes.
        try:
            from app.collector.health_score import recompute_health_scores
            recompute_health_scores()
        except Exception as e:
            log_event("health_score_failed",
                      f"recompute_health_scores (post-evaluation) failed (non-fatal): {e}", severity="WARNING")
        try:
            from app.collector.escalation import evaluate_escalations
            evaluate_escalations()
        except Exception as e:
            log_event("escalation_eval_failed", f"evaluate_escalations failed (non-fatal): {e}", severity="WARNING")
        

    # AIOps: lite CSPM security checks (2026-09-14) -- run on the
    # "extended" tier (60-min cadence, see run_loop's docstring).
    # Security configuration (public buckets, open security groups,
    # IAM hygiene) changes far less often than metrics, and the
    # underlying IAM/S3 describe calls are broader-scoped than this
    # app's normal CloudWatch/Describe-only permissions -- a slower
    # cadence is kinder to both AWS API rate limits and the extra
    # trust this grants the monitoring role. See app/collector/cspm.py's
    # module docstring for the exact IAM permissions this needs and
    # what happens (a skipped check, not a crash) if they're missing.
    if tier == "extended":
        try:
            from app.collector.cspm import run_security_checks
            run_security_checks()
        except Exception as e:
            log_event("cspm_check_failed",
                      f"run_security_checks failed (non-fatal): {e}", severity="WARNING")

def run_discovery_once():
    from app.collector.discovery.runner import run_discovery
    run_discovery()


def _load_last_run_times() -> dict:
    """
    Seeds run_loop's per-tier 'last ran' timers from persisted history
    instead of always starting every tier at 0 -- see run_loop's own
    2026-09-19 fix note for the incident this closes. Queried ONCE per
    leadership generation (i.e. once per process start or restart, not
    once per cycle) -- this doesn't add any steady-state cost to the
    loop, it only changes what its very first iteration assumes.

    Deliberately reuses op_events (db/migrations/022_op_events_table.sql)
    rather than a new table: it's already indexed on (event_type,
    created_at), already has a 30-day prune job that comfortably outlives
    even the slowest (24h) tier's cadence, and this is exactly the kind
    of "something happened, here's when" fact it already exists to hold.
    Falls back to "run everything now" (the pre-fix behavior) on any
    query failure -- a missed optimization, not a correctness problem,
    so this is intentionally non-fatal.
    """
    tiers = ("standard", "low", "extended", "slow_extended")
    now = time.time()
    result = {t: 0 for t in tiers}
    try:
        conn = get_connection(); cur = conn.cursor(dictionary=True)
        try:
            placeholders = ", ".join(["%s"] * len(tiers))
            cur.execute(f"""
                SELECT event_type, MAX(created_at) AS last_at
                FROM op_events
                WHERE event_type IN ({placeholders})
                GROUP BY event_type
            """, tuple(f"scheduler_tier_{t}_completed" for t in tiers))
            for row in cur.fetchall():
                tier = row["event_type"][len("scheduler_tier_"):-len("_completed")]
                if tier in result and row["last_at"]:
                    # min(now, ...) guards against clock skew between
                    # this process and whatever wrote the row making a
                    # persisted timestamp look like it's in the future,
                    # which would otherwise make the tier look "not due
                    # for a very long time" instead of just "due now".
                    result[tier] = min(now, row["last_at"].timestamp())
        finally:
            cur.close(); conn.close()
    except Exception as e:
        logger.warning(f"[scheduler] couldn't load persisted tier run-times, "
                        f"defaulting to running every tier on this first cycle (non-fatal): {e}")
    return result


def _mark_tier_completed(tier: str) -> None:
    """Companion to _load_last_run_times() -- records that this tier
    just completed, so the NEXT process to call run_loop() (after a
    restart or a leadership handover) knows not to immediately re-run
    it. log_event() already swallows its own DB failures (see
    op_log.py), so this is safe to call unconditionally after a
    successful tier run without needing its own try/except here."""
    from app.collector.op_log import log_event
    log_event(f"scheduler_tier_{tier}_completed", f"{tier} tier completed", severity="INFO")


class _LeadershipLost(Exception):
    """Raised between steps of one run_loop iteration once leader_event
    has been cleared -- see _require_leader()."""


def _require_leader(leader_event):
    """Audit B14: leadership used to be checked only at the TOP of each
    run_loop iteration, but one iteration runs critical + standard + low +
    extended + slow_extended + discovery back to back (many minutes when
    the slower tiers coincide). A worker that lost the lock mid-iteration
    kept going through every remaining tier while the new leader started
    its own -- duplicate billed GetMetricData calls and two concurrent
    evaluate_alerts() runs (the Sep 5 deadlock pattern). Checked before
    every step now, so an ex-leader stops within one step."""
    if leader_event is not None and not leader_event.is_set():
        raise _LeadershipLost()


def run_loop(leader_event=None):
    """
    Tiered loop:
      Every 2 min  → critical tier
      Every 5 min  → standard tier (+ alerts)
      Every 15 min → low tier + discovery + partition check
      Every 60 min → extended tier
      Every 24 h   → slow_extended tier

    leader_event: a threading.Event from app/collector/leader.py, set()
    for as long as this process actually holds the collector-leader lock.
    Checked once per cycle -- if it's ever cleared (lock lost, even
    without this process dying), this loop stops itself instead of
    running forever as an orphaned second scheduler. Optional/None for
    any caller outside the normal leader-elected startup path (e.g.
    tests, the standalone `run()` entry point never reaches here).

    2026-09-19 FIX -- confirmed cost on Dev during a burst of 12
    deliberate `systemctl restart` cycles in under 4 hours (routine
    during active development, not an incident): every restart is a
    brand-new call to this function with last_standard/last_low/
    last_extended/last_slow_extended all starting at 0 below, so every
    tier -- extended included, which is where CSPM's broader-scoped
    IAM/S3 checks run (see run_once's own comment on that) -- fired
    immediately on the very next cycle regardless of how recently it
    had already run under the previous (pre-restart) process. 12
    restarts that day produced 58 extended-tier runs against an
    expected ~24 for clean hourly cadence -- not a leak, not a bug in
    the tier-dispatch logic itself, just this function having no way to
    know "a few minutes ago, in a process that no longer exists,
    extended already ran". _load_last_run_times() now answers exactly
    that question from persisted history, once, right here -- critical
    tier is deliberately NOT covered by this (see its own always-run
    block below): it's the cheapest tier and the one where restart-time
    freshness matters most, so re-running it immediately on every
    restart is correct, not wasteful.
    """
    seed = _load_last_run_times()
    last_standard   = seed["standard"]
    last_low        = seed["low"]
    last_extended   = seed["extended"]
    last_slow_extended = seed["slow_extended"]
    last_discovery  = 0
    cycle           = 0

    logger.info("Tiered scheduler started "
                "(critical=2min, standard=5min, low=15min, extended=60min, slow_extended=24h)")

    while not _stop_event.is_set():
        if leader_event is not None and not leader_event.is_set():
            logger.warning("[scheduler] leadership lost -- stopping this loop "
                            "(another worker is now the leader)")
            return

        try:
            now    = time.time()
            cycle += 1

            _require_leader(leader_event)
            # ── Critical tier (2 min) ─────────────────────────────
            logger.info(f"[Cycle {cycle}] critical tier")
            try:
                run_once("critical")
            except Exception as e:
                logger.error(f"Critical tier error: {e}")

            _require_leader(leader_event)
            # Synthetic/uptime checks -- deliberately its own call, not
            # inside run_once("critical"), since it has nothing to do with
            # cloud-account metric collection (run_once's whole purpose).
            # Only probes checks that are actually due (see synthetic.py's
            # run_due_checks() docstring) -- cheap to call every 2-min tick
            # even when nothing is due yet.
            try:
                from app.collector.synthetic import run_due_checks
                run_due_checks()
            except Exception as e:
                logger.error(f"Synthetic check tier error: {e}")

            _require_leader(leader_event)
            # Maintenance-window silencing sync (2026-09-14) -- see
            # app/collector/maintenance.py's module docstring. Runs every
            # 2-min critical-tier tick so silencing activates/deactivates
            # promptly at a window's exact start/end time.
            try:
                from app.collector.maintenance import sync_maintenance_silencing
                sync_maintenance_silencing()
            except Exception as e:
                logger.error(f"Maintenance-window silencing sync error: {e}")

            _require_leader(leader_event)
            # ── Standard tier (5 min) ─────────────────────────────
            if now - last_standard >= STANDARD_INTERVAL:
                logger.info(f"[Cycle {cycle}] standard tier")
                try:
                    run_once("standard")
                    _mark_tier_completed("standard")
                except Exception as e:
                    logger.error(f"Standard tier error: {e}")
                finally:
                    # Attempted counts as run (audit B14): a tier that keeps
                    # failing (e.g. evaluate_alerts raising AFTER collection
                    # succeeded) used to be retried on every 2-min tick,
                    # repeating its billed GetMetricData calls ~2.5x-30x too
                    # often. It now retries at its normal cadence. Only a
                    # SUCCESS is persisted via _mark_tier_completed().
                    last_standard = now

            _require_leader(leader_event)
            # ── Low tier + discovery (15 min) ─────────────────────
            if now - last_low >= LOW_INTERVAL:
                logger.info(f"[Cycle {cycle}] low tier")
                try:
                    run_once("low")
                    _mark_tier_completed("low")
                except Exception as e:
                    logger.error(f"Low tier error: {e}")
                finally:
                    # Attempted counts as run (audit B14): a tier that keeps
                    # failing (e.g. evaluate_alerts raising AFTER collection
                    # succeeded) used to be retried on every 2-min tick,
                    # repeating its billed GetMetricData calls ~2.5x-30x too
                    # often. It now retries at its normal cadence. Only a
                    # SUCCESS is persisted via _mark_tier_completed().
                    last_low = now

            _require_leader(leader_event)
            # ── Extended tier (60 min) ─────────────────────────────
            if now - last_extended >= EXTENDED_INTERVAL:
                logger.info(f"[Cycle {cycle}] extended tier")
                try:
                    run_once("extended")
                    _mark_tier_completed("extended")
                except Exception as e:
                    logger.error(f"Extended tier error: {e}")
                finally:
                    # Attempted counts as run (audit B14): a tier that keeps
                    # failing (e.g. evaluate_alerts raising AFTER collection
                    # succeeded) used to be retried on every 2-min tick,
                    # repeating its billed GetMetricData calls ~2.5x-30x too
                    # often. It now retries at its normal cadence. Only a
                    # SUCCESS is persisted via _mark_tier_completed().
                    last_extended = now

            _require_leader(leader_event)
            # ── Slow-extended tier (24 h) ───────────────────────────
            if now - last_slow_extended >= SLOW_EXTENDED_INTERVAL:
                logger.info(f"[Cycle {cycle}] slow_extended tier")
                try:
                    run_once("slow_extended")
                    _mark_tier_completed("slow_extended")
                except Exception as e:
                    logger.error(f"Slow-extended tier error: {e}")
                finally:
                    # Attempted counts as run (audit B14): a tier that keeps
                    # failing (e.g. evaluate_alerts raising AFTER collection
                    # succeeded) used to be retried on every 2-min tick,
                    # repeating its billed GetMetricData calls ~2.5x-30x too
                    # often. It now retries at its normal cadence. Only a
                    # SUCCESS is persisted via _mark_tier_completed().
                    last_slow_extended = now

                # Daily Ollama model refresh (2026-09-15) -- re-pulls
                # whatever model OLLAMA_MODEL names, picking up any weight
                # update the publisher has pushed to that SAME tag. Never
                # switches to a different model automatically -- see
                # app/llm/summarizer.py's refresh_ollama_model() docstring
                # for why a full auto-upgrade policy is a real production
                # risk this app intentionally doesn't take.
                try:
                    from app.llm.summarizer import refresh_ollama_model
                    refresh_ollama_model()
                except Exception as e:
                    logger.error(f"Ollama model refresh error (non-fatal): {e}")

            _require_leader(leader_event)
            if now - last_discovery >= DISCOVERY_INTERVAL:
                logger.info(f"[Cycle {cycle}] discovery")
                try:
                    run_discovery_once()
                    last_discovery = now
                except Exception as e:
                    logger.error(f"Discovery error: {e}")

        except _LeadershipLost:
            logger.warning("[scheduler] leadership lost mid-cycle -- stopping this loop "
                            "(another worker is now the leader)")
            return

        # Sleep until next critical cycle
        elapsed = time.time() - now
        sleep   = max(0, CRITICAL_INTERVAL - elapsed)
        logger.info(f"[Cycle {cycle}] done in {elapsed:.1f}s — next in {sleep:.0f}s")
        _stop_event.wait(timeout=sleep)


# ── Standalone entry ──────────────────────────────────────────

def run():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger.info("Single collection cycle (standard)...")
    run_discovery_once()
    run_once("standard")
    logger.info("Done.")


if __name__ == "__main__":
    run()