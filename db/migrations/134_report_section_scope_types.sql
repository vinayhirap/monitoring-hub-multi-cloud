-- db/migrations/134_report_section_scope_types.sql
--
-- Reports for the newer sections: cloud cost, host agents, environments, security findings and SLOs.
-- report_jobs.scope_type and reports.scope_type are ENUMs (047, extended by 090 for SYNTHETIC); the new values are
-- appended at the END so MySQL changes the column definition without rewriting the table.
-- Idempotent: running it again sets the same list. Existing rows are untouched.

ALTER TABLE report_jobs
  MODIFY COLUMN scope_type ENUM('ACCOUNT','RESOURCE','INCIDENT','CLIENT','SYNTHETIC','COST','HOST','ENVIRONMENT','SECURITY','SLO') NOT NULL;
ALTER TABLE reports
  MODIFY COLUMN scope_type ENUM('ACCOUNT','RESOURCE','INCIDENT','CLIENT','SYNTHETIC','COST','HOST','ENVIRONMENT','SECURITY','SLO') NOT NULL;
