-- db/migrations/125_cost_explorer_usage.sql
--
-- Cost explorer page: keep the usage QUANTITY next to the cost (Cost Explorer returns both in the same request, at no
-- extra charge), so the page can show metrics requested / requests / GB per day and prove CloudOps's own meter against
-- what AWS billed. Also records how many days each successful read covered, so only the first read pulls 90 days and
-- later reads pull 14 (one Cost Explorer request per account per day instead of re-reading settled history).
--
-- Idempotent: safe to run twice. MySQL 8 and MariaDB. Nothing reads the new columns unless they exist.

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'aws_cost_actuals' AND column_name = 'usage_quantity');
SET @sql := IF(@c = 0,
  'ALTER TABLE aws_cost_actuals ADD COLUMN usage_quantity DECIMAL(20,4) NULL',
  'SELECT "aws_cost_actuals.usage_quantity already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'aws_cost_actuals' AND column_name = 'usage_unit');
SET @sql := IF(@c = 0,
  'ALTER TABLE aws_cost_actuals ADD COLUMN usage_unit VARCHAR(40) NULL',
  'SELECT "aws_cost_actuals.usage_unit already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'aws_cost_fetch_log' AND column_name = 'window_days');
SET @sql := IF(@c = 0,
  'ALTER TABLE aws_cost_fetch_log ADD COLUMN window_days INT NULL',
  'SELECT "aws_cost_fetch_log.window_days already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
