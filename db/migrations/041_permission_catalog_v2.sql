-- db/migrations/041_permission_catalog_v2.sql
--
-- Closes the drift between the permission catalog (seeded in 015, with
-- three one-off additions in 024/029/032) and the features that have
-- actually shipped since.
--
-- AUDIT THAT MOTIVATED THIS (run against the tree at 9c31df0):
--   * 31 permission codes defined, 26 enforced anywhere.
--   * 17 codes defined but never passed to require_permission():
--       dashboard.view, groups.view, monitoring.advanced,
--       organization.settings.view/update, permissions.view/manage,
--       rbac.policy.view/manage, roles.view, system.admin,
--       system.config.manage, system.smtp.manage,
--       troubleshooting.execute, users.view/update/delete
--     Several of these ARE gated -- but by require_role("admin"),
--     which bypasses the permission layer entirely and so cannot be
--     delegated, reviewed, or shown in the permission matrix UI. That
--     is the actual defect: two parallel enforcement mechanisms where
--     only one is visible to administrators.
--   * Whole features with no write-side permission at all: incidents
--     (incidents.view only, for 5 endpoints), escalation policies
--     (borrowed alerts.configure), status_page (manage with no view),
--     webhooks (no permission whatsoever -- router mounted without
--     _auth_dep in main.py, intentionally, since it is an inbound
--     receiver; the MANAGEMENT side still needs one).
--
-- Adding a code here is inert until an endpoint references it, so this
-- migration cannot break a running system on its own. The companion
-- code change wires them up one router at a time.

-- ─────────────────────────────────────────────────────────────────────
-- New permission codes
-- ─────────────────────────────────────────────────────────────────────
INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  -- Alerts: view existed; the write side was all folded into the
  -- single overloaded 'operations.execute'.
  ('alerts.acknowledge',   'Monitoring', 'Acknowledge Alerts',   'Acknowledge an active alert', 0),
  ('alerts.resolve',       'Monitoring', 'Resolve Alerts',       'Manually resolve an alert', 0),
  ('alerts.suppress',      'Monitoring', 'Suppress Alerts',      'Silence or mark an alert a false positive', 0),

  -- Incidents: 'incidents.view' was the only code for the whole feature.
  ('incidents.create',     'Incidents',  'Create Incidents',     'Open an incident manually', 0),
  ('incidents.update',     'Incidents',  'Update Incidents',     'Edit incident status, severity, assignment', 0),
  ('incidents.resolve',    'Incidents',  'Resolve Incidents',    'Close out an incident', 0),
  ('incidents.postmortem.view',     'Incidents', 'View Postmortems',     'Read generated postmortem reports', 0),
  ('incidents.postmortem.generate', 'Incidents', 'Generate Postmortems', 'Trigger LLM postmortem generation', 0),

  -- Escalation: was reusing alerts.configure, which conflated
  -- "who gets paged" with "what threshold fires".
  ('escalation.view',      'Operations', 'View Escalation Policies',   'See on-call and escalation routing', 0),
  ('escalation.manage',    'Operations', 'Manage Escalation Policies', 'Create/edit escalation and on-call routing', 0),

  -- Operational events
  ('op_events.create',     'Operations', 'Record Operational Events',  'Log a deploy/change event', 0),

  -- Topology: manage existed (024), view did not.
  ('topology.view',        'Resources',  'View Topology',        'See the resource dependency graph', 0),

  -- Status page: manage existed (037), view did not.
  ('status_page.view',     'Operations', 'View Status Page Config',  'See status page configuration', 0),
  ('status_page.publish',  'Operations', 'Publish Status Updates',   'Post public incident updates', 0),

  -- Security / CSPM findings
  ('security.suppress',    'Security',   'Suppress Findings',    'Mark a security finding accepted/suppressed', 0),
  ('security.resolve',     'Security',   'Resolve Findings',     'Close out a security finding', 0),

  -- Metric catalog: was borrowing accounts.onboard for discovery.
  ('metric_catalog.view',     'Monitoring', 'View Metric Catalog',     'Browse available metrics per service', 0),
  ('metric_catalog.manage',   'Monitoring', 'Manage Metric Catalog',   'Enable/disable metrics for collection', 0),
  ('metric_catalog.discover', 'Monitoring', 'Discover Metrics',        'Run on-demand CloudWatch ListMetrics discovery', 0),

  -- Accounts: onboard existed; update/delete/credential rotation did not.
  ('accounts.update',      'Resources',  'Update Accounts',      'Edit an onboarded account''s settings', 0),
  ('accounts.delete',      'Resources',  'Remove Accounts',      'Offboard a cloud account', 0),
  ('accounts.credentials.manage', 'Resources', 'Manage Account Credentials', 'Rotate role ARNs, keys, service principals', 0),

  -- Synthetic monitoring: view/manage existed (031); run did not.
  ('synthetic.run',        'Monitoring', 'Run Synthetic Checks', 'Trigger an on-demand synthetic check', 0),

  -- Deploy risk: view existed (032).
  ('deploy_risk.manage',   'Operations', 'Manage Deploy Risk',   'Configure deploy risk scoring inputs', 0),

  -- Webhooks (management side)
  ('webhooks.view',        'Integrations', 'View Webhooks',      'See configured outbound webhooks', 0),
  ('webhooks.manage',      'Integrations', 'Manage Webhooks',    'Create/edit outbound webhook targets', 0),

  -- SSO
  ('sso.view',             'Integrations', 'View SSO Config',    'See SAML/OIDC configuration', 0),
  ('sso.manage',           'Integrations', 'Manage SSO Config',  'Change identity provider configuration', 0),

  -- User management gaps
  ('users.password.reset', 'User Management', 'Reset User Passwords', 'Force a password reset for another user', 0),

  -- RBAC v2 surfaces (migration 040)
  ('roles.create',         'RBAC Administration', 'Create Roles',        'Define a new custom role', 0),
  ('roles.update',         'RBAC Administration', 'Update Roles',        'Edit a role''s permission set', 0),
  ('roles.delete',         'RBAC Administration', 'Delete Roles',        'Remove a custom role', 0),
  ('rbac.binding.view',    'RBAC Administration', 'View Role Bindings',  'See who holds which role at which scope', 0),
  ('rbac.binding.manage',  'RBAC Administration', 'Manage Role Bindings','Grant/revoke a role at a scope', 0),
  ('rbac.scope.view',      'RBAC Administration', 'View Scopes',         'See defined access scopes', 0),
  ('rbac.scope.manage',    'RBAC Administration', 'Manage Scopes',       'Create/edit reusable access scopes', 0),
  ('rbac.override.manage', 'RBAC Administration', 'Manage Deny Overrides','Create explicit permission denies', 0),
  ('rbac.review.conduct',  'RBAC Administration', 'Conduct Access Reviews','Attest/revoke access during a review', 0),
  ('audit.export',         'RBAC Administration', 'Export Audit Logs',   'Download the audit trail', 0)
;


-- ─────────────────────────────────────────────────────────────────────
-- Role -> permission mapping for the new codes
-- ─────────────────────────────────────────────────────────────────────
-- Written against roles/role_permissions_v2 (migration 040) AND
-- mirrored into the legacy role_permissions table, so a deploy that
-- runs these migrations but has not yet cut over to the v2 resolver
-- behaves identically. Remove the legacy half once
-- app.auth.permissions no longer reads role_permissions.

-- viewer: read surfaces only. Deliberately NOT given
-- incidents.postmortem.generate (LLM spend) or metric_catalog.discover
-- (live CloudWatch API calls billed per request) -- both are reads in
-- name but cost money per invocation, which is an authorization
-- concern, not a UX one.
INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'viewer' AND p.code IN (
  'escalation.view','topology.view','status_page.view','metric_catalog.view',
  'incidents.postmortem.view','webhooks.view'
);

-- editor: viewer's set plus every operational write within scope.
-- NOT given: sso.manage, accounts.delete, accounts.credentials.manage,
-- or any rbac.* code -- those alter the security boundary itself and
-- stay admin-only regardless of how wide an editor's scope is.
INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'editor' AND p.code IN (
  'alerts.acknowledge','alerts.resolve','alerts.suppress',
  'incidents.create','incidents.update','incidents.resolve',
  'incidents.postmortem.view','incidents.postmortem.generate',
  'escalation.view','escalation.manage','op_events.create',
  'topology.view','status_page.view','status_page.publish',
  'security.suppress','security.resolve',
  'metric_catalog.view','metric_catalog.manage','metric_catalog.discover',
  'accounts.update','synthetic.run','deploy_risk.manage',
  'webhooks.view','webhooks.manage'
);

-- admin: everything, including the new rbac.* and credential codes.
INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p WHERE r.role_key = 'admin';

-- Legacy mirror.
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT r.role_key, rpv.permission_id
FROM role_permissions_v2 rpv JOIN roles r ON r.id = rpv.role_id
WHERE r.role_key IN ('admin','editor','viewer');
