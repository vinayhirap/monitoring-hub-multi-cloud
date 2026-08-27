#!/bin/bash
# =============================================================
# CLEAN INSTALL + SETUP — AuroGov Mumbai (PRODUCTION)
# Region: ap-south-1 (Mumbai)
#
# Based on the CloudOps_Main (ap-south-2 / Hyderabad) setup.sh, updated for
# a fresh Mumbai production box. Same DB password and same VM_URL as the
# Hyderabad server on purpose (per 2026-08-26 decision) — this server sits
# in the SAME region as the VictoriaMetrics/YACE test_server box
# (3.109.181.40), so the cross-region public-IP Security Group requirement
# noted below technically no longer applies the same way, but VM_URL is
# left as the public IP (not switched to a private/VPC IP) to keep this
# script identical in shape to the Hyderabad one — revisit if you want to
# tighten this to a private-IP path later.
#
# What this does:
#   - Full fresh install: code, venv, systemd unit, nginx site, DB, schema.
#   - Runs every schema migration this app's current code needs, including
#     the migrations discovered during the 2026-08-25/26 AuroGov Mumbai
#     dashboard incident (resources.resource_id widen, resources.region
#     column, metrics dedup + unique key, provider_credentials type fix).
#   - Verifies app/aws/sts.py actually has the same-account AssumeRole
#     short-circuit committed, instead of assuming it (added 2026-08-26).
#   - Attempts a post-deploy health check (verify_deployment.py) and now
#     WARNS explicitly if that script is missing, instead of silently
#     skipping it (added 2026-08-26 — confirmed via `git log` that this
#     script has never actually existed in the repo).
#
# PUBLIC_IP is set below to this instance's actual public IP (35.154.149.94).
#
# Run this once, as: sudo bash setup.sh
# To also wipe the database (DESTROYS all existing data — only for a true
# from-scratch box, which a brand-new Mumbai server should be):
#   sudo bash setup.sh --wipe-db
# =============================================================
set -e

REPO_URL="https://github.com/vinayhirap/monitoring-hub-multi-cloud.git"
APP_DIR="/opt/monitoring-hub"
REPO_DIR="$APP_DIR/app"
VENV_DIR="$APP_DIR/venv"
SERVICE_NAME="monitoring-hub"

PUBLIC_IP="35.154.149.94"

DB_NAME="monitoring_hub"
DB_USER="monitor"
DB_PASS="root123"
# Same DB password as CloudOps_Main, kept identical on purpose.
# NOTE: root's MySQL password is intentionally never set/changed by this
# script (see step 3 below) — root stays on the default auth_socket auth.

# test_server — VictoriaMetrics + YACE, ap-south-1 (Mumbai). Same box,
# same URL as used by CloudOps_Main — kept identical on purpose. This new
# server is now in the SAME region as this box, so the cross-region public
# internet routing note below is technically less critical here than it
# was for CloudOps_Main in ap-south-2, but the SG rule (inbound 80 from
# THIS server's IP) must still exist on 3.109.181.40's own Security Group
# regardless of region. CONFIRMED 2026-08-26: this rule was in fact missing
# on a fresh setup and caused every VM query/push to hang until connect-
# timeout (5-10s per call) rather than fail fast — see the health-check
# step near the end of this script, which now warns about this explicitly
# if verify_deployment.py isn't present to catch it automatically.
#
# VM_URL has NO port because nginx on the VM box reverse-proxies
# 80 -> 127.0.0.1:8428 (VictoriaMetrics' real port).
VM_URL="http://3.109.181.40"
AWS_DEFAULT_REGION="ap-south-1"

REAL_USER="${SUDO_USER:-$USER}"

WIPE_DB=false
if [ "$1" == "--wipe-db" ]; then
    WIPE_DB=true
    echo "WARNING: --wipe-db passed. The existing ${DB_NAME} database will be"
    echo "DROPPED and recreated empty. This destroys any existing account"
    echo "config, users, alerts, and history on this server."
    read -p "Type 'wipe' to confirm: " CONFIRM
    if [ "$CONFIRM" != "wipe" ]; then
        echo "Not confirmed — aborting."
        exit 1
    fi
fi

echo "=== [1/10] Stop and remove old app install (code/service/nginx only) ==="
systemctl stop ${SERVICE_NAME} 2>/dev/null || true
systemctl disable ${SERVICE_NAME} 2>/dev/null || true
rm -f /etc/systemd/system/${SERVICE_NAME}.service
systemctl daemon-reload
rm -f /etc/nginx/sites-enabled/${SERVICE_NAME} /etc/nginx/sites-available/${SERVICE_NAME}
if [ -d "$REPO_DIR" ]; then
    echo "Removing old app code + venv at ${APP_DIR} (DB is untouched)."
    rm -rf "$REPO_DIR" "$VENV_DIR"
fi

echo "=== [2/10] System packages ==="
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip mysql-server redis-server nginx git curl

echo "=== [3/10] MySQL + Redis ==="
systemctl enable --now mysql
systemctl enable --now redis-server

# NOTE: we deliberately never touch root's password/auth plugin here.
# A fresh Ubuntu mysql-server ships with root on auth_socket (passwordless
# via 'sudo mysql'), and changing that to a password-based plugin caused a
# full root lockout on a previous run. The app only ever needs the
# 'monitor' user below, so root is left exactly as the package sets it.
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

echo "=== [4/10] Clone repo ==="
mkdir -p "$APP_DIR"
chown "$REAL_USER":"$REAL_USER" "$APP_DIR"
sudo -u "$REAL_USER" git clone "$REPO_URL" "$REPO_DIR"
cd "$REPO_DIR"

echo "--- Checking app/aws/sts.py for the same-account AssumeRole short-circuit ---"
# Same check as update.sh — see that script's header comment for the full
# root-cause story. On a genuinely fresh clone this should already be
# committed; this just verifies that instead of assuming it.
if grep -q "No STS call is actually needed" app/aws/sts.py 2>/dev/null; then
    echo "OK: same-account short-circuit present in app/aws/sts.py."
else
    echo "WARNING: app/aws/sts.py does NOT have the same-account AssumeRole"
    echo "short-circuit. If the AWS account you're about to add for this"
    echo "server IS this server's own account, discovery and metrics for it"
    echo "will silently fail with AssumeRole/AccessDenied errors."
    echo "Get this committed upstream into app/aws/sts.py. Stopgap for now"
    echo "(run after step 5 below, once the venv exists):"
    echo "  sudo -u $REAL_USER $VENV_DIR/bin/python3 apply_same_account_role_fix.py"
    echo "  sudo -u $REAL_USER $VENV_DIR/bin/python3 apply_same_account_role_fix_v2.py"
fi

# db_backups/ is written to by every apply_*.py migration script below
# (each takes a mysqldump backup before altering). Pre-create it with the
# right ownership so a stray root-owned dir can't block a later run as
# $REAL_USER — bit us during the 2026-08-24 metric-catalog incident.
mkdir -p "$REPO_DIR/db_backups"
chown -R "$REAL_USER":"$REAL_USER" "$REPO_DIR/db_backups"

echo "=== [5/10] Python venv + dependencies ==="
sudo -u "$REAL_USER" python3 -m venv "$VENV_DIR"
sudo -u "$REAL_USER" "$VENV_DIR/bin/pip" install --upgrade pip
sudo -u "$REAL_USER" "$VENV_DIR/bin/pip" install -r requirements.txt

echo "=== [6/10] Environment file (.env) ==="
JWT_SECRET=$(openssl rand -hex 32)
# Fernet key for app/credentials.py (encrypts Azure/GCP secrets at rest).
# Generated fresh for THIS server — intentionally NOT copied from
# CloudOps_Main, since a shared encryption key across servers is not
# something to replicate; each server's stored Azure/GCP secrets should
# only be decryptable by that same server.
CREDENTIAL_ENCRYPTION_KEY=$("$VENV_DIR/bin/python3" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

cat > "$REPO_DIR/.env" <<EOF
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=${DB_USER}
DB_PASSWORD=${DB_PASS}
DB_NAME=${DB_NAME}
REDIS_HOST=127.0.0.1
REDIS_PORT=6379

APP_ENV=production
APP_PORT=8000

AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}
VM_URL=${VM_URL}

JWT_SECRET=${JWT_SECRET}
CREDENTIAL_ENCRYPTION_KEY=${CREDENTIAL_ENCRYPTION_KEY}
# Set to true once this is served over HTTPS
COOKIE_SECURE=false
CORS_ALLOWED_ORIGINS=http://${PUBLIC_IP}
EOF
chown "$REAL_USER":"$REAL_USER" "$REPO_DIR/.env"
chmod 600 "$REPO_DIR/.env"
echo "Generated JWT_SECRET and CREDENTIAL_ENCRYPTION_KEY — .env is the only"
echo "copy of both. Back it up (off this box) if you care about existing"
echo "sessions and any stored Azure/GCP secrets surviving a future redeploy."

echo "=== [7/10] Base schema (fresh DB only) ==="
# db/schema.sql in this repo is STALE and doesn't match the actual code.
# db_schema_only.sql at the repo root is a mysqldump of a working dev DB
# and is the correct starting point, but it predates several migrations
# (see step 8) — those run unconditionally afterwards regardless of
# whether this base import ran, so an existing (non-fresh) DB still ends
# up fully migrated. It's UTF-16 with CRLF line endings, needs converting.
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
    echo "import and user seeding. Migrations in step 8 still run against it."
fi

echo "--- Stopping backend service before migrations (harmless if not running yet) ---"
# Kept for parity with update.sh and safety on a re-run against an existing
# install (e.g. setup.sh run again on a box where the service is already
# up) — see update.sh's header comment for the full root-cause story on
# why a running service during migrations caused a lock pile-up incident.
systemctl stop ${SERVICE_NAME} 2>/dev/null || true

echo "=== [8/10] Schema migrations (always run, safe to repeat) ==="
# Every script below is existence-checked (safe to re-run) and takes its
# own mysqldump backup before altering anything.
#
# The fk-type-fix and metrics-dedup scripts are included here as a safety
# net for a TRUE fresh install too: apply_fresh_schema_migrations.py's
# provider_credentials CREATE TABLE was already corrected to BIGINT in the
# committed source as of 2026-08-26, so on a genuinely fresh clone the
# fk-type-fix script should just print "Already applied" and exit — same
# for the metrics dedup script on an empty metrics table. They're run
# anyway in case this ever runs against a DB that isn't actually fresh
# (e.g. a restored backup).
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
    "safety net: provider_credentials.aws_account_id INT->BIGINT (should be a no-op on a fresh clone)"
run_migration apply_fresh_schema_migrations.py \
    "003/004/005/010(table)/011(widen): metric_catalog full columns + account_metric_selections, provider_credentials table, password_reset_tokens, metrics dedup key, resources.resource_id widen"
run_migration apply_resources_region_column_fix.py \
    "resources.region column + backfill from aws_accounts.default_region"
run_migration apply_metrics_dedup_fix.py \
    "safety net: metrics duplicate cleanup + uniq_metrics_resource_metric (now checks index_exists first — fixed 2026-08-26, was not actually idempotent before)"
run_migration apply_access_scopes_migration.py \
    "011: access_scopes table (Phase 1 authorization)"
run_migration apply_alert_evaluation_hardening_migration.py \
    "012: alerts.last_seen_at/healthy_streak + alert_pending table"
run_migration scripts/seed_metric_catalog.py \
    "seed: metric_catalog curated + directory entries"

echo "=== [9/10] Build frontend ==="
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs
cd "$REPO_DIR/frontend"
sudo -u "$REAL_USER" npm install
sudo -u "$REAL_USER" npm run build

echo "=== [10/10] systemd service + nginx ==="
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
nginx -t && systemctl restart nginx

# --- Post-deploy health check -------------------------------------------
echo ""
echo "=== Post-deploy health check ==="
if [ -f "$REPO_DIR/verify_deployment.py" ]; then
    sudo -u "$REAL_USER" "$VENV_DIR/bin/python3" "$REPO_DIR/verify_deployment.py" || true
else
    echo "WARNING: verify_deployment.py not found in $REPO_DIR."
    echo "This script has been referenced as the automated post-deploy health"
    echo "check (VM-box SG rule, schema drift, same-account AssumeRole) but"
    echo "has never actually existed in this repo's history — confirmed via"
    echo "'git log -- verify_deployment.py' returning empty on 2026-08-26."
    echo "Either get it written and committed, or verify manually right now:"
    echo "  1. VM box reachable + its Security Group allows THIS server's IP"
    echo "     (${PUBLIC_IP}) on port 80 — confirmed missing on a prior setup:"
    echo "       curl -m5 ${VM_URL}/api/v1/query?query=up"
    echo "  2. Metrics landing (allow a few minutes after this restart):"
    echo "       mysql -u${DB_USER} -p ${DB_NAME} -e \"SELECT COUNT(*) FROM metrics;\""
    echo "  3. app/aws/sts.py same-account short-circuit — see check earlier in this run."
fi

echo ""
echo "=== Setup complete ==="
echo "App:      http://${PUBLIC_IP}/"
if [ "$TABLE_COUNT" -lt 2 ] 2>/dev/null || [ "$WIPE_DB" = true ]; then
    echo "Logins:   admin/admin123, editor/editor123, viewer/viewer123  <- change these"
else
    echo "Logins:   existing users preserved (DB was not wiped)"
fi
echo "VM_URL:   ${VM_URL}  (test_server, ap-south-1 Mumbai — same region as this server)"
echo "Backend:  systemctl status ${SERVICE_NAME}"
echo ""
echo "IMPORTANT: open inbound port 80 (and 22 for SSH) on THIS instance's"
echo "AWS Security Group, or the app won't be reachable from outside."
echo "IMPORTANT: confirm 3.109.181.40's OWN Security Group (attached to that"
echo "instance, not this one) allows inbound from ${PUBLIC_IP} on port 80."
echo "This was confirmed MISSING on a prior AuroGov Mumbai setup and caused"
echo "every VM query/push to hang until connect-timeout instead of failing"
echo "fast — the health check above will flag it if verify_deployment.py"
echo "exists, but confirm it yourself with the curl command above regardless."
echo "IMPORTANT: if the AuroGov Mumbai AWS account being monitored is THIS"
echo "server's OWN account, do NOT set a role_arn that points back at this"
echo "instance's own IAM role — leave role_arn empty for that account row"
echo "so the collector uses the instance profile directly (same-account"
echo "AssumeRole always fails AccessDenied; see app/aws/sts.py's same-"
echo "account short-circuit and apply_same_account_role_fix.py's docstring"
echo "for the full story from the 2026-08-26 incident)."
echo "IMPORTANT: if any WARNING was printed in step 8 or the health check"
echo "above, some pages (Accounts, Metric Catalog, Settings->Credentials,"
echo "password reset, onboarding wizard, or the dashboard graphs) will 500"
echo "or misbehave until that item is fixed manually:"
echo "  sudo -u ${REAL_USER} ${VENV_DIR}/bin/python3 ${REPO_DIR}/<script>.py"
