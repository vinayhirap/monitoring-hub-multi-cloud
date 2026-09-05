#!/bin/bash
# =============================================================
# UPDATE — run this every time you want the server to pick up a push
# Run as: sudo bash update.sh   (from anywhere)
#
# "No manual intervention" version. Everything below was previously a
# manual fix applied live on the server after something broke — now it's
# either fixed at the source or checked automatically with a hard exit.
#
#   1. Stops the service BEFORE migrations, restarts after (unchanged —
#      this was already fixed 2026-08-26: a running service holds DB
#      connections that deadlock a migration's ALTER TABLE).
#
#   2. Ends with a HARD verification gate (table checks, code checks,
#      live endpoint checks, VM box reachability) that exits non-zero if
#      anything is actually broken — not just a printed WARNING buried in
#      a long log that's easy to miss.
#
#   3. bcrypt is pinned to 4.0.1 upstream in requirements.txt now (fixed
#      at the repo level) — this script also verifies the pin didn't
#      regress, since an unpinned bcrypt>=4.1 silently breaks every
#      login/password-hash call the next time deps are reinstalled.
#
#   4. Retries git pull / pip / npm on transient network failures instead
#      of hard-failing the whole deploy on a single flaky connection.
#
#   5. Installs Node if it's somehow missing instead of assuming it's
#      already there (defensive — update.sh previously just assumed
#      setup.sh had already installed it).
# =============================================================
set -e
set -o pipefail

APP_DIR="/opt/monitoring-hub"
REPO_DIR="$APP_DIR/app"
VENV_DIR="$APP_DIR/venv"
SERVICE_NAME="monitoring-hub"
DB_NAME="monitoring_hub"
DB_USER="monitor"
REAL_USER="${SUDO_USER:-$USER}"
EXPECTED_REPO="https://github.com/vinayhirap/monitoring-hub-multi-cloud.git"

retry() {
    local n=0 max=5 delay=5
    until "$@"; do
        n=$((n+1))
        if [ "$n" -ge "$max" ]; then
            echo "ERROR: command failed after $max attempts: $*"
            return 1
        fi
        echo "Retry $n/$max for: $* (waiting ${delay}s)"
        sleep "$delay"
    done
}

DEPLOY_FAILED=false
on_exit() {
    if [ "$DEPLOY_FAILED" = true ]; then
        echo ""
        echo "=== UPDATE FAILED — attempting to leave service/nginx running anyway ==="
        systemctl restart ${SERVICE_NAME} 2>/dev/null || true
        nginx -t 2>/dev/null && systemctl reload nginx 2>/dev/null || true
        echo "Re-run this script after fixing the error above. Do NOT treat this deploy as live."
    fi
}
trap on_exit EXIT
trap 'DEPLOY_FAILED=true' ERR

echo "=== Updating Monitoring Hub ==="
cd "$REPO_DIR"

CURRENT_REMOTE=$(sudo -u "$REAL_USER" git remote get-url origin 2>/dev/null || echo "")
if [ "$CURRENT_REMOTE" != "$EXPECTED_REPO" ]; then
    echo "ERROR: origin is '$CURRENT_REMOTE', expected '$EXPECTED_REPO'."
    echo "Run deploy.sh first, or fix with:"
    echo "  git -C $REPO_DIR remote set-url origin $EXPECTED_REPO"
    exit 1
fi

# Read the real DB password from .env instead of assuming a literal value,
# so this script never drifts from whatever deploy.sh actually wrote.
DB_PASS=$(grep -m1 '^DB_PASSWORD=' "$REPO_DIR/.env" | cut -d= -f2-)
VM_URL=$(grep -m1 '^VM_URL=' "$REPO_DIR/.env" | cut -d= -f2-)
if [ -z "$DB_PASS" ]; then
    echo "ERROR: DB_PASSWORD not found in $REPO_DIR/.env — was this box ever run through deploy.sh?"
    exit 1
fi

# Refuse to pull with local changes uncommitted upstream — a `git pull`
# that silently 3-way-merges server-side hotfixes is how divergence
# between the server's checkout and GitHub happens unnoticed.
if [ -n "$(sudo -u "$REAL_USER" git status --porcelain)" ]; then
    echo "ERROR: this server's checkout has local uncommitted changes:"
    sudo -u "$REAL_USER" git status --short
    echo "Commit and push them first (so they're not silently lost/merged),"
    echo "or stash them, before re-running update.sh."
    exit 1
fi

retry sudo -u "$REAL_USER" git pull origin main
echo "--- Code updated ---"

echo "--- Updating Python deps ---"
retry sudo -u "$REAL_USER" "$VENV_DIR/bin/pip" install -r requirements.txt --quiet

if ! grep -q "^bcrypt==4\.0\.1" requirements.txt; then
    echo "WARNING: requirements.txt no longer pins bcrypt==4.0.1 — the"
    echo "passlib/__about__ crash landmine may have regressed."
fi

mkdir -p "$REPO_DIR/db_backups"
chown -R "$REAL_USER":"$REAL_USER" "$REPO_DIR/db_backups"

echo "--- Stopping backend service before migrations ---"
systemctl stop ${SERVICE_NAME} 2>/dev/null || true

echo "--- Idempotent schema migrations ---"
MIGRATION_FAILURES=()
run_migration() {
    local script="$1"
    local what="$2"
    echo "--- $what ---"
    if ! sudo -u "$REAL_USER" "$VENV_DIR/bin/python3" "$script"; then
        echo "WARNING: $script failed — check manually before relying on: $what"
        MIGRATION_FAILURES+=("$script")
    fi
}

run_migration apply_multi_cloud_migration.py \
    "009: aws_accounts/resources/metric_catalog provider columns"
run_migration apply_multi_cloud_credentials.py \
    "010 (partial): aws_accounts.client_secret / gcp_service_account_key"
run_migration apply_fresh_schema_migrations_fk_type_fix.py \
    "safety net: provider_credentials.aws_account_id INT->BIGINT"
run_migration apply_fresh_schema_migrations.py \
    "003/004/005/010(table)/011(widen): metric_catalog + account_metric_selections + provider_credentials + password_reset_tokens + metrics dedup key + resources.resource_id widen"
run_migration apply_resources_region_column_fix.py \
    "resources.region column + backfill"
run_migration apply_metrics_dedup_fix.py \
    "metrics duplicate cleanup + uniq_metrics_resource_metric"
run_migration apply_access_scopes_migration.py \
    "011: access_scopes table (Phase 1 authorization)"
run_migration apply_alert_evaluation_hardening_migration.py \
    "012: alerts.last_seen_at/healthy_streak + alert_pending table"
run_migration apply_org_group_rbac.py \
    "013: org_groups/group_policies/user_group_memberships -- fixes editor/viewer 500s on scoped endpoints"
# apply_org_groups_ui_and_role_sync_fix.py and apply_permission_rbac_system.py
# are retired as of 2026-09-05: both abort in preflight because their
# literal-text anchors no longer match current main (function
# signatures / file structure moved on since these were written).
# Everything they were meant to add -- GROUP_LEVEL_ROLE, the Roles tab,
# app/auth/permissions.py, require_permission(...) on every group/user
# route, db/migrations/015_permissions_rbac.sql -- is already merged
# into main. Confirmed by inspection before removing this call; do not
# re-add without first checking whether the target code has moved on
# again.
run_migration apply_group_level_role_fix.py \
    "guard: re-assert GROUP_LEVEL_ROLE in authorization.py after 013's full rewrite drops it"
run_migration apply_default_org_groups_seed.py \
    "seed: default L1 Monitoring / L2 Operations / L3 Administrator org groups (must run AFTER apply_org_groups_ui_and_role_sync_fix.py)"
run_migration apply_permission_rbac_migration.py \
    "015: permissions/role_permissions seed data (must run AFTER apply_permission_rbac_system.py)"
run_migration scripts/seed_metric_catalog.py \
    "seed: metric_catalog curated + directory entries"

echo "--- db/migrations/*.sql tracking (migrate.py) ---"
# This is the permanent fix for the exact incident that prompted writing
# migrate.py: 014_user_email_column.sql shipped in the repo, git pull
# brought the FILE to this server, but nothing ever ran it -- there was
# no tracking of "pending" vs "applied" schema files, only this script's
# hardcoded run_migration list above (which only covers code+schema
# apply_*.py patches, not raw db/migrations/*.sql). From now on, any
# .sql file added to db/migrations/ is applied here automatically the
# next time update.sh runs -- no more hand-adding a run_migration line
# for schema-only changes, and no more silent gaps like 014.
#
# Refuses to run if migrate.py finds duplicate migration numbers (see
# `migrate.py status`) rather than guessing intent -- resolve those by
# renaming files, not by skipping this step.
if ! sudo -u "$REAL_USER" "$VENV_DIR/bin/python3" migrate.py apply --all-pending --yes; then
    echo "WARNING: migrate.py apply --all-pending failed or found unresolved"
    echo "duplicate migration numbers -- run 'migrate.py status' manually to see why."
    MIGRATION_FAILURES+=("migrate.py apply --all-pending")
fi

echo "--- Rebuilding frontend ---"
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | cut -d. -f1 | tr -d v)" -lt 20 ]; then
    echo "Node 20 not found — installing (should only happen once, defensively)."
    retry bash -c "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -"
    apt install -y nodejs
fi
cd "$REPO_DIR/frontend"
retry sudo -u "$REAL_USER" npm install --silent
sudo -u "$REAL_USER" npm run build

# Restore cwd to $REPO_DIR -- the verification block below greps
# RELATIVE paths (app/aws/sts.py, app/api/live_data.py, etc.) and will
# report false FAILs against frontend/app/... if left in the frontend
# dir where the npm build step above put us.
cd "$REPO_DIR"

echo "--- Restarting backend service ---"
systemctl restart ${SERVICE_NAME}
nginx -t
systemctl reload nginx

# --- HARD verification: exit non-zero if anything critical is actually missing ---
echo ""
echo "=== Post-update verification (hard gate — non-zero exit means NOT done) ==="
VERIFY_FAILED=false

check_table() {
    local table="$1"
    if ! mysql -u"${DB_USER}" -p"${DB_PASS}" "${DB_NAME}" -e "SHOW TABLES LIKE '${table}';" 2>/dev/null | grep -q "$table"; then
        echo "FAIL: table '${table}' is missing."
        VERIFY_FAILED=true
    fi
}
for t in org_groups group_policies user_group_memberships permissions role_permissions access_scopes alert_pending resources; do
    check_table "$t"
done

if ! grep -q "No STS call is actually needed" app/aws/sts.py 2>/dev/null; then
    echo "FAIL: app/aws/sts.py is missing the same-account AssumeRole short-circuit."
    VERIFY_FAILED=true
fi

if ! grep -q "max_workers=min(len(accounts), 8) or 1" app/api/live_data.py 2>/dev/null; then
    echo "FAIL: app/api/live_data.py is missing the max_workers>0 fix."
    VERIFY_FAILED=true
fi

if ! grep -q "GROUP_LEVEL_ROLE = " app/auth/authorization.py 2>/dev/null; then
    echo "FAIL: app/auth/authorization.py is missing GROUP_LEVEL_ROLE."
    echo "      Every POST /api/groups/{id}/members call will crash with"
    echo "      AttributeError, silently swallowed by the frontend's .catch(() => {})."
    VERIFY_FAILED=true
fi

if [ ! -f app/collector/leader.py ] || ! grep -q "run_when_leader" app/main.py 2>/dev/null; then
    echo "FAIL: app/collector/leader.py / app/main.py leader-election is missing."
    echo "      With --workers 2, every worker would start its own independent"
    echo "      copy of the scheduler/describe-poll/multicloud loops -- this is"
    echo "      the exact root cause of the Sep 5 2026 incident (duplicate"
    echo "      cycles, MySQL deadlocks, doubled AWS API calls). If a future"
    echo "      commit removed this, that commit needs to be reverted, not"
    echo "      this check."
    VERIFY_FAILED=true
fi

if ! grep -q "def get_db_cursor" app/db.py 2>/dev/null || ! grep -q "weakref" app/db.py 2>/dev/null; then
    echo "FAIL: app/db.py is missing the connection-pool leak-guard/context"
    echo "      manager. Without it, any exception in a DB call site without"
    echo "      try/finally leaks a pooled connection permanently -- this is"
    echo "      what exhausted the pool and took the dashboard offline for"
    echo "      hours on Sep 5 2026."
    VERIFY_FAILED=true
fi

if [ ${#MIGRATION_FAILURES[@]} -gt 0 ]; then
    echo "FAIL: these migration scripts reported errors: ${MIGRATION_FAILURES[*]}"
    VERIFY_FAILED=true
fi

echo "--- App endpoints ---"

if ! systemctl is-active --quiet ${SERVICE_NAME}; then
    echo "FAIL: ${SERVICE_NAME} is not active after restart -- dumping the last 50 log lines:"
    journalctl -u ${SERVICE_NAME} --no-pager -n 50 || true
    VERIFY_FAILED=true
fi

sleep 3

echo "--- Leader-election sanity (exactly one worker should have started collectors) ---"
LEADER_COUNT=$(journalctl -u ${SERVICE_NAME} --no-pager -S "$(date -d '-30 seconds' '+%Y-%m-%d %H:%M:%S')" 2>/dev/null \
    | grep -c "\[leader\] acquired")
echo "leader-election log lines since restart: ${LEADER_COUNT}"
if [ "$LEADER_COUNT" -eq 0 ]; then
    echo "FAIL: no worker acquired the collector leader lock -- background"
    echo "      loops (scheduler/discovery/alerts) are not running at all."
    echo "      Check MySQL connectivity from app/collector/leader.py."
    VERIFY_FAILED=true
elif [ "$LEADER_COUNT" -gt 1 ]; then
    echo "FAIL: ${LEADER_COUNT} workers acquired the leader lock -- this is"
    echo "      the exact duplicate-collector-loops condition that caused the"
    echo "      Sep 5 2026 MySQL deadlocks and pool exhaustion. Leader election"
    echo "      is broken; do not consider this update done."
    VERIFY_FAILED=true
fi

AUTH_ME_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/api/auth/me || true)
echo "auth/me:        ${AUTH_ME_CODE}"
LIVE_ACCOUNTS_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/api/live/accounts || true)
echo "live/accounts:  ${LIVE_ACCOUNTS_CODE}"
PERMISSIONS_ME_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/api/permissions/me || true)
echo "permissions/me: ${PERMISSIONS_ME_CODE}"
FRONTEND_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1/ || true)
echo "frontend:       ${FRONTEND_CODE}"

if [ "$AUTH_ME_CODE" = "000" ] || [ "$LIVE_ACCOUNTS_CODE" = "000" ] || [ "$PERMISSIONS_ME_CODE" = "000" ]; then
    echo "FAIL: backend is not answering on 127.0.0.1:8000 at all (000 = connection failed, not just unauthorized)."
    if systemctl is-active --quiet ${SERVICE_NAME}; then
        echo "      Service reports 'active' but isn't listening -- dumping the last 50 log lines:"
        journalctl -u ${SERVICE_NAME} --no-pager -n 50 || true
    fi
    VERIFY_FAILED=true
fi
if [ "$FRONTEND_CODE" != "200" ]; then
    echo "FAIL: frontend (nginx) did not return 200 (got ${FRONTEND_CODE})."
    VERIFY_FAILED=true
fi

if [ -n "$VM_URL" ]; then
    echo "--- VM box reachability ---"
    if ! curl -m5 -sf "${VM_URL}/api/v1/query?query=up" > /dev/null; then
        echo "FAIL: cannot reach ${VM_URL} from this server (check its Security Group)."
        VERIFY_FAILED=true
    fi
fi

if [ -f "$REPO_DIR/verify_deployment.py" ]; then
    sudo -u "$REAL_USER" "$VENV_DIR/bin/python3" "$REPO_DIR/verify_deployment.py" || VERIFY_FAILED=true
fi

echo "--- Infra hardening checks (soft — logged, do not fail the update) ---"
# update.sh doesn't re-provision infra (that's deploy.sh's job) but a quick
# re-check here catches drift -- e.g. a box deployed with an older deploy.sh
# before this hardening existed, or someone manually disabling swap/cron.
swapon --show | grep -q '/swapfile' || echo "WARNING: swap is not active -- see deploy.sh's [Hardening] Swap section to add it."
[ "$(systemctl show ${SERVICE_NAME} -p OOMScoreAdjust --value 2>/dev/null)" = "-500" ] \
    || echo "WARNING: OOMScoreAdjust is not set to -500 on ${SERVICE_NAME}."
timedatectl status | grep -q "System clock synchronized: yes" \
    || echo "WARNING: system clock is not NTP-synchronized (check 'chronyc sources -v')."
if [ -x "$APP_DIR/scripts/self_check.sh" ]; then
    CRON_LINE="*/5 * * * * $APP_DIR/scripts/self_check.sh >> /var/log/monitoring-hub-selfcheck.log 2>&1"
    sudo -u "$REAL_USER" crontab -l 2>/dev/null | grep -qF "self_check.sh" \
        || ( sudo -u "$REAL_USER" crontab -l 2>/dev/null; echo "$CRON_LINE" ) | sudo -u "$REAL_USER" crontab -
else
    echo "WARNING: $APP_DIR/scripts/self_check.sh not found -- re-run deploy.sh's hardening section or copy it manually."
fi

echo ""
if [ "$VERIFY_FAILED" = true ]; then
    echo "=== UPDATE FINISHED WITH FAILURES — see FAIL lines above ==="
    DEPLOY_FAILED=true
    exit 1
fi

echo "=== Update complete and verified clean ==="
systemctl status ${SERVICE_NAME} --no-pager

# NOTE: this script still does NOT run every file in db/migrations/
# automatically — only the migrations above. A genuinely new, safe
# migration should be written as an apply_*.py in the same idempotent
# style (existence checks + mysqldump backup + dry-run flag) and added
# to BOTH this file's list and deploy.sh's list — keep them in sync.
