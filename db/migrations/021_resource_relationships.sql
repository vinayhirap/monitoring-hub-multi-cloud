-- db/migrations/021_resource_relationships.sql
--
-- Topology/dependency graph, roadmap phase 4/7 (2026-09-13) -- ties
-- directly to the 2026-08-26 Mumbai RCA: that incident's actual failure
-- mode was resources (ELB/ALB) existing in AWS but silently absent from
-- this app's inventory, undetected until someone went looking by hand.
-- A dependency graph doubles as a coverage check going forward -- "this
-- ALB routes to 4 targets per AWS, do we have edges/nodes for all 4?"
-- surfaces that class of silent gap visually instead of requiring a
-- manual RCA.
--
-- Two edge sources, both stored in the same table, distinguished by
-- `source`:
--   'auto'   -- inferred from data this app ALREADY fetches for other
--              reasons (ALB target health -- see
--              app/aws/describe_polling.py's poll_alb_target_health(),
--              extended by this phase to also persist edges instead of
--              only healthy/unhealthy counts). Zero new AWS API calls.
--   'manual' -- operator-declared, for relationships no Describe API can
--              tell us (this Lambda calls that RDS instance, this EC2
--              talks to an external service). Added/removed via
--              app/api/topology.py; never touched by the auto-sync job,
--              so a resync can't clobber something added by hand.
--
-- source_resource_id / target_resource_id are the AWS resource
-- identifier strings (ARN or instance/volume/etc ID) -- same identifier
-- space as resources.resource_id / alerts.resource_id -- not
-- resources.id. VARCHAR(512) to match resources.resource_id's own width
-- (see db/migrations/016_widen_resource_id.sql).
--
-- edge_hash: see the first apply attempt's "1071 key too long" fix --
-- a generated STORED SHA2 hash of the full edge tuple is indexed
-- instead of the two raw VARCHAR(512) columns directly.
--
-- CREATE TABLE and the foreign key are deliberately TWO separate
-- statements, not one. The second apply attempt (still in the same
-- CREATE TABLE, FK added inline) hit "1215 Cannot add foreign key
-- constraint" even though aws_accounts.id is a plain `bigint` that
-- matches aws_account_id here exactly (confirmed via SHOW CREATE TABLE
-- aws_accounts, 2026-09-13) -- a known MySQL 8.0.x quirk where a STORED
-- generated column (edge_hash) combined with a FOREIGN KEY in the same
-- CREATE TABLE statement can trip errno 1215 even when every type lines
-- up. Splitting into CREATE TABLE (no FK) + ALTER TABLE ADD CONSTRAINT
-- sidesteps it entirely -- compare to 023_escalation_policies.sql,
-- which has two FKs inline successfully, but no generated column.
CREATE TABLE IF NOT EXISTS resource_relationships (
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
  aws_account_id      BIGINT       NOT NULL,
  source_resource_id  VARCHAR(512) NOT NULL,
  target_resource_id  VARCHAR(512) NOT NULL,
  relationship_type   VARCHAR(50)  NOT NULL,   -- 'routes_to' (auto, ALB->EC2), 'depends_on' (manual)
  source              VARCHAR(10)  NOT NULL DEFAULT 'auto',  -- 'auto' | 'manual'
  edge_hash CHAR(64) GENERATED ALWAYS AS (
    SHA2(CONCAT_WS(':', aws_account_id, source_resource_id, target_resource_id, relationship_type), 256)
  ) STORED,
  created_at          TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_edge_hash (edge_hash),
  KEY idx_rel_account (aws_account_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── Third apply attempt's fix: the REAL root cause ───────────────────
-- The second attempt (CREATE TABLE with no FK, then a separate ALTER
-- TABLE ADD CONSTRAINT -- the "sidesteps it entirely" claim in the
-- comment above) was tried live and hit the exact same bare 1215 on
-- the ALTER. A follow-up fix attempt assumed a type/signedness
-- mismatch against aws_accounts.id (this codebase has hit that exact
-- class of bug once before -- see migration 010's comment) and tried
-- dynamically matching the type via information_schema. That was
-- re-tried live too and got the IDENTICAL 1215 again, which ruled out
-- type mismatch entirely -- confirmed by reproducing this on a real,
-- freshly-installed MySQL 8.0.46 instance with aws_accounts.id and
-- aws_account_id as the exact same plain signed BIGINT on both sides,
-- and still getting error 1215 at the same line.
--
-- The actual cause, confirmed against MySQL's own reference manual
-- (13.1.20.8, "CREATE TABLE and Generated Columns"):
--
--   "A foreign key constraint on the base column of a stored
--    generated column cannot use CASCADE, SET NULL, or SET DEFAULT
--    as ON UPDATE or ON DELETE referential actions."
--
-- aws_account_id is a base column of edge_hash (a STORED generated
-- column, per its expression above), and the FK on aws_account_id
-- used ON DELETE CASCADE -- exactly the forbidden combination. This
-- is a hard MySQL 8 restriction with no workaround via column types;
-- it has nothing to do with the generated column's presence alone
-- (a FK with ON DELETE RESTRICT on a base column of a stored
-- generated column is fine), only with CASCADE/SET NULL/SET DEFAULT
-- specifically. (Testing this against MariaDB earlier did NOT
-- reproduce the error, because MariaDB's generated-column
-- implementation doesn't enforce this MySQL-specific restriction --
-- worth remembering for any future local reproduction attempts on
-- this codebase: MariaDB is not a reliable stand-in for MySQL 8's
-- generated-column/FK behavior.)
--
-- Fix: ON DELETE RESTRICT instead of CASCADE. This has NO effect on
-- current app behavior -- app/api/admin/accounts.py's account-removal
-- endpoint never issues a real DELETE against aws_accounts at all; it
-- soft-deletes via `UPDATE aws_accounts SET status = 'inactive'`, so
-- the CASCADE was dead code for that flow regardless. Confirmed via a
-- real MySQL 8.0.46 instance: FK creation succeeds, a normal INSERT +
-- the generated edge_hash both work, a raw DELETE against aws_accounts
-- is correctly blocked (1451, as RESTRICT should do), and the app's
-- actual UPDATE-based removal path is completely unaffected.
--
-- Follow-up recommended (not required for this migration to succeed):
-- app/api/admin/accounts.py's remove_account cleanup block already
-- explicitly deletes from resources/metrics/alerts for a removed
-- account (see its "this was previously a bug" comment) but does not
-- yet do the same for resource_relationships, since accounts.py isn't
-- one of the files this phase's patch touches. Worth adding
-- `DELETE FROM resource_relationships WHERE aws_account_id = %s`
-- alongside those once this table is actually being populated, so
-- removed accounts don't leave orphaned edges behind -- the same class
-- of gap that comment already fixed for the other three tables.
ALTER TABLE resource_relationships
  ADD CONSTRAINT fk_rel_account
    FOREIGN KEY (aws_account_id) REFERENCES aws_accounts(id) ON DELETE RESTRICT;
