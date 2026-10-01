# app/collector/llm_summarizer.py
"""
AIOps roadmap #13/#15 -- background LLM summary cache population
(2026-09-14). Runs in scheduler.py's "low" tier, AFTER
correlate_alerts_into_incidents/recompute_health_scores (same place
rca.py's own signals -- related-alert count, health context -- are
already fresh for this cycle). Writes app/llm/summarizer.py's polished
paragraph into alerts.llm_summary (migration 030) for currently-active
alerts, so app/api/alerts.py's GET /{alert_id}/explain can serve it
with a plain read -- no LLM API call ever happens inside a user-facing
request. See app/llm/summarizer.py's module docstring for the
never-invents-facts / graceful-fallback contract this relies on.

COST CONTROL: entirely no-op (zero DB reads, zero API calls) unless
LLM_SUMMARY_ENABLED=true (and, if LLM_PROVIDER=ollama, a running local
Ollama server -- see
is_enabled()). Even when enabled, only regenerates a summary when its
underlying deterministic facts actually CHANGED since the last cycle
(hash comparison -- see migration 030's docstring), and caps how many
alerts get a fresh LLM call in any single cycle via
LLM_SUMMARY_BATCH_LIMIT, so a fleet-wide backlog can't produce an
unbounded number of API calls (and therefore unbounded cost) in one
run -- it catches up gradually over successive cycles instead.
"""
import logging
import os
import time

from app.db import get_connection
from app.llm.summarizer import is_enabled, polish_summary, source_hash

logger = logging.getLogger(__name__)

_DEFAULT_BATCH_LIMIT = 50
_DEFAULT_BUDGET_SECONDS = 90   # total wall-clock cap per cycle
_MAX_CONSECUTIVE_FALLBACKS = 2  # LLM down/slow -> stop the batch early


def refresh_llm_summaries() -> int:
    """Returns the number of alerts whose llm_summary cache was
    (re)written this cycle. Safe to call every "low" tier cycle --
    cheap no-op when disabled or when nothing's stale."""
    if not is_enabled():
        return 0

    batch_limit = int(os.getenv("LLM_SUMMARY_BATCH_LIMIT", _DEFAULT_BATCH_LIMIT))

    budget = float(os.getenv("LLM_SUMMARY_BUDGET_SECONDS", _DEFAULT_BUDGET_SECONDS))
    started = time.monotonic()
    fallbacks = 0

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    refreshed = 0
    try:
        # Only active alerts -- a resolved alert's explanation is no
        # longer customer-actionable, not worth spending an API call
        # polishing. LIMIT bounds this cycle's worst-case API spend.
        #
        # 2026-09-23 FIX: this ordered by `created_at`, a column
        # alerts has never had -- the real column is `triggered_at`
        # (confirmed via DESCRIBE alerts on both Dev and Prod). Since
        # this whole SELECT lived inside refresh_llm_summaries()'s own
        # try/except, the 1054 "Unknown column" error was caught the
        # same way any other failure here would be, logged as
        # [llm_summary_refresh_failed], and swallowed non-fatally --
        # meaning this query never once succeeded since
        # LLM_SUMMARY_ENABLED was first turned on: EVERY active alert
        # stayed on its plain rca.py template forever, with zero rows
        # ever selected, on every single "low" tier cycle, for as long
        # as the feature had been enabled. Not a timing issue, not a
        # cold-start issue -- a plain wrong column name, one word,
        # never previously run against a real `alerts` table before
        # today. See tests/test_llm_summarizer_refresh.py for the
        # regression test (confirmed it fails loudly on a revert back
        # to `created_at` before writing this fix).
        cursor.execute("""
            SELECT id, llm_summary_source_hash
            FROM alerts
            WHERE status = 'active' AND metric_name != 'multivariate_anomaly'
            ORDER BY triggered_at DESC
            LIMIT %s
        """, (batch_limit,))
        candidates = cursor.fetchall()
        # Phase 1 AI/ML audit (2026-10-02): end the read snapshot now. The loop
        # below can run for the whole budget (each LLM call takes 15-60s on this
        # hardware) -- nothing should stay open or locked while we wait on Ollama.
        conn.commit()

        # Local import to avoid a circular import at module load time
        # (rca.py doesn't import this module, but keeping the edge
        # one-directional here is cheap insurance either way).
        from app.collector.rca import explain_alert

        for row in candidates:
            if time.monotonic() - started >= budget:
                logger.warning(f"[llm_summarizer] {budget:.0f}s budget reached, "
                               f"deferring remaining alerts to next cycle")
                break
            if fallbacks >= _MAX_CONSECUTIVE_FALLBACKS:
                logger.warning("[llm_summarizer] LLM unresponsive "
                               f"({fallbacks} fallbacks in a row), ending batch early")
                break
            alert_id = row["id"]
            try:
                explanation = explain_alert(alert_id)
                if not explanation:
                    continue

                deterministic_summary = explanation["template_summary"]
                current_hash = source_hash(deterministic_summary)
                if current_hash == row["llm_summary_source_hash"]:
                    continue  # facts unchanged since last cycle -- cache still valid

                facts = {
                    "confidence": explanation.get("confidence"),
                    "trend": explanation.get("trend"),
                    "is_likely_flapping": explanation.get("is_likely_flapping"),
                    "probable_trigger": explanation.get("probable_trigger"),
                    "related_alert_count": explanation.get("related_alert_count"),
                }
                polished = polish_summary(facts, deterministic_summary)
                is_fallback = (polished == deterministic_summary)
                fallbacks = fallbacks + 1 if is_fallback else 0

                # 2026-09-29 FIX -- confirmed live on Prod, not
                # hypothetical: alerts 5614/5615 both had rca.py's own
                # verbatim "flat_then_breach" template sentence cached
                # as their llm_summary after a timeout, word for word,
                # under a real hash and a real llm_summary_generated_at
                # -- indistinguishable from a genuine success by every
                # signal this table stored. The old code here wrote
                # current_hash unconditionally (see the comment this
                # replaced), so the next cycle's skip check
                # (current_hash == stored hash, a few lines up) matched
                # immediately -- a single transient timeout got treated
                # as "already polished, don't touch" for as long as the
                # alert's facts stayed the same, never self-correcting.
                #
                # Leaving llm_summary_source_hash NULL on a fallback
                # fixes that: current_hash is always a real 64-char hex
                # string, which can never equal NULL, so the skip check
                # never fires for this row -- it's retried next cycle
                # instead of potentially never again. llm_summary and
                # llm_summary_generated_at are still written either way
                # -- harmless, correct content for the UI meanwhile
                # (the same plain template it would show with the
                # feature off entirely).
                #
                # This is a narrower fix than it would have been before
                # _MAX_CONSECUTIVE_FALLBACKS/budget existed above: those
                # already bound a sustained-outage cycle's worst-case
                # cost, so retrying a fallback row every cycle (rather
                # than backing off further) doesn't reintroduce the
                # "hammer a dead API forever" risk this comment used to
                # warn about.
                cursor.execute("""
                    UPDATE alerts
                    SET llm_summary = %s,
                        llm_summary_source_hash = %s,
                        llm_summary_generated_at = NOW()
                    WHERE id = %s
                """, (polished, None if is_fallback else current_hash, alert_id))
                refreshed += cursor.rowcount
                # Commit per alert, not once after the loop: the UPDATE above
                # holds a row lock on `alerts` until commit, and with the LLM
                # taking up to ~60s per alert a single end-of-loop commit kept
                # earlier alerts' rows locked for the rest of the batch (one
                # health-score 1205 lock-wait on PROD 2026-09-30 fits this shape).
                conn.commit()

            except Exception:
                logger.exception(
                    f"[llm_summarizer] failed to refresh summary for alert {alert_id}, "
                    f"leaving its existing cache (or lack of one) unchanged"
                )
                continue

        conn.commit()
        logger.info(f"[llm_summarizer] refreshed {refreshed} alert summary(ies)")
        return refreshed
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
