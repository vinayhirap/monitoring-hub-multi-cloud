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

from app.db import get_connection
from app.llm.summarizer import is_enabled, polish_summary, source_hash

logger = logging.getLogger(__name__)

_DEFAULT_BATCH_LIMIT = 50


def refresh_llm_summaries() -> int:
    """Returns the number of alerts whose llm_summary cache was
    (re)written this cycle. Safe to call every "low" tier cycle --
    cheap no-op when disabled or when nothing's stale."""
    if not is_enabled():
        return 0

    batch_limit = int(os.getenv("LLM_SUMMARY_BATCH_LIMIT", _DEFAULT_BATCH_LIMIT))

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

        # Local import to avoid a circular import at module load time
        # (rca.py doesn't import this module, but keeping the edge
        # one-directional here is cheap insurance either way).
        from app.collector.rca import explain_alert

        for row in candidates:
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

                # If polish_summary fell back to the deterministic text
                # unchanged (disabled mid-run, transient API failure),
                # still cache it under the current hash -- correct,
                # non-stale content either way, and avoids retrying an
                # unreachable API on every single cycle until it comes
                # back.
                cursor.execute("""
                    UPDATE alerts
                    SET llm_summary = %s,
                        llm_summary_source_hash = %s,
                        llm_summary_generated_at = NOW()
                    WHERE id = %s
                """, (polished, current_hash, alert_id))
                refreshed += cursor.rowcount

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
