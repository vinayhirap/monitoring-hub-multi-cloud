#!/bin/bash
# =============================================================
# DEPLOY — clean install of Monitoring Hub (AuroGov Mumbai, ap-south-1)
# Run as: sudo bash deploy.sh
# Wipe existing DB too (destroys all data — only for a true fresh box):
#   sudo bash deploy.sh --wipe-db          (asks for typed confirmation)
#   sudo bash deploy.sh --wipe-db --yes    (skips the prompt, for CI/automation)
#
# This is the "no manual intervention" version. Every landmine hit in
# deployment history up to 2026-09-04 is fixed AT THE SOURCE here, not
# worked around after the fact:
#
#   1. PUBLIC_IP is now AUTO-DETECTED via EC2 IMDSv2 instead of hardcoded.
#      Root cause of the recurring manual `sed -i PUBLIC_IP=...` step:
#      the IP was hardcoded per-server and had to be hand-edited every
#      time the script was copied to a new box or a box got a new IP.
#      Now it just asks the instance.
#
#   2. bcrypt is pinned to 4.0.1 in requirements.txt (fixed at the repo
#      level, verified here too). bcrypt>=4.1 removed the __about__
#      module that passlib==1.7.4 reads to detect its backend version —
#      installing it breaks every login/password-hash call with
#      AttributeError: module 'bcrypt' has no attribute '__about__'.
#      This was previously live-patched with a shim on the server and
#      reverted (2026-09-04) — the pin is the actual permanent fix.
#
#   3. The org-group + permission RBAC migrations
#      (apply_org_group_rbac.py, apply_permission_rbac_system.py,
#      apply_permission_rbac_migration.py) are now in THIS script's
#      migration list too, not just update.sh's. Root cause of the
#      "GET /api/users -> 500, user_group_memberships doesn't exist"
#      incident: a box brought up via setup.sh's original migration list
#      never got these tables at all, because they were only ever added
#      to update.sh, not here.
#
#   4. Ends with a HARD verification block that exits non-zero if any
#      table/column/code fix this app depends on is actually missing —
#      not just a printed WARNING that's easy to miss in a long log.
#      A non-zero exit here means "do not consider this deploy done."
#
#   5. Network flakiness pre-empted instead of reacted to: ForceIPv4 is
#      set before the first apt call (previously only discovered as a
#      fix mid-incident after apt/curl hung), and apt/npm/pip calls are
#      wrapped with a retry.
#
#   6. A trap on ERR/EXIT makes a best-effort attempt to leave the
#      service running and nginx valid even if a later step fails, so a
#      failed deploy doesn't also take down whatever was working before.
# =============================================================
set -e
set -o pipefail

REPO_URL="https://github.com/vinayhirap/monitoring-hub-multi-cloud.git"
APP_DIR="/opt/monitoring-hub"
REPO_DIR="$APP_DIR/app"
VENV_DIR="$APP_DIR/venv"
SERVICE_NAME="monitoring-hub"

DB_NAME="monitoring_hub"
DB_USER="monitor"
DB_PASS="root123"
# Same DB password across all boxes on purpose. Root's MySQL password is
# deliberately never touched (stays on auth_socket / passwordless sudo
# mysql) — changing that caused a full root lockout on a previous run.

# test_server — VictoriaMetrics + YACE, ap-south-1 (Mumbai).
# VM_URL has NO port: nginx on that box reverse-proxies 80 -> 127.0.0.1:8428.
VM_URL="http://3.109.181.40"
AWS_DEFAULT_REGION="ap-south-1"

REAL_USER="${SUDO_USER:-$USER}"

WIPE_DB=false
AUTO_YES=false
for arg in "$@"; do
    case "$arg" in
        --wipe-db) WIPE_DB=true ;;
        --yes) AUTO_YES=true ;;
    esac
done

if [ "$WIPE_DB" = true ] && [ "$AUTO_YES" = false ]; then
    echo "WARNING: --wipe-db passed. The existing ${DB_NAME} database will be"
    echo "DROPPED and recreated empty. This destroys any existing account"
    echo "config, users, alerts, and history on this server."
    read -p "Type 'wipe' to confirm: " CONFIRM
    if [ "$CONFIRM" != "wipe" ]; then
        echo "Not confirmed — aborting."
        exit 1
    fi
fi

# --- Best-effort cleanup on any failure: never leave the box worse off ---
DEPLOY_FAILED=false
on_exit() {
    if [ "$DEPLOY_FAILED" = true ]; then
        echo ""
        echo "=== DEPLOY FAILED — attempting to leave service/nginx in a running state ==="
        systemctl restart ${SERVICE_NAME} 2>/dev/null || true
        nginx -t 2>/dev/null && systemctl restart nginx 2>/dev/null || true
        echo "Re-run this script after fixing the error above. Do NOT treat this box as deployed."
    fi
}
trap on_exit EXIT
trap 'DEPLOY_FAILED=true' ERR

# --- Network resilience: fix BEFORE the first apt call, not after a hang ---
echo "=== [0/11] Network pre-flight ==="
echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4
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
retry curl -m5 -sI http://security.ubuntu.com > /dev/null

# --- Auto-detect this instance's public IP via IMDSv2 (no more hand-editing) ---
echo "=== [1/11] Detect public IP (IMDSv2) ==="
IMDS_TOKEN=$(retry curl -sf -m5 -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
PUBLIC_IP=$(curl -sf -m5 -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
    http://169.254.169.254/latest/meta-data/public-ipv4)
if [ -z "$PUBLIC_IP" ]; then
    echo "ERROR: could not auto-detect this instance's public IP via IMDSv2."
    echo "Either this isn't an EC2 instance, or the instance has no public IP"
    echo "(private-subnet box behind a load balancer) — in that case set"
    echo "PUBLIC_IP manually below this line and re-run."
    exit 1
fi
echo "Detected PUBLIC_IP=${PUBLIC_IP}"

echo "=== [2/11] Stop and remove old app install (code/service/nginx only) ==="
systemctl stop ${SERVICE_NAME} 2>/dev/null || true
systemctl disable ${SERVICE_NAME} 2>/dev/null || true
rm -f /etc/systemd/system/${SERVICE_NAME}.service
systemctl daemon-reload
rm -f /etc/nginx/sites-enabled/${SERVICE_NAME} /etc/nginx/sites-available/${SERVICE_NAME}
if [ -d "$REPO_DIR" ]; then
    echo "Removing old app code + venv at ${APP_DIR} (DB is untouched)."
    rm -rf "$REPO_DIR" "$VENV_DIR"
fi

echo "=== [3/11] System packages ==="
retry apt update
apt upgrade -y
apt install -y python3 python3-venv python3-pip mysql-server redis-server nginx git curl

echo "=== [4/11] MySQL + Redis ==="
systemctl enable --now mysql
systemctl enable --now redis-server

if ! sudo mysql -e "SELECT 1;" > /dev/null 2>&1; then
    echo "ERROR: 'sudo mysql' can't connect. MySQL is likely already in a"
    echo "broken/locked-out state from a previous run. Fix that first,"
    echo "then re-run this script."
    exit 1
fi

if [ "$WIPE_DB" = true ]; then
    sudo mysql -e "DROP DATABASE IF EXISTS ${DB_NAME};"
fi

sudo mysql -e "
CREATE DATABASE IF NOT EXISTS ${DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASS}';
GRANT ALL PRIVILEGES ON ${DB_NAME}.* TO '${DB_USER}'@'localhost';
FLUSH PRIVILEGES;
"

echo "=== [5/11] Clone repo ==="
mkdir -p "$APP_DIR"
chown "$REAL_USER":"$REAL_USER" "$APP_DIR"
retry sudo -u "$REAL_USER" git clone "$REPO_URL" "$REPO_DIR"
cd "$REPO_DIR"

mkdir -p "$REPO_DIR/db_backups"
chown -R "$REAL_USER":"$REAL_USER" "$REPO_DIR/db_backups"

echo "=== [6/11] Python venv + dependencies ==="
sudo -u "$REAL_USER" python3 -m venv "$VENV_DIR"
sudo -u "$REAL_USER" "$VENV_DIR/bin/pip" install --upgrade pip
retry sudo -u "$REAL_USER" "$VENV_DIR/bin/pip" install -r requirements.txt

if ! grep -q "^bcrypt==4\.0\.1" requirements.txt; then
    echo "WARNING: requirements.txt no longer pins bcrypt==4.0.1 — the"
    echo "passlib/__about__ crash landmine may have regressed. See the"
    echo "comment above requirements.txt's bcrypt line for the full story."
fi

echo "=== [7/11] Environment file (.env) ==="
JWT_SECRET=$(openssl rand -hex 32)
tee "$REPO_DIR/.env" > /dev/null <<EOF
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=${DB_USER}
DB_PASSWORD=${DB_PASS}
DB_NAME=${DB_NAME}
JWT_SECRET=${JWT_SECRET}
VM_URL=${VM_URL}
AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}
PUBLIC_IP=${PUBLIC_IP}
EOF
chown "$REAL_USER":"$REAL_USER" "$REPO_DIR/.env"
chmod 600 "$REPO_DIR/.env"

echo "=== [8/11] Base schema import (fresh DB only) ==="
TABLE_COUNT=$(mysql -u"${DB_USER}" -p"${DB_PASS}" "${DB_NAME}" -e "SHOW TABLES;" 2>/dev/null | wc -l)
if [ "$TABLE_COUNT" -lt 2 ]; then
    iconv -f utf-16 -t utf-8 "$REPO_DIR/db_schema_only.sql" | sed 's/\r$//' > /tmp/schema_correct.sql
    mysql -u"${DB_USER}" -p"${DB_PASS}" "${DB_NAME}" < /tmp/schema_correct.sql
    rm -f /tmp/schema_correct.sql

    sudo -u "$REAL_USER" "$VENV_DIR/bin/python3" - <<PYEOF
import bcrypt, mysql.connector
users = [("admin", "admin123", "admin"), ("editor", "editor123", "editor"), ("viewer", "viewer123", "viewer")]
conn = mysql.connector.connect(host="127.0.0.1", port=3306, user="${DB_USER}", password="${DB_PASS}", database="${DB_NAME}")
cur = conn.cursor()
for username, pw, role in users:
    h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    cur.execute("INSERT INTO users (username, password, role, active) VALUES (%s,%s,%s,1)", (username, h, role))
conn.commit()
cur.close(); conn.close()
print("Seeded users: admin/admin123, editor/editor123, viewer/viewer123")
PYEOF
else
    echo "DB already has tables (TABLE_COUNT=${TABLE_COUNT}) — skipping base schema"
    echo "import and user seeding. Migrations below still run against it."
fi

echo "--- Stopping backend service before migrations (harmless if not running yet) ---"
systemctl stop ${SERVICE_NAME} 2>/dev/null || true

echo "=== [9/11] Schema migrations (idempotent, safe to repeat) ==="
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
    "safety net: metrics duplicate cleanup + uniq_metrics_resource_metric"
run_migration apply_access_scopes_migration.py \
    "011: access_scopes table (Phase 1 authorization)"
run_migration apply_alert_evaluation_hardening_migration.py \
    "012: alerts.last_seen_at/healthy_streak + alert_pending table"
run_migration apply_org_group_rbac.py \
    "013: org_groups/group_policies/user_group_memberships -- required or ANY non-admin login 500s on scoped endpoints"
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
# The curated run_migration list above handles code+schema patches that
# ship as root-level apply_*.py scripts. db/migrations/*.sql is a SEPARATE,
# smaller set of raw numbered SQL files with no tracking of its own --
# that gap is exactly how 014_user_email_column.sql shipped in the repo
# but was never applied to a production DB (see monitoring_hub_mumbai_rca.md
# for the incident). On a fresh box, the schema import + migrations above
# already bring the DB to equivalent state, so we baseline (record as
# applied, without re-running raw SQL that could conflict with what the
# apply_*.py scripts above already created) rather than apply. Any FILE
# added to db/migrations/ AFTER this deploy will show as genuinely pending
# and get picked up automatically by update.sh's `migrate.py apply
# --all-pending` -- no more hand-editing this script's migration list for
# schema-only changes.
sudo -u "$REAL_USER" "$VENV_DIR/bin/python3" migrate.py baseline --all-except-rollbacks

echo "=== [10/11] Build frontend ==="
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | cut -d. -f1 | tr -d v)" -lt 20 ]; then
    retry bash -c "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -"
    apt install -y nodejs
fi
cd "$REPO_DIR/frontend"
retry sudo -u "$REAL_USER" npm install
sudo -u "$REAL_USER" npm run build

# Restore cwd to $REPO_DIR -- everything below (systemd unit paths are
# absolute so this didn't matter yet, but the verification block's
# `grep ... app/aws/sts.py` etc. are RELATIVE paths) must run from
# $REPO_DIR, not $REPO_DIR/frontend where the npm build step left us.
# Without this, every one of those checks silently greps a path that
# never exists (frontend/app/aws/sts.py) and reports a false FAIL even
# when the real file at $REPO_DIR/app/aws/sts.py is fine.
cd "$REPO_DIR"

echo "=== [11/11] systemd service + nginx ==="
tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=CloudOps Monitoring Hub (AuroGov Mumbai)
After=network.target mysql.service redis-server.service

[Service]
Type=simple
User=${REAL_USER}
WorkingDirectory=${REPO_DIR}
Environment="PATH=${VENV_DIR}/bin"
EnvironmentFile=${REPO_DIR}/.env
ExecStart=${VENV_DIR}/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}

sed "s/server_name _;/server_name ${PUBLIC_IP};/; s#/opt/monitoring-hub/app/frontend/dist#${REPO_DIR}/frontend/dist#" \
    "$REPO_DIR/deploy/nginx.conf" > /etc/nginx/sites-available/${SERVICE_NAME}
ln -sf /etc/nginx/sites-available/${SERVICE_NAME} /etc/nginx/sites-enabled/${SERVICE_NAME}
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx

# --- HARD verification: exit non-zero if anything critical is actually missing ---
echo ""
echo "=== Post-deploy verification (hard gate — non-zero exit means NOT done) ==="
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
    echo "      Any monitored account in THIS server's own AWS account will"
    echo "      silently fail EC2/RDS/S3/Lambda/ECS/EBS discovery with AccessDenied."
    VERIFY_FAILED=true
fi

if ! grep -q "max_workers=min(len(accounts), 8) or 1" app/api/live_data.py 2>/dev/null; then
    echo "FAIL: app/api/live_data.py is missing the max_workers>0 fix."
    echo "      /api/live/accounts will crash with ValueError when there are 0 active accounts."
    VERIFY_FAILED=true
fi

if ! grep -q "GROUP_LEVEL_ROLE = " app/auth/authorization.py 2>/dev/null; then
    echo "FAIL: app/auth/authorization.py is missing GROUP_LEVEL_ROLE."
    echo "      Every POST /api/groups/{id}/members call will crash with"
    echo "      AttributeError, silently swallowed by the frontend's .catch(() => {})."
    VERIFY_FAILED=true
fi

check_table "org_groups"
ORG_GROUP_COUNT=$(mysql -u"${DB_USER}" -p"${DB_PASS}" "${DB_NAME}" -e "SELECT COUNT(*) FROM org_groups;" 2>/dev/null | tail -1)
if [ "${ORG_GROUP_COUNT:-0}" -lt 3 ]; then
    echo "FAIL: org_groups has ${ORG_GROUP_COUNT:-0} rows, expected at least 3 (L1 Monitoring / L2 Operations / L3 Administrator)."
    VERIFY_FAILED=true
fi

if [ ${#MIGRATION_FAILURES[@]} -gt 0 ]; then
    echo "FAIL: these migration scripts reported errors: ${MIGRATION_FAILURES[*]}"
    VERIFY_FAILED=true
fi

echo "--- App endpoints (from the app server itself) ---"

if ! systemctl is-active --quiet ${SERVICE_NAME}; then
    echo "FAIL: ${SERVICE_NAME} is not active after restart -- dumping the last 50 log lines:"
    journalctl -u ${SERVICE_NAME} --no-pager -n 50 || true
    VERIFY_FAILED=true
fi

sleep 3
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

echo "--- VM box reachability (Security Group check) ---"
if ! curl -m5 -sf "${VM_URL}/api/v1/query?query=up" > /dev/null; then
    echo "FAIL: cannot reach ${VM_URL} from this server."
    echo "      Confirm 3.109.181.40's OWN Security Group allows inbound from"
    echo "      ${PUBLIC_IP} on port 80 — this exact rule was found missing"
    echo "      on a prior AuroGov Mumbai setup and caused every VM query/push"
    echo "      to hang until connect-timeout instead of failing fast."
    VERIFY_FAILED=true
fi

if [ -f "$REPO_DIR/verify_deployment.py" ]; then
    sudo -u "$REAL_USER" "$VENV_DIR/bin/python3" "$REPO_DIR/verify_deployment.py" || VERIFY_FAILED=true
fi

echo ""
if [ "$VERIFY_FAILED" = true ]; then
    echo "=== DEPLOY FINISHED WITH FAILURES — see FAIL lines above ==="
    DEPLOY_FAILED=true
    exit 1
fi

echo "=== Deploy complete and verified clean ==="
echo "App:      http://${PUBLIC_IP}/"
if [ "$TABLE_COUNT" -lt 2 ] || [ "$WIPE_DB" = true ]; then
    echo "Logins:   admin/admin123, editor/editor123, viewer/viewer123  <- change these"
else
    echo "Logins:   existing users preserved (DB was not wiped)"
fi
echo "VM_URL:   ${VM_URL}"
echo "Backend:  systemctl status ${SERVICE_NAME}"
echo ""
echo "REMINDER: open inbound port 80 (and 22 for SSH) on THIS instance's own"
echo "AWS Security Group, or the app won't be reachable from outside."
echo "REMINDER: if a monitored AWS account is THIS server's own account,"
echo "leave that account row's role_arn empty so the collector uses the"
echo "instance profile directly instead of a same-account AssumeRole (which"
echo "always fails AccessDenied)."
