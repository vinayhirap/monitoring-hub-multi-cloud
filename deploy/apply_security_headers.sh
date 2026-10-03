#!/usr/bin/env bash
# deploy/apply_security_headers.sh -- add security headers to the LIVE nginx config
# (audit E2 / Phase 1). Same safety model as deploy/enable_tls.sh: timestamped
# backup, `nginx -t`, automatic restore on failure, idempotent (re-run is a no-op).
#
#   sudo ./deploy/apply_security_headers.sh                 # headers + CSP in REPORT-ONLY mode
#   sudo ./deploy/apply_security_headers.sh --hsts          # also Strict-Transport-Security (ONLY after https works)
#   sudo ./deploy/apply_security_headers.sh --enforce-csp   # CSP enforced (after checking the browser console is clean)
#
# What it adds inside the server block (inherited by every location):
#   server_tokens off; X-Content-Type-Options; X-Frame-Options; Referrer-Policy;
#   Permissions-Policy; Content-Security-Policy[-Report-Only]; [HSTS];
#   gzip for JSON/JS/CSS/SVG (API responses were sent uncompressed -- audit C2).
#
# The FastAPI app (app/main.py _security_headers) already sets the same headers on
# /api responses. nginx static files (the SPA shell) never pass through it, which is
# why they were bare. To avoid DUPLICATE headers on proxied responses (Chrome ignores
# a duplicated X-Frame-Options), the block hides the app's copies of the headers nginx
# now owns. The app's own enforced Content-Security-Policy is left alone.
# Re-running with different flags replaces the previous block.
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-monitoring-hub}"
CONF="/etc/nginx/sites-available/${SERVICE_NAME}"
HSTS=0; ENFORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --hsts)         HSTS=1; shift ;;
    --enforce-csp)  ENFORCE=1; shift ;;
    -h|--help)      sed -n 2,14p "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
[ -f "$CONF" ]       || { echo "not found: $CONF (set SERVICE_NAME=... if your site file is named differently)"; exit 1; }
if [ "$HSTS" -eq 1 ] && ! grep -q 'listen 443' "$CONF"; then
  echo "refusing --hsts: no 'listen 443' in $CONF. Run deploy/enable_tls.sh first." >&2; exit 1
fi

BACKUP="${CONF}.bak.$(date +%Y%m%d_%H%M%S)"
cp -p "$CONF" "$BACKUP"
restore() {
  echo "FAILED -- restoring ${BACKUP}" >&2
  cp -p "$BACKUP" "$CONF"
  nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
}
trap restore ERR

# Google Fonts + the alarm .ogg are the only third-party origins the UI uses today.
CSP="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data: blob:; media-src 'self' https://actions.google.com data: blob:; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
if [ "$ENFORCE" -eq 1 ]; then CSP_HDR="Content-Security-Policy"; else CSP_HDR="Content-Security-Policy-Report-Only"; fi

BLOCK=$(mktemp)
{
  echo "    # >>> monitoring-hub security headers (deploy/apply_security_headers.sh)"
  echo "    server_tokens off;"
  echo "    proxy_hide_header X-Content-Type-Options;"
  echo "    proxy_hide_header X-Frame-Options;"
  echo "    proxy_hide_header Referrer-Policy;"
  echo "    proxy_hide_header Permissions-Policy;"
  echo "    proxy_hide_header Strict-Transport-Security;"
  echo "    gzip on;"
  echo "    gzip_vary on;"
  echo "    gzip_proxied any;"
  echo "    gzip_comp_level 5;"
  echo "    gzip_min_length 1024;"
  echo "    gzip_types application/json application/javascript text/css image/svg+xml text/plain;"
  echo "    add_header X-Content-Type-Options \"nosniff\" always;"
  echo "    add_header X-Frame-Options \"DENY\" always;"
  echo "    add_header Referrer-Policy \"strict-origin-when-cross-origin\" always;"
  echo "    add_header Permissions-Policy \"camera=(), microphone=(), geolocation=()\" always;"
  echo "    add_header ${CSP_HDR} \"${CSP}\" always;"
  if [ "$HSTS" -eq 1 ]; then
    echo "    add_header Strict-Transport-Security \"max-age=86400\" always;"
  fi
  echo "    # <<< monitoring-hub security headers"
} > "$BLOCK"

# Remove any previous block, then insert the new one right after the first server_name line.
sed -i '/# >>> monitoring-hub security headers/,/# <<< monitoring-hub security headers/d' "$CONF"
awk -v blk="$BLOCK" '{ print } /^[[:space:]]*server_name / && !done { print ""; while ((getline line < blk) > 0) print line; done = 1 }' \
  "$CONF" > "${CONF}.new"
mv "${CONF}.new" "$CONF"
rm -f "$BLOCK"

nginx -t
systemctl reload nginx
trap - ERR

cat <<MSG

Security headers applied (CSP mode: $([ "$ENFORCE" -eq 1 ] && echo ENFORCED || echo report-only), HSTS: $([ "$HSTS" -eq 1 ] && echo "on, max-age=86400" || echo off)).
Backup: ${BACKUP}
Verify:   curl -sI http://127.0.0.1/ | grep -i -E 'server|x-frame|x-content|referrer|content-security|strict-transport'
          curl -sI -H 'Accept-Encoding: gzip' http://127.0.0.1/api/alerts | grep -i -E 'content-encoding|x-frame'   # 401 is fine: headers still show
ROLLBACK: cp -p ${BACKUP} ${CONF} && nginx -t && sudo systemctl reload nginx
MSG
