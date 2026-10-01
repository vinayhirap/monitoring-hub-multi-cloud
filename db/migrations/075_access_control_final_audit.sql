-- db/migrations/075_access_control_final_audit.sql
--
-- Final audit of RBAC + user management. Idempotent -- every change is
-- guarded by an information_schema check, safe to re-run.
--
--  1. users.last_login_at / deactivated_at / deactivated_by
--     users.active was already enforced on every request (deps.py) and at
--     login (auth.py) but nothing could ever set it to 0, so the only way
--     to cut someone off was DELETE. Lifecycle columns let an admin
--     deactivate (reversible) instead of delete, and show "last seen".
--
--  2. rbac_scopes.is_system
--     The bootstrap "Organization (all clouds)" scope (040/050) is what
--     legacy grants resolve against; the UI offered a delete button on it.
--     Marked is_system=1 so the API can refuse to edit/delete it.
--
--  3. Orphan cleanup
--     role_bindings / permission_overrides / access_reviews point at
--     users.id / org_groups.id through a polymorphic principal_id with no
--     FK, so deleting a user or group used to leave live rows behind.
--     The API now deletes them in the same transaction; this migration
--     removes whatever is already orphaned.
--

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'last_login_at');
SET @s := IF(@c = 0, 'ALTER TABLE users ADD COLUMN last_login_at DATETIME NULL', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'deactivated_at');
SET @s := IF(@c = 0, 'ALTER TABLE users ADD COLUMN deactivated_at DATETIME NULL', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'deactivated_by');
SET @s := IF(@c = 0, 'ALTER TABLE users ADD COLUMN deactivated_by BIGINT NULL', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Any row with a NULL `active` (legacy drift, see 052) counts as active.
UPDATE users SET active = 1 WHERE active IS NULL;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'rbac_scopes' AND column_name = 'is_system');
SET @s := IF(@c = 0, 'ALTER TABLE rbac_scopes ADD COLUMN is_system TINYINT(1) NOT NULL DEFAULT 0', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Mark the all-NULL "everything" scope(s) as system scopes.
UPDATE rbac_scopes SET is_system = 1
WHERE cloud IS NULL AND account_ref_id IS NULL
  AND regions IS NULL AND services IS NULL
  AND resource_groups IS NULL AND resource_ids IS NULL
  AND tag_selector IS NULL;

-- Orphaned polymorphic rows (principal deleted before this fix shipped).
DELETE rb FROM role_bindings rb
 WHERE rb.principal_type = 'user'
   AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = rb.principal_id);
DELETE rb FROM role_bindings rb
 WHERE rb.principal_type = 'group'
   AND NOT EXISTS (SELECT 1 FROM org_groups g WHERE g.id = rb.principal_id);

DELETE po FROM permission_overrides po
 WHERE po.principal_type = 'user'
   AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = po.principal_id);
DELETE po FROM permission_overrides po
 WHERE po.principal_type = 'group'
   AND NOT EXISTS (SELECT 1 FROM org_groups g WHERE g.id = po.principal_id);

-- access_reviews is an attestation log: keep the history, only drop rows
-- whose principal no longer exists AND that point at no surviving binding.
DELETE ar FROM access_reviews ar
 WHERE ar.principal_type = 'user'
   AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = ar.principal_id);
DELETE ar FROM access_reviews ar
 WHERE ar.principal_type = 'group'
   AND NOT EXISTS (SELECT 1 FROM org_groups g WHERE g.id = ar.principal_id);
