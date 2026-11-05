-- Email & Reports: split the single emailcenter.manage into the pieces that carry different risk.
--
--   emailcenter.view        editor, admin   status summary, report schedules, alert rules (no addresses)       (135, unchanged)
--   emailcenter.manage      admin           create / edit / pause / delete schedules and rules, run now         (135, narrowed in meaning)
--   emailcenter.recipients  admin           recipient lists and the user / group pickers (people's addresses)
--   emailcenter.log         admin           the delivery log and the details of the last failure (addresses, SMTP errors)
--   emailcenter.test        admin           send a test email (a way to make the mail server send to any address)
--
-- Nobody loses access: every role that already holds emailcenter.manage is given the three new codes, in both the
-- legacy role_permissions table and the v2 role_permissions_v2 table. Revoke them per role afterwards to narrow it.
-- Idempotent (INSERT IGNORE / UPDATE by code), MariaDB-safe.

INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('emailcenter.recipients', 'Operations', 'Manage Email & Reports recipients',
   'Create, edit and delete recipient lists and pick CloudOps users and org groups (their email addresses become visible)', 0),
  ('emailcenter.log',        'Operations', 'Read the Email & Reports delivery log',
   'Read every email CloudOps sent (recipient addresses, subjects, failure reasons)', 0),
  ('emailcenter.test',       'Operations', 'Send Email & Reports test emails',
   'Make the mail server send a test message to an address you type', 0);

UPDATE permissions SET label = 'Manage report schedules and alert email rules',
  description = 'Create, edit, pause, delete and run report schedules and alert & incident email rules'
WHERE code = 'emailcenter.manage';

-- legacy role_permissions: whoever has manage gets the three new codes
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT rp.role, np.id
FROM role_permissions rp
JOIN permissions mp ON mp.id = rp.permission_id AND mp.code = 'emailcenter.manage'
JOIN permissions np ON np.code IN ('emailcenter.recipients', 'emailcenter.log', 'emailcenter.test');

-- v2 resolver: same rule
INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT rp.role_id, np.id
FROM role_permissions_v2 rp
JOIN permissions mp ON mp.id = rp.permission_id AND mp.code = 'emailcenter.manage'
JOIN permissions np ON np.code IN ('emailcenter.recipients', 'emailcenter.log', 'emailcenter.test');

-- admin explicitly (its runtime check bypasses the table, but the permission catalog UI reads these rows)
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code IN ('emailcenter.recipients', 'emailcenter.log', 'emailcenter.test');
INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'admin' AND p.code IN ('emailcenter.recipients', 'emailcenter.log', 'emailcenter.test');
