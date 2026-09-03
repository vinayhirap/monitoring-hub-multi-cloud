-- db/migrations/014_user_email_column.sql
--
-- Adds an optional email address per user, needed to actually send
-- mail (welcome emails on account creation, password-reset links --
-- see app/email/mailer.py). Nullable: existing users have none on
-- file and nothing breaks for them; they simply don't receive email
-- until an admin sets one. No UI for editing an EXISTING user's email
-- yet in this pass -- only settable at creation time.

ALTER TABLE users ADD COLUMN email VARCHAR(255) NULL AFTER username;
