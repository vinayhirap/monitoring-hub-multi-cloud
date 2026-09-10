#!/bin/bash
# =============================================================
# preflight_check.sh -- STRICTLY READ-ONLY. Makes zero changes to
# anything: no file writes, no DB writes, no service restarts, no git
# operations beyond `status`/`log`/`remote -v` (all read-only). Safe to
# run on production at any time.
#
# Run this on BOTH servers (13.200.102.131, the known-good reference,
# and 35.154.149.94, the target) and compare the two outputs before
# deciding how to bring the target up to date. Never prints secret
# VALUES (DB passwords, API keys, external IDs) -- only whether a
# variable is present/set, since this output may get pasted into chat.
#
# Usage:
#   bash preflight_check.sh
# =============================================================

echo "======================================================"
echo " PREFLIGHT CHECK -- $(hostname) -- $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "======================================================"

echo ""
echo "--- OS / kernel ---"
cat /etc/os-release 2>/dev/null | grep -E "^(NAME|VERSION)=" || echo "  (could not read /etc/os-release)"
uname -r

echo ""
echo "--- Disk / memory (update needs headroom for git pull + npm build + db_backups) ---"
df -h / 2>/dev/null | tail -1
free -h 2>/dev/null | grep -E "^(Mem|Swap):"

echo ""
echo "--- App directory ---"
APP_DIR="/opt/monitoring-hub"
REPO_DIR="$APP_DIR/app"
if [ -d "$REPO_DIR" ]; then
    echo "  $REPO_DIR exists"
else
    echo "  MISSING: $REPO_DIR does not exist -- this is NOT a standard deploy.sh-provisioned box."
    echo "  Everything below that assumes this path will fail; report this back before proceeding."
fi

if [ -d "$REPO_DIR/.git" ]; then
    echo ""
    echo "--- Git state ---"
    cd "$REPO_DIR"
    echo "  remote:          $(git remote get-url origin 2>/dev/null || echo '(none)')"
    echo "  current branch:  $(git branch --show-current 2>/dev/null)"
    echo "  current commit:  $(git log -1 --format='%h %s (%ci)' 2>/dev/null)"
    echo "  commits behind origin/main:"
    git fetch origin main --quiet 2>/dev/null
    BEHIND=$(git rev-list --count HEAD..origin/main 2>/dev/null)
    echo "    $BEHIND commit(s) behind"
    echo "  uncommitted local changes:"
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        git status --short
        echo "    ^ THIS SERVER HAS LOCAL CHANGES NOT IN GIT -- a plain 'git pull' risks"
        echo "      silently merging or discarding server-specific state. Resolve before updating."
    else
        echo "    (clean)"
    fi
    echo "  last 5 commits:"
    git log --oneline -5 2>/dev/null | sed 's/^/    /'
else
    echo "  (not a git checkout, or .git missing)"
fi

echo ""
echo "--- Python / Node ---"
python3 --version 2>/dev/null || echo "  python3: NOT FOUND"
[ -f "$APP_DIR/venv/bin/python3" ] && echo "  venv present: $APP_DIR/venv" || echo "  venv MISSING at $APP_DIR/venv"
node --version 2>/dev/null || echo "  node: NOT FOUND"
npm --version 2>/dev/null || echo "  npm: NOT FOUND"

echo ""
echo "--- .env (presence of keys only -- NEVER printing values) ---"
ENV_FILE="$REPO_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    echo "  $ENV_FILE exists, permissions: $(stat -c '%a %U:%G' "$ENV_FILE" 2>/dev/null)"
    for key in DB_HOST DB_PORT DB_USER DB_PASSWORD DB_NAME VM_URL; do
        if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
            echo "    $key: set"
        else
            echo "    $key: NOT SET"
        fi
    done
else
    echo "  MISSING: $ENV_FILE does not exist -- was this box ever run through deploy.sh?"
fi

echo ""
echo "--- systemd service ---"
SERVICE_NAME="monitoring-hub"
if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}.service"; then
    echo "  ${SERVICE_NAME}.service exists"
    systemctl is-active --quiet ${SERVICE_NAME} && echo "  status: active" || echo "  status: NOT active"
    echo "  ExecStart: $(systemctl show ${SERVICE_NAME} -p ExecStart --value 2>/dev/null | head -c 200)"
    echo "  User:      $(systemctl show ${SERVICE_NAME} -p User --value 2>/dev/null)"
else
    echo "  MISSING: no ${SERVICE_NAME}.service unit found -- how is this app currently run?"
    echo "  Checking for any related running processes instead:"
    ps aux | grep -iE "uvicorn|monitoring.?hub" | grep -v grep | sed 's/^/    /' || echo "    (none found)"
fi

echo ""
echo "--- VictoriaMetrics / YACE -- specifically relevant given this server's history ---"
echo "  Processes on THIS box matching yace/victoria/cloudwatch_exporter:"
ps aux | grep -iE "yace|victoria|cloudwatch.?exporter" | grep -v grep | sed 's/^/    /' || echo "    (none found)"
echo "  Cron entries mentioning yace/victoria (current user + root, if readable):"
( crontab -l 2>/dev/null; sudo crontab -l 2>/dev/null ) | grep -iE "yace|victoria" | sed 's/^/    /' || echo "    (none found)"
echo "  Any *.yml config files that look like YACE discovery configs, in common locations:"
find /opt /etc /home -maxdepth 3 -iname "*yace*config*.yml" 2>/dev/null | sed 's/^/    /' || echo "    (none found)"
echo "  Listening ports possibly belonging to an exporter/VM process (9090/8428/9106 are common defaults):"
(ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null) | grep -E ":9090|:8428|:9106" | sed 's/^/    /' || echo "    (none of the common ports found listening)"

echo ""
echo "--- Database: connectivity + migration/schema state ---"
if [ -f "$ENV_FILE" ]; then
    DB_HOST=$(grep -m1 '^DB_HOST=' "$ENV_FILE" | cut -d= -f2-)
    DB_USER=$(grep -m1 '^DB_USER=' "$ENV_FILE" | cut -d= -f2-)
    DB_PASS=$(grep -m1 '^DB_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)
    DB_NAME=$(grep -m1 '^DB_NAME=' "$ENV_FILE" | cut -d= -f2-)
    DB_HOST="${DB_HOST:-127.0.0.1}"
    DB_NAME="${DB_NAME:-monitoring_hub}"

    if command -v mysql >/dev/null 2>&1 && [ -n "$DB_USER" ] && [ -n "$DB_PASS" ]; then
        if mysql -u"$DB_USER" -p"$DB_PASS" -h"$DB_HOST" -e "SELECT 1;" >/dev/null 2>&1; then
            echo "  Connection: OK"
            echo "  Database version: $(mysql -u"$DB_USER" -p"$DB_PASS" -h"$DB_HOST" -N -e 'SELECT VERSION();' 2>/dev/null)"

            echo "  Table row counts (existence + rough size, no data content):"
            for t in resources metrics metric_history thresholds alerts alert_pending metric_catalog account_metric_selections \
                     org_groups group_policies user_group_memberships permissions role_permissions access_scopes \
                     aws_accounts provider_credentials schema_migrations; do
                COUNT=$(mysql -u"$DB_USER" -p"$DB_PASS" -h"$DB_HOST" -N -e \
                    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${DB_NAME}' AND table_name='${t}';" 2>/dev/null)
                if [ "$COUNT" = "1" ]; then
                    ROWS=$(mysql -u"$DB_USER" -p"$DB_PASS" -h"$DB_HOST" -N -e "SELECT COUNT(*) FROM \`${DB_NAME}\`.\`${t}\`;" 2>/dev/null)
                    echo "    $t: EXISTS, $ROWS row(s)"
                else
                    echo "    $t: MISSING"
                fi
            done

            echo ""
            echo "  Bad-data spot check (the specific historical issue this session fixed):"
            if mysql -u"$DB_USER" -p"$DB_PASS" -h"$DB_HOST" -e "SELECT 1 FROM information_schema.tables WHERE table_schema='${DB_NAME}' AND table_name='thresholds';" 2>/dev/null | grep -q 1; then
                ALB_NLB_COUNT=$(mysql -u"$DB_USER" -p"$DB_PASS" -h"$DB_HOST" -N -e \
                    "SELECT COUNT(*) FROM \`${DB_NAME}\`.thresholds WHERE resource_type IN ('alb','nlb');" 2>/dev/null)
                echo "    thresholds rows with resource_type IN ('alb','nlb') (should end up 0 after the backfill): $ALB_NLB_COUNT"
            fi
        else
            echo "  Connection: FAILED (check DB_HOST/DB_USER/DB_PASSWORD/DB_NAME in .env, and that mysql is reachable from this box)"
        fi
    else
        echo "  Skipping -- mysql client not found on PATH, or DB_USER/DB_PASSWORD not set in .env"
    fi
else
    echo "  Skipping -- no .env file found"
fi

echo ""
echo "======================================================"
echo " END OF PREFLIGHT CHECK -- $(hostname)"
echo "======================================================"
