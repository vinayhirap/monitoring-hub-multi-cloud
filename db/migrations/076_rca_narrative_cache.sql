-- db/migrations/076_rca_narrative_cache.sql
--
-- AI/ML audit Phase 1 (2026-10-02): cache for the LLM-written RCA report narrative.
--
-- WHY: GET /api/alerts/{id}/rca-report used to call Ollama synchronously inside the web
-- request, uncached. Measured on PROD (t3.large, CPU credits exhausted): llama3.2:3b runs
-- ~3.5 tokens/s, so the ~250-token narrative needs 60-90 s -- at or beyond the 60 s
-- LLM_SUMMARY_TIMEOUT_SECONDS, so reports silently fell back to the template text.
-- app/llm/rca_report.py now generates the narrative on a background thread, waits only
-- briefly for it, and stores the result here; the next download is served instantly.
--
-- facts_hash is a sha256 of the report's facts: if anything the narrative describes changes
-- (new current_value, resolved, new trigger...) the hash no longer matches and the stale
-- narrative is never served. Only LLM output is cached; the deterministic template is
-- always recomputed. No foreign key on purpose (same reasoning as migration
-- fresh_schema_migrations_fk_type_fix: alerts.id type differs between installs); the code
-- tolerates this table not existing yet, so code and migration can deploy in either order.
-- Idempotent.

CREATE TABLE IF NOT EXISTS rca_narratives (
    alert_id           BIGINT UNSIGNED NOT NULL,
    facts_hash         CHAR(64)        NOT NULL,
    narrative_markdown TEXT            NOT NULL,
    generated_at       TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (alert_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
