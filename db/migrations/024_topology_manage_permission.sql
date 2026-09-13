-- db/migrations/024_topology_manage_permission.sql
--
-- app/api/topology.py's add_manual_edge/delete_manual_edge endpoints
-- (roadmap phase 4/7) were gated on resources.view -- the same
-- permission as the read-only GET endpoint. That's a real RBAC gap:
-- resources.view is granted to the viewer role (see
-- db/migrations/015_permissions_rbac.sql's viewer seed list), so any
-- viewer could add or delete a manually-declared dependency between
-- two resources, despite the permission's own catalog description
-- being "EC2/RDS/Lambda/etc. resource listings" -- a read description,
-- not a write one. Every other write action in this app (threshold
-- config, escalation policies, user management, group management) has
-- its own distinct permission from the read-only view alongside it;
-- topology was the one exception, presumably because it was built as
-- a single feature in one pass rather than read/write being considered
-- separately.
--
-- Fix: a dedicated topology.manage permission for the two write
-- endpoints only. GET stays on resources.view, unchanged -- viewing
-- the topology graph is exactly the same kind of action as viewing any
-- other resource listing. Granted to editor (not viewer) by default,
-- matching every other write permission's default placement in the
-- viewer/editor/admin ladder.
INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('topology.manage', 'Resources', 'Manage Resource Topology', 'Add or remove manually-declared resource dependencies', 0);

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code = 'topology.manage';

-- admin already gets every permission via the SELECT 'admin', id FROM
-- permissions pattern in 015 for permissions that existed at that
-- migration's run time -- a permission added later needs its own
-- explicit admin grant, same as editor above (admin's runtime
-- permission check bypasses the table entirely per 015's comment, but
-- this row still exists so the permissions-catalog UI renders admin's
-- column as fully checked without special-casing it client-side).
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code = 'topology.manage';
