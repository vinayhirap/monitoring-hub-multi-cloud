-- db/migrations/070_fix_rbac_v2_collation_and_reports_v2_seed.sql
--
-- Two independent, low-risk fixes found auditing 040-049 (RBAC v2 /
-- permission catalog v2 / scoping / reports).
--
-- ─────────────────────────────────────────────────────────────────────
-- 1. COLLATION DRIFT in 040_rbac_v2_bindings.sql
-- ─────────────────────────────────────────────────────────────────────
-- 039_fix_collation_direction.sql standardized every table in this
-- schema on utf8mb4_0900_ai_ci (confirmed against db_schema_only.sql,
-- which uses utf8mb4_0900_ai_ci uniformly). 040 does this correctly
-- for `roles`, `role_permissions_v2` and `rbac_service_catalog`, but
-- `rbac_scopes`, `role_bindings`, `permission_overrides` and
-- `access_reviews` were created with utf8mb4_unicode_ci instead --
-- inconsistent with the rest of the schema, including the OTHER
-- tables in the very same migration file.
--
-- No query in the current codebase joins a string/ENUM column across
-- this collation boundary (every join between these tables is on a
-- BIGINT id), so this is not causing a live "Illegal mix of
-- collations" error today. Left as-is, it is a landmine for the next
-- query that does -- e.g. any future comparison involving
-- rbac_scopes.cloud or role_bindings.principal_type alongside a
-- 0900_ai_ci column. Cheap to align now while these tables are new
-- and (for a fresh 040-049 deploy) hold little or no data.
ALTER TABLE rbac_scopes         CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
ALTER TABLE role_bindings       CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
ALTER TABLE permission_overrides CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
ALTER TABLE access_reviews      CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

-- ─────────────────────────────────────────────────────────────────────
-- 2. role_permissions_v2 never seeded for the reports.* codes (047)
-- ─────────────────────────────────────────────────────────────────────
-- 041 and 049 both seed the new permission codes into role_permissions
-- (legacy) AND role_permissions_v2 (040's scoped-binding resolver).
-- 047_reports_engine.sql seeded only role_permissions for
-- reports.view/generate/download/email -- role_permissions_v2 has no
-- rows for them at all.
--
-- No live route currently uses require_permission_v2 (the resolver in
-- app/auth/rbac.py is not yet wired to any router -- confirmed by grep
-- across app/api), so this has caused no incorrect behaviour so far.
-- But the moment reports.py (or anything else checking these codes)
-- migrates to the v2 resolver, a user resolved purely from a v2
-- role_binding -- including an admin with an explicit v2 binding,
-- since 040's one-time "admin gets every existing permission" backfill
-- ran before these codes existed and is not re-triggered by later
-- INSERTs into `permissions` -- would be incorrectly denied
-- reports.view/generate/download/email. Fixing the seed now, same
-- shape 041/049 already used, rather than waiting for that migration
-- to happen and silently break reports access for v2-bound users.
INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'viewer' AND p.code IN ('reports.view');

INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'editor' AND p.code IN
  ('reports.view', 'reports.generate', 'reports.download', 'reports.email');

-- admin: every code that exists, same blanket grant 040/041/049 already
-- use -- also closes the same gap for any OTHER permission code added
-- after 040 that a later migration forgot to mirror into
-- role_permissions_v2 (harmless re-INSERT IGNORE for codes already
-- covered).
INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p WHERE r.role_key = 'admin';
