-- db/migrations/039_fix_collation_direction.sql
--
-- CORRECTS migration 038, which fixed the collation mismatch in the
-- WRONG DIRECTION (found via the person's own diagnostic query against
-- production, 2026-09-15 -- static inference from the error message
-- text alone was not enough; this needed an actual
-- information_schema.columns check, which is exactly what surfaced
-- the mistake).
--
-- 038's reasoning assumed the pre-existing schema uses
-- utf8mb4_unicode_ci (inferred from which collation name appeared on
-- which side of the MySQL 1267 error text) and converted the new
-- tables to match that. The person's direct query proved the opposite:
--     alerts.resource_id     -> utf8mb4_0900_ai_ci
--     resources.resource_id  -> utf8mb4_0900_ai_ci
--     slo_definitions.resource_id (after 038) -> utf8mb4_unicode_ci
-- i.e. the REAL pre-existing convention in this schema is
-- utf8mb4_0900_ai_ci, and 038 moved the new tables further away from
-- it, not closer -- the query still failed afterward for exactly the
-- same reason, just with the two collation names swapped in the error
-- text.
--
-- This migration converts the same six tables 038 touched to the
-- CORRECT target, utf8mb4_0900_ai_ci, matching resources/alerts (the
-- two tables actually confirmed by direct query, and the two every new
-- table's JOINs need to match against).
ALTER TABLE synthetic_checks         CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
ALTER TABLE synthetic_check_results  CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
ALTER TABLE slo_definitions          CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
ALTER TABLE security_findings        CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
ALTER TABLE maintenance_windows      CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
ALTER TABLE status_page_components   CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
