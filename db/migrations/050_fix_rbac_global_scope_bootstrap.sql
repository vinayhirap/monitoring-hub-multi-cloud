-- db/migrations/050_fix_rbac_global_scope_bootstrap.sql
--
-- Fixes a latent bug in 040_rbac_v2_bindings.sql's bootstrap INSERT
-- for the global rbac_scope row. Found during Phase 2 verification
-- (spinning up a fresh MariaDB locally and running the full migration
-- set end to end) -- never triggered in THIS deployment because
-- prod/dev already had users by the time 040 ran, but it would break
-- anyone provisioning a genuinely fresh dev/staging/DR database from
-- these migrations in order.
--
-- The bug: 040's INSERT is
--   SELECT 'Organization (all clouds)', ..., MIN(id) FROM users
--   WHERE EXISTS (SELECT 1 FROM users) AND NOT EXISTS (...)
-- MIN(id) is an aggregate with no GROUP BY, so the query ALWAYS
-- returns exactly one row, even when `users` is empty -- an aggregate
-- over zero rows returns one row of NULLs, it does not return zero
-- rows. WHERE runs before aggregation and can't see that the
-- aggregate came back NULL, so the "WHERE EXISTS (SELECT 1 FROM
-- users)" guard 040's own comment describes as making this "safe to
-- run against a database with no users yet" doesn't actually prevent
-- the INSERT from being attempted -- it just makes created_by NULL,
-- which then fails the NOT NULL constraint and aborts the whole
-- migration (taking the deny-override table, the service catalog
-- seed, and access_reviews down with it, since mysql's default
-- multi-statement execution stops at the first error).
--
-- The fix: HAVING runs AFTER aggregation, so it can actually see and
-- discard the synthetic NULL row that WHERE couldn't. Dropped the now-
-- redundant "WHERE EXISTS (SELECT 1 FROM users)" entirely -- HAVING
-- MIN(id) IS NOT NULL covers the same case correctly. The NOT EXISTS
-- dedup guard is unchanged and still does its job in WHERE, since it
-- doesn't depend on the aggregate.
--
-- Idempotent and safe to run at any point after 040, on any DB state:
-- inserts the global scope only if (a) at least one user exists and
-- (b) no all-NULL-dimension scope already exists.

INSERT INTO rbac_scopes (label, cloud, account_ref_id, regions, services, created_by)
SELECT 'Organization (all clouds)', NULL, NULL, NULL, NULL, MIN(id)
FROM users
WHERE NOT EXISTS (
    SELECT 1 FROM rbac_scopes
    WHERE cloud IS NULL AND account_ref_id IS NULL
      AND regions IS NULL AND services IS NULL
      AND resource_groups IS NULL AND resource_ids IS NULL
      AND tag_selector IS NULL
  )
HAVING MIN(id) IS NOT NULL;
