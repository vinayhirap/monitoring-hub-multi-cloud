-- db/migrations/038_fix_collation_mismatch.sql
--
-- PRODUCTION BUG FIX (found via live QA, 2026-09-15): every new table
-- created this session (031, 034, 035, 036) was created without an
-- explicit charset/collation, so each defaulted to MySQL's modern
-- default (utf8mb4_0900_ai_ci) -- but every PRE-EXISTING table in this
-- schema (resources, aws_accounts, alerts, resource_relationships,
-- etc.) uses utf8mb4_unicode_ci. The moment a query compares a VARCHAR
-- column on one of the new tables against a VARCHAR column on an old
-- table (e.g. slo_definitions.resource_id vs. resources.resource_id
-- in app/api/slo.py's LEFT JOIN), MySQL raises error 1267 "Illegal mix
-- of collations" -- confirmed in production via GET /api/slo's real
-- traceback, which is what surfaced this.
--
-- This is a schema mistake, not a data problem: it fails even against
-- an EMPTY table, because MySQL validates collation compatibility at
-- query-plan time regardless of row count. Fixing it requires
-- explicitly converting the already-created tables' collation to
-- match the rest of the schema -- CREATE TABLE IF NOT EXISTS in the
-- original migrations can't do this retroactively since the tables
-- already exist.
--
-- Fixed here, proactively, for every new table -- not just
-- slo_definitions (the one that actually crashed), but also
-- maintenance_windows (whose _affected_resource_ids() recursive CTE in
-- app/collector/maintenance.py joins resource_id against the
-- pre-existing resource_relationships table -- the same latent bug,
-- just not yet triggered because it only fires once a maintenance
-- window with dependents to cascade to is actually created),
-- security_findings and synthetic_checks (no confirmed crash yet, but
-- same root cause, same fix, cheaper to fix now than wait for the next
-- surprise 500).
ALTER TABLE synthetic_checks         CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE synthetic_check_results  CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE slo_definitions          CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE security_findings        CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE maintenance_windows      CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE status_page_components   CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
