-- db/migrations/023_escalation_policies.sql
--
-- Escalation policy (ack SLA -> escalate to next group), roadmap phase 9
-- (2026-09-13).
--
-- IMPORTANT LIMITATION, stated plainly: this policy ENGINE has no
-- notification TEETH yet. Escalating an alert here means "reassign its
-- visible owner to org_group X and record that it happened" -- it does
-- NOT send an email/Slack message to anyone, because item #3 (SMTP
-- wiring for alert notifications) is explicitly deferred pending a
-- department mailbox. app/collector/escalation.py's
-- _notify_escalation() is the single place to plug real notification in
-- once that lands -- everything else in this phase (policy CRUD,
-- SLA evaluation, audit trail) works today and is immediately useful for
-- the Alerts UI to show "this has been escalated to L2-platform" even
-- before anyone gets pinged about it.
--
-- escalate_to_group_id references org_groups (db/migrations/013), NOT a
-- single user -- escalating to a GROUP, not a person, is the whole point
-- (see roadmap discussion: "ack SLA -> escalate to next user/group").
CREATE TABLE IF NOT EXISTS escalation_policies (
  id                    BIGINT AUTO_INCREMENT PRIMARY KEY,
  aws_account_id        BIGINT NULL,        -- NULL = applies to every account (global fallback policy)
  severity              ENUM('WARNING','CRITICAL') NOT NULL,
  ack_sla_minutes       INT NOT NULL,
  escalate_to_group_id  BIGINT NOT NULL,
  enabled               TINYINT(1) NOT NULL DEFAULT 1,
  created_by            BIGINT NOT NULL,
  created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_policy_scope (aws_account_id, severity),
  CONSTRAINT fk_esc_account FOREIGN KEY (aws_account_id) REFERENCES aws_accounts(id) ON DELETE CASCADE,
  CONSTRAINT fk_esc_group   FOREIGN KEY (escalate_to_group_id) REFERENCES org_groups(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Tracks escalation state directly on the alert row so the Alerts UI
-- can show it without a join, and so app/collector/escalation.py's
-- evaluation query can filter "not yet escalated" cheaply.
ALTER TABLE alerts
  ADD COLUMN escalated_at          TIMESTAMP NULL AFTER muted_until,
  ADD COLUMN escalated_to_group_id BIGINT    NULL AFTER escalated_at;
