#!/bin/bash
# =============================================================
# UPDATE — run this every time you want AuroGov Mumbai to pick up a push
# Run as: sudo bash update.sh   (from anywhere)
#
# Revised 2026-08-26 after the AuroGov Mumbai schema-migration incident.
# Changes from the prior version, and why:
#
#   1. STOPS monitoring-hub BEFORE running migrations, restarts it after.
#      Root cause of that incident's stuck ALTER TABLE: the live service
#      was still running and holding open DB connections/transactions
#      while a migration tried to ALTER the same tables, producing a
#      metadata-lock pile-up that hung indefinitely. setup.sh never had
#      this problem (service isn't started until its last step) — but
#      update.sh runs migrations against an already-running service on
#      every single deploy, so this was a standing landmine, not a
#      one-off. See the "Stop backend service" step below.
#
#   2. Checks app/aws/sts.py for the same-account AssumeRole short-circuit
#      after the git pull and WARNS loudly if it's missing, instead of
#      silently trusting that it was committed upstream. See the
#      "same-account AssumeRole fix" step below for the full story.
#
#   3. No longer silently swallows a missing verify_deployment.py.
#      Previously `|| true` meant a missing script and a script that ran
#      but failed looked identical in the output — confirmed via
#      `git log -- verify_deployment.py` that this file has never existed
#      in this repo, so every prior deploy's "post-deploy health check"
#      silently did nothing. Now it's an explicit, visible WARNING with
#      manual fallback checks.
# =============================================================
set -e

APP_DIR="/opt/monitoring-hub"
REPO_DIR="$APP_DIR/app"
VENV_DIR="$APP_DIR/venv"
SERVICE_NAME="monitoring-hub"
DB_NAME="monitoring_hub"
REAL_USER="${SUDO_USER:-$USER}"
EXPECTED_REPO="https://github.com/vinayhirap/monitoring-hub-multi-cloud.git"

echo "=== Updating CloudOps Monitoring Hub (AuroGov Mumbai) ==="

cd "$REPO_DIR"

CURRENT_REMOTE=$(sudo -u "$REAL_USER" git remote get-url origin 2>/dev/null || echo "")
if [ "$CURRENT_REMOTE" != "$EXPECTED_REPO" ]; then
    echo "WARNING: origin is '$CURRENT_REMOTE', expected '$EXPECTED_REPO'."
    echo "Run setup.sh first, or fix with:"
    echo "  git -C $REPO_DIR remote set-url origin $EXPECTED_REPO"
    exit 1
fi

sudo -u "$REAL_USER" git pull origin main
echo "--- Code updated ---"

echo "--- Checking app/aws/sts.py for the same-account AssumeRole short-circuit ---"
# Root cause (2026-08-26): a monitored AWS account CAN legitimately be the
# same account the app server itself lives in. A real sts:AssumeRole call
# against your own role always fails AccessDenied unless its trust policy
# explicitly allows self-assumption, which silently breaks EC2/RDS/S3/
# Lambda/ECS/EBS discovery + metrics for that account (ALB is unaffected —
# it goes through a separate no-AssumeRole path in collector_direct.py).
# The fix belongs committed into app/aws/sts.py so `git pull` above already
# picked it up — this just verifies that actually happened instead of
# assuming it.
if grep -q "No STS call is actually needed" app/aws/sts.py 2>/dev/null; then
    echo "OK: same-account short-circuit present in app/aws/sts.py."
else
    echo "WARNING: app/aws/sts.py does NOT have the same-account AssumeRole"
    echo "short-circuit. Any account row whose role_arn points at this"
    echo "server's own account will silently fail EC2/RDS/S3/Lambda/ECS/EBS"
    echo "discovery and metrics collection (dashboard will show accounts/"
    echo "instances but no metric data, with 'GetFederationToken'/AccessDenied"
    echo "or AssumeRole errors in the logs)."
    echo "Get this committed upstream into app/aws/sts.py. Stopgap for now:"
    echo "  sudo -u $REAL_USER $VENV_DIR/bin/python3 apply_same_account_role_fix.py"
    echo "  sudo -u $REAL_USER $VENV_DIR/bin/python3 apply_same_account_role_fix_v2.py"
    echo "(v2 fixes v1's own bug: GetFederationToken can't be called with the"
    echo "session credentials an EC2 instance profile provides — v2 replaces"
    echo "that call with a plain boto3.Session(), which is the correct fix.)"
fi

echo "--- Updating Python deps (if requirements.txt changed) ---"
sudo -u "$REAL_USER" "$VENV_DIR/bin/pip" install -r requirements.txt --quiet

# Pre-create db_backups with correct ownership before any apply_*.py script
# runs, so a stray root-owned dir from a prior sudo run can't block these
# under $REAL_USER. Bit us during the 2026-08-24 metric-catalog incident.
mkdir -p "$REPO_DIR/db_backups"
chown -R "$REAL_USER":"$REAL_USER" "$REPO_DIR/db_backups"

echo "--- Stopping backend service before migrations ---"
# Prevents the 2026-08-26 lock-pileup incident from recurring: with the
# service running, its own open DB connections/transactions can block a
# migration's ALTER TABLE indefinitely (metadata lock wait), and a killed/
# retried migration run can itself leave orphaned connections that block
# the NEXT run too. Stopping first means every migration below runs
# against a DB with no other active client.
systemctl stop ${SERVICE_NAME} 2>/dev/null || true

echo "--- Idempotent schema migrations (safe to re-run every deploy) ---"
# Only existence-checked, backed-up, non-destructive migrations run
# automatically here. This deliberately does NOT run everything in
# db/migrations/ — some of those delete or rewrite data and must stay a
# manual, reviewed step.
run_migration() {
    local script="$1"
    local what="$2"
    echo "--- $what ---"
    sudo -u "$REAL_USER" "$VENV_DIR/bin/python3" "$script" || \
        echo "WARNING: $script failed — check manually before relying on: $what"
}

run_migration apply_multi_cloud_migration.py \
    "009: aws_accounts/resources/metric_catalog provider columns"
run_migration apply_multi_cloud_credentials.py \
    "010 (partial): aws_accounts.client_secret / gcp_service_account_key"
run_migration apply_fresh_schema_migrations_fk_type_fix.py \
    "safety net: provider_credentials.aws_account_id INT->BIGINT (no-op once already applied)"
run_migration apply_fresh_schema_migrations.py \
    "003/004/005/010(table)/011(widen): metric_catalog full columns + account_metric_selections, provider_credentials table, password_reset_tokens, metrics dedup key, resources.resource_id widen"
run_migration apply_resources_region_column_fix.py \
    "resources.region column + backfill (no-op once already applied)"
run_migration apply_metrics_dedup_fix.py \
    "metrics duplicate cleanup + uniq_metrics_resource_metric (now checks index_exists first — fixed 2026-08-26, was not actually idempotent before)"
run_migration apply_access_scopes_migration.py \
    "011: access_scopes table (Phase 1 authorization)"
run_migration apply_alert_evaluation_hardening_migration.py \
    "012: alerts.last_seen_at/healthy_streak + alert_pending table"
run_migration scripts/seed_metric_catalog.py \
    "seed: metric_catalog curated + directory entries"

echo "--- Rebuilding frontend ---"
cd "$REPO_DIR/frontend"
sudo -u "$REAL_USER" npm install --silent
sudo -u "$REAL_USER" npm run build

echo "--- Restarting backend service ---"
systemctl restart ${SERVICE_NAME}
systemctl reload nginx

echo ""
echo "=== Update complete ==="
systemctl status ${SERVICE_NAME} --no-pager

echo ""
echo "--- Post-deploy health check ---"
if [ -f "$REPO_DIR/verify_deployment.py" ]; then
    sudo -u "$REAL_USER" "$VENV_DIR/bin/python3" "$REPO_DIR/verify_deployment.py" || true
else
    echo "WARNING: verify_deployment.py not found in $REPO_DIR."
    echo "This script has been referenced by setup.sh/update.sh as the"
    echo "automated post-deploy health check (VM-box SG rule, schema drift,"
    echo "same-account AssumeRole) but has never actually existed in this"
    echo "repo's history — every prior deploy silently skipped this check."
    echo "Either get it written and committed, or verify manually:"
    echo "  1. VM box reachable + its Security Group allows this server's IP:"
    echo "       curl -m5 \"\${VM_URL:-<check .env for VM_URL>}/api/v1/query?query=up\""
    echo "  2. Metrics are actually landing (allow a few minutes after restart):"
    echo "       mysql -umonitor -p $DB_NAME -e \"SELECT COUNT(*) FROM metrics;\""
    echo "  3. app/aws/sts.py same-account short-circuit — see check earlier in this run."
fi

# NOTE: this script still does NOT run every file in db/migrations/
# automatically — only the migrations above. New migration files that
# aren't wrapped in one of those (or a new apply_*.py in the same
# existence-checked style) still need a manual, reviewed run:
#   mysql -umonitor -p<pass> monitoring_hub < db/migrations/<file>.sql
# If a new migration is genuinely safe to automate, prefer writing it as
# an apply_*.py in the apply_multi_cloud_migration.py style (column/index/
# table existence checks, mysqldump backup, dry-run flag) and adding it
# to the list above.
