#!/usr/bin/env bash
# Manual end-to-end test of the host agent against the DEV hub.
# Prereqs on DEV: chat B (tables/auth/enroll/ingest) and chat C merged, HOST_AGENT_ENABLED=true in .env,
#                 monitoring-hub restarted, and an AWS account id the admin user can manage.
#
# Run ON the DEV box (or any Linux box that can reach the hub), as a sudoer:
#   cd /opt/monitoring-hub/app/agent
#   make dist                       # needs Go; or copy dist/ built elsewhere
#   HUB_URL=https://<DEV_IP> ACCOUNT_ID=<aws_accounts.id> ADMIN_USER=<admin username> \
#   INSECURE=1 ./scripts/e2e-dev.sh            # INSECURE=1 only if HUB_URL is http:// or a self-signed cert
# Optional: OFFLINE_TEST=1 (needs iptables) to also test buffer+replay by blocking the hub for the agent user.
# The admin password is prompted (never stored/echoed). Nothing here prints tokens or keys.
set -uo pipefail

: "${HUB_URL:?set HUB_URL, e.g. https://<DEV_IP>}"
: "${ACCOUNT_ID:?set ACCOUNT_ID}"
: "${ADMIN_USER:?set ADMIN_USER}"
HUB_URL=${HUB_URL%/}
HERE=$(cd "$(dirname "$0")/.." && pwd)
case "$(uname -m)" in x86_64) ARCH=amd64 ;; aarch64) ARCH=arm64 ;; *) echo "unsupported arch"; exit 1 ;; esac
CURL=(curl -sS --max-time 20); CURL_K=""
[ "${INSECURE:-}" = 1 ] && { CURL+=(-k); CURL_K="--insecure-dev"; }
JAR=$(mktemp); trap 'rm -f "$JAR"' EXIT
PASS_N=0; FAIL_N=0
ok()   { echo "  PASS  $*"; PASS_N=$((PASS_N+1)); }
bad()  { echo "  FAIL  $*"; FAIL_N=$((FAIL_N+1)); }
step() { echo; echo "== $*"; }
jget() { python3 -c 'import sys,json
d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d=d[int(k)] if isinstance(d,list) else d.get(k) if isinstance(d,dict) else None
print("" if d is None else d)' "$1" 2>/dev/null; }
api()  { # api METHOD PATH [json-body] -> body on stdout, status in $STATUS
  local m=$1 p=$2 b=${3:-}; local out
  if [ -n "$b" ]; then out=$("${CURL[@]}" -b "$JAR" -c "$JAR" -X "$m" -H 'Content-Type: application/json' -d "$b" -w '\n%{http_code}' "$HUB_URL$p")
  else out=$("${CURL[@]}" -b "$JAR" -c "$JAR" -X "$m" -w '\n%{http_code}' "$HUB_URL$p"); fi
  STATUS=${out##*$'\n'}; printf '%s' "${out%$'\n'*}"; }
agent_state() { api GET /api/host-agents | python3 -c 'import sys,json
h=sys.argv[1]
for a in json.load(sys.stdin):
    if a.get("hostname")==h: print(a.get("state","")); break' "$(hostname)" 2>/dev/null; }
wait_state() { local want=$1 t=${2:-150} i=0 s; while [ $i -lt "$t" ]; do s=$(agent_state); [ "$s" = "$want" ] && return 0; sleep 5; i=$((i+5)); done; return 1; }

[ -x "$HERE/dist/cloudops-agent-linux-$ARCH" ] || { echo "run 'make dist' in $HERE first"; exit 1; }

step "0. flag/route sanity"
api GET /api/host-agents >/dev/null; echo "  GET /api/host-agents (unauthenticated) -> $STATUS"
[ "$STATUS" = 404 ] && { echo "  404 = HOST_AGENT_ENABLED is false or chat B not deployed. Stop."; exit 1; }
[ "$STATUS" = 401 ] || [ "$STATUS" = 403 ] && ok "route exists, requires auth" || bad "expected 401/403, got $STATUS"

step "1. admin login"
read -rsp "  password for $ADMIN_USER: " PW; echo
BODY=$(python3 -c 'import json,sys; print(json.dumps({"username":sys.argv[1],"password":sys.argv[2]}))' "$ADMIN_USER" "$PW"); unset PW
api POST /api/auth/login "$BODY" >/dev/null; unset BODY
[ "$STATUS" = 200 ] && ok "logged in" || { bad "login failed ($STATUS)"; exit 1; }

step "2. create a single-use enrollment key"
R=$(api POST /api/host-agents/enrollment-keys "{\"aws_account_id\": $ACCOUNT_ID, \"label\": \"e2e-$(date +%s)\", \"max_uses\": 1, \"expires_in_hours\": 1}")
KEY=$(printf '%s' "$R" | jget key); KID=$(printf '%s' "$R" | jget id)
[ "$STATUS" = 200 ] && [ "${KEY:0:4}" = cak_ ] && ok "key created (id=$KID)" || { bad "key creation failed ($STATUS)"; exit 1; }
api GET /api/host-agents/enrollment-keys | grep -q "$KEY" && bad "key listing leaks the key" || ok "key list never returns the key"

step "3. install + enroll"
sudo "$HERE/install.sh" --binary "$HERE/dist/cloudops-agent-linux-$ARCH" --sums "$HERE/dist/SHA256SUMS" --no-start ${CURL_K:+$CURL_K} \
  && ok "installed" || { bad "install failed"; exit 1; }
sudo CLOUDOPS_AGENT_ENROLL_KEY="$KEY" /usr/local/bin/cloudops-agent enroll --url "$HUB_URL" --force $CURL_K \
  && ok "enrolled" || { bad "enroll failed"; exit 1; }
[ "$(sudo stat -c %a /etc/cloudops-agent/agent.json)" = 600 ] && ok "agent.json is 0600" || bad "agent.json mode wrong"
sudo grep -q "$KEY" /etc/cloudops-agent/agent.json && bad "enrollment key stored on disk" || ok "enrollment key not on disk"
sudo /usr/local/bin/cloudops-agent enroll --url "$HUB_URL" --key "$KEY" --force $CURL_K >/dev/null 2>&1 \
  && bad "single-use key was accepted twice" || ok "single-use key rejected on reuse"
unset KEY

step "4. start service, host goes online"
sudo systemctl start cloudops-agent; sleep 3
systemctl is-active --quiet cloudops-agent && ok "service active" || { bad "service not active"; sudo journalctl -u cloudops-agent -n 20 --no-pager; }
wait_state online 150 && ok "hub shows state=online" || bad "host never became online (state=$(agent_state))"
ps -o rss=,pcpu= -p "$(systemctl show -p MainPID --value cloudops-agent)" | awk '{printf "  info  RSS=%.1f MB  CPU=%s%%\n",$1/1024,$2}'

step "5. live config push (no restart)"
HID=$(api GET /api/host-agents | python3 -c 'import sys,json
for a in json.load(sys.stdin):
    if a.get("hostname")==sys.argv[1]: print(a["id"])' "$(hostname)")
PIDBEFORE=$(systemctl show -p MainPID --value cloudops-agent)
api PATCH "/api/host-agents/$HID/config" '{"interval_seconds": 15, "watch_services": ["cron","ssh","sshd"]}' >/dev/null
[ "$STATUS" = 200 ] && ok "config PATCH accepted" || bad "config PATCH -> $STATUS"
sleep 40
sudo journalctl -u cloudops-agent --since "2 min ago" --no-pager | grep -q "config updated by hub" && ok "agent applied new config" || bad "no 'config updated by hub' in journal"
[ "$(systemctl show -p MainPID --value cloudops-agent)" = "$PIDBEFORE" ] && ok "applied without restart (same PID)" || bad "service restarted"

if [ "${OFFLINE_TEST:-}" = 1 ]; then
  step "6. offline buffer + replay (iptables block for the agent user)"
  HOSTPORT=$(python3 -c 'import sys,urllib.parse as u; p=u.urlparse(sys.argv[1]); print(p.hostname, p.port or (443 if p.scheme=="https" else 80))' "$HUB_URL")
  set -- $HOSTPORT
  UID_=$(id -u cloudops-agent)
  sudo iptables -I OUTPUT -m owner --uid-owner "$UID_" -d "$1" -p tcp --dport "$2" -j REJECT && ok "hub blocked for the agent user"
  sleep 75
  N=$(sudo ls /var/lib/cloudops-agent/queue 2>/dev/null | grep -c '\.q$'); [ "$N" -ge 2 ] && ok "buffered $N payloads on disk" || bad "expected >=2 queued files, found $N"
  sudo iptables -D OUTPUT -m owner --uid-owner "$UID_" -d "$1" -p tcp --dport "$2" -j REJECT
  sleep 60
  N=$(sudo ls /var/lib/cloudops-agent/queue 2>/dev/null | grep -c '\.q$'); [ "$N" -eq 0 ] && ok "queue replayed and empty" || bad "$N files still queued"
  sudo journalctl -u cloudops-agent --since "5 min ago" --no-pager | grep -q "hub reachable again" && ok "recovery logged" || bad "no recovery log"
else
  step "6. offline buffer test skipped (set OFFLINE_TEST=1 to run; needs iptables)"
fi

step "7. no inbound listener"
LS=$(sudo ss -ltnp 2>/dev/null | grep -c cloudops-agent); [ "$LS" -eq 0 ] && ok "agent listens on no port" || bad "agent has a listening socket"

step "8. SIGTERM is graceful"
sudo systemctl stop cloudops-agent; sudo journalctl -u cloudops-agent -n 5 --no-pager | grep -q "agent stopped" && ok "clean stop logged" || bad "no clean-stop log"
sudo systemctl start cloudops-agent; sleep 3

step "9. revoke -> agent stops and does not hammer"
api POST "/api/host-agents/$HID/revoke" >/dev/null; [ "$STATUS" = 200 ] && ok "host revoked on hub" || bad "revoke -> $STATUS"
sleep 90
S=$(systemctl is-active cloudops-agent || true)
[ "$S" != active ] && ok "service not running after 401 (state=$S)" || bad "service still active after revoke"
sudo journalctl -u cloudops-agent --since "3 min ago" --no-pager | grep -c "HTTP 401" | awk '{ if ($1>=1 && $1<=3) print "  PASS  401 logged "$1" time(s), no hammering"; else print "  FAIL  401 logged "$1" times" }'
[ "$(agent_state)" = revoked ] && ok "hub shows state=revoked" || bad "hub state is '$(agent_state)'"
sudo journalctl -u cloudops-agent --no-pager | grep -q 'cah_' && bad "token found in journal" || ok "token never appears in the journal"

step "10. cleanup"
[ -n "$KID" ] && api DELETE "/api/host-agents/enrollment-keys/$KID" >/dev/null
sudo "$HERE/uninstall.sh" --purge >/dev/null && ok "uninstalled" || bad "uninstall failed"
echo; echo "RESULT: $PASS_N passed, $FAIL_N failed"; [ "$FAIL_N" -eq 0 ]
