-- db/migrations/033_sso_auth_source.sql
--
-- SAML SSO scaffolding (2026-09-14) -- see app/auth/saml.py's module
-- docstring for the full design (single-IdP, env-var configured, OFF
-- by default until SSO_SAML_ENABLED=true).
--
-- auth_source is purely informational -- lets an admin see, in the
-- existing user-management UI, which accounts were auto-provisioned
-- via SSO vs. created locally with a password (e.g. to know which
-- users would lose access if SSO were later disabled). Nothing in
-- app/api/auth.py's existing local-password login reads or checks
-- this column -- local login keeps working completely unchanged,
-- for every user, regardless of this column's value.
ALTER TABLE users
  ADD COLUMN auth_source ENUM('local', 'sso') NOT NULL DEFAULT 'local' AFTER role;
