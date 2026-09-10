-- db/migrations/011_widen_resource_id.sql
--
-- resources.resource_id was VARCHAR(100). AWS resource IDs (i-xxxx,
-- vol-xxxx, ARNs for Lambda) fit comfortably. GCP resource paths
-- (projects/{p}/zones/{z}/instances/{name}) mostly fit too.
--
-- Azure ARM resource IDs do NOT fit:
--   /subscriptions/{36-char-guid}/resourceGroups/{rg}/providers/
--     Microsoft.Compute/virtualMachines/{name}
-- routinely runs 120-160+ characters. Under MySQL 8's default strict
-- mode, every Azure resource discovery INSERT for VM/Storage/SQL/App
-- Service resources has been failing outright with "Data too long for
-- column 'resource_id'" since app/providers/azure/discovery.py started
-- writing full ARM IDs. This widens the column so those inserts (and the
-- Step 4 Azure metrics collector, which needs the full ARM ID to query
-- Azure Monitor) actually work.
--
-- Safe to run repeatedly / on a table that already has this width.

ALTER TABLE resources
  MODIFY COLUMN resource_id VARCHAR(512) NOT NULL;
