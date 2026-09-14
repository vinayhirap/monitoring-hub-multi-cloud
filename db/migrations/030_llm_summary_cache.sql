-- db/migrations/030_llm_summary_cache.sql
--
-- AIOps roadmap #13/#15 (LLM-polished incident/alert summaries,
-- 2026-09-14). Adds a CACHE for the LLM-polished version of
-- app/collector/rca.py's existing deterministic explain_alert()
-- summary -- the deterministic template summary itself is UNCHANGED
-- and remains the guaranteed fallback (see app/llm/summarizer.py's
-- module docstring for the "never invents facts, never blocks, never
-- raises" contract).
--
-- WHY A CACHE COLUMN, NOT AN ON-DEMAND CALL FROM THE GET ENDPOINT:
-- app/api/alerts.py's GET /{alert_id}/explain is explicitly documented
-- as having "no side effect ... safe to cache/refetch freely". Calling
-- a hosted LLM API synchronously from inside that handler would both
-- violate that invariant (network call + latency on every page load)
-- and add an availability dependency (a slow/down LLM API would slow
-- down or break a page that works fine without it today). Instead,
-- app/collector/llm_summarizer.py generates and writes this cache in
-- the SAME background "low" tier scheduler cycle as
-- correlate_alerts_into_incidents/recompute_health_scores -- the GET
-- endpoint only ever READS this column, same as every other value it
-- already returns.
--
-- llm_summary_source_hash lets the cache self-invalidate: it's a
-- sha256 of the deterministic template summary at generation time. If
-- rca.py's own signal-gathering later produces a DIFFERENT
-- deterministic summary for this alert (new CloudTrail event found,
-- trend context changed, etc.), the hash no longer matches and
-- explain_alert() falls back to the fresh deterministic text until the
-- next background cycle catches up -- never serves a stale LLM
-- paragraph describing facts that no longer hold.
ALTER TABLE alerts
  ADD COLUMN llm_summary             TEXT         NULL AFTER status,
  ADD COLUMN llm_summary_source_hash CHAR(64)     NULL AFTER llm_summary,
  ADD COLUMN llm_summary_generated_at TIMESTAMP   NULL AFTER llm_summary_source_hash;
