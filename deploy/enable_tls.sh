#!/usr/bin/env bash
# deploy/enable_tls.sh -- add HTTPS to the LIVE nginx config (audit B01, F5).
#
# Until now the app was served over plain HTTP on :80, so passwords and the
# mh_session cookie crossed the network in cleartext (COOKIE_SECURE=false).
# This edits /etc/nginx/sites-available/<service> in place, keeps a timestamped
# backup, runs `nginx -t`, and restores the backup automatically if anything
# fails. It is idempotent (safe to re-run) and does NOT touch .env or restart
# the app -- see "NEXT STEPS" printed at the end.
#
#   sudo ./deploy/enable_tls.sh --domain hub.example.com --email you@example.com [--redirect]
#       Let's Encrypt cert via certbot (needs a DNS name that resolves to this
#       server and inbound TCP 80 + 443 open in the AWS security group).
#   sudo ./deploy/enable_tls.sh --self-signed --cn 13.200.102.131 [--redirect]
#       No domain: 825-day self-signed cert. Traffic is encrypted and the
#       Secure cookie works, but browsers warn once until the cert is trusted.
#
# --redirect  also 301-redirects http -> https. Leave it off for the first run,
#             confirm https works in a browser, then re-run with --redirect.
#             (COOKIE_SECURE=true means http logins stop working, so turn the
#             redirect on when you turn COOKIE_SECURE on.)
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-monitoring-hub}"
CONF="/etc/nginx/sites-available/${SERVICE_NAME}"
SSL_DIR="/etc/ssl/${SERVICE_NAME}"
MODE=""; DOMAIN=""; EMAIL=""; CN=""; REDIRECT=0

usage() { sed -n '2,24p' "$0"; exit "${1:-1}"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --domain)      MODE="certbot"; DOMAIN="${2:?--domain needs a value}"; shift 2 ;;
    --email)       EMAIL="${2:?--email needs a value}"; shift 2 ;;
    --self-signed) MODE="self"; shift ;;
    --cn)          CN="${2:?--cn needs a value}"; shift 2 ;;
    --redirect)    REDIRECT=1; shift ;;
    -h|--help)     usage 0 ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
done

[ -n "$MODE" ] || usage
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
[ -f "$CONF" ]     || { echo "not found: $CONF (set SERVICE_NAME=... if your site file is named differently)"; exit 1; }
if [ "$MODE" = "certbot" ]; then [ -n "$EMAIL" ] || { echo "--domain needs --email"; exit 1; }; fi
if [ "$MODE" = "self" ];    then [ -n "$CN" ]    || { echo "--self-signed needs --cn <public IP or hostname>"; exit 1; }; fi

BACKUP="${CONF}.bak.$(date +%Y%m%d_%H%M%S)"
cp -p "$CONF" "$BACKUP"
restore() {
  echo "FAILED -- restoring ${BACKUP}" >&2
  cp -p "$BACKUP" "$CONF"
  nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
}
trap restore ERR

# 1. Tell the app which scheme the client used (uvicorn --proxy-headers -> request.url.scheme).
sed -i '/proxy_set_header X-Forwarded-Proto/d' "$CONF"
sed -i 's/^\([[:space:]]*\)proxy_set_header X-Real-IP \(.*\)$/&\n\1proxy_set_header X-Forwarded-Proto $scheme;/' "$CONF"

# 2. HTTPS listener.
if [ "$MODE" = "self" ]; then
  mkdir -p "$SSL_DIR"; chmod 750 "$SSL_DIR"
  if [ ! -s "$SSL_DIR/cert.pem" ] || [ ! -s "$SSL_DIR/key.pem" ]; then
    if [[ "$CN" =~ ^[0-9.]+$ ]]; then SAN="IP:${CN}"; else SAN="DNS:${CN}"; fi
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
      -keyout "$SSL_DIR/key.pem" -out "$SSL_DIR/cert.pem" \
      -subj "/CN=${CN}" -addext "subjectAltName=${SAN}" 2>/dev/null
    chmod 600 "$SSL_DIR/key.pem"
  fi
  if ! grep -q 'listen 443' "$CONF"; then
    awk -v ssl="$SSL_DIR" '
      /^[[:space:]]*listen 80;/ && !done {
        print; match($0, /^[[:space:]]*/); ind = substr($0, 1, RLENGTH)
        print ind "listen 443 ssl;"
        print ind "ssl_certificate " ssl "/cert.pem;"
        print ind "ssl_certificate_key " ssl "/key.pem;"
        print ind "ssl_protocols TLSv1.2 TLSv1.3;"
        done = 1; next
      } { print }' "$CONF" > "${CONF}.new"
    mv "${CONF}.new" "$CONF"
  fi
  if [ "$REDIRECT" -eq 1 ] && ! grep -q 'return 301 https' "$CONF"; then
    awk '{ print } /^[[:space:]]*server_name / && !done {
           match($0, /^[[:space:]]*/); ind = substr($0, 1, RLENGTH)
           print ""; print ind "if ($scheme = http) { return 301 https://$host$request_uri; }"; done = 1 }' \
      "$CONF" > "${CONF}.new"
    mv "${CONF}.new" "$CONF"
  fi
else
  command -v certbot >/dev/null 2>&1 || { apt-get update -y && apt-get install -y certbot python3-certbot-nginx; }
  sed -i "s/^\([[:space:]]*server_name\) .*;/\1 ${DOMAIN};/" "$CONF"
  nginx -t
  if [ "$REDIRECT" -eq 1 ]; then RD="--redirect"; else RD="--no-redirect"; fi
  certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos --non-interactive "$RD"
fi

# 3. Validate, then go live.
nginx -t
systemctl reload nginx
trap - ERR

cat <<MSG

TLS enabled. Backup of the previous config: ${BACKUP}

NEXT STEPS
  1. AWS security group: allow inbound TCP 443 (and keep 80).
  2. Browse to https://<host>/ and confirm the app loads and live updates work.
  3. Then, in /opt/monitoring-hub/app/.env set:
       COOKIE_SECURE=true
       PUBLIC_APP_URL=https://<host>
     (also SSO_SP_ACS_URL / CORS_ALLOWED_ORIGINS if you use them), then:
       sudo ./deploy/enable_tls.sh <same options> --redirect
       sudo systemctl restart monitoring-hub
     and rebuild the frontend (cd frontend && npm install --silent && npm run build).
  ROLLBACK: cp -p ${BACKUP} ${CONF} && nginx -t && sudo systemctl reload nginx
MSG
