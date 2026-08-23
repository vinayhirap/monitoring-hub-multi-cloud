-- db/migrations/010_provider_credentials_table.sql
--
-- Step 2 of the multi-cloud build: storage for Azure/GCP secrets. AWS uses
-- IAM Role ARNs (an identity, not a secret) so it never needed this; Azure
-- Service Principals and GCP Service Accounts have a real secret (client
-- secret / SA key JSON) that must be encrypted at rest and never returned
-- by any read API.
--
-- One row per account (1:1 with aws_accounts.id). credential_ref on
-- aws_accounts is an opaque pointer only; the actual ciphertext lives here,
-- separated so a leaked aws_accounts row/backup never carries a secret with it.
--
-- Do not run this file directly — apply_multicloud_full_build.py checks
-- table existence first (safe to re-run).

CREATE TABLE IF NOT EXISTS provider_credentials (
  aws_account_id     INT NOT NULL PRIMARY KEY,
  provider            ENUM('azure','gcp') NOT NULL,
  credential_ref      VARCHAR(64) NOT NULL,
  secret_encrypted     MEDIUMBLOB NOT NULL,
  created_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_provider_credentials_account
    FOREIGN KEY (aws_account_id) REFERENCES aws_accounts(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
