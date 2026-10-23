#!/usr/bin/env bash
# CloudOps host agent installer (Linux amd64/arm64, systemd).
#
#   sudo ./install.sh --base-url https://<where-binaries-are-hosted>/cloudops-agent \
#        [--version 0.1.0] [--url https://<hub> --key cak_...] [--insecure-dev]
#   sudo ./install.sh --binary ./dist/cloudops-agent-linux-amd64 --sums ./dist/SHA256SUMS ...
#
# --base-url must serve  cloudops-agent-linux-<arch>  and  SHA256SUMS  (what `make dist` produces).
# The download is ALWAYS verified against SHA256SUMS; a mismatch aborts before anything is installed.
set -euo pipefail

BIN_DIR=/usr/local/bin
BIN=$BIN_DIR/cloudops-agent
ETC_DIR=/etc/cloudops-agent
DATA_DIR=/var/lib/cloudops-agent
UNIT=/etc/systemd/system/cloudops-agent.service
SVC_USER=cloudops-agent

BASE_URL=${CLOUDOPS_AGENT_BASE_URL:-}
LOCAL_BIN=""
LOCAL_SUMS=""
HUB_URL=""
ENROLL_KEY=${CLOUDOPS_AGENT_ENROLL_KEY:-}
INSECURE=""
START=1

die()  { echo "install.sh: $*" >&2; exit 1; }
info() { echo "==> $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --base-url)     BASE_URL=${2:?}; shift 2 ;;
    --binary)       LOCAL_BIN=${2:?}; shift 2 ;;
    --sums)         LOCAL_SUMS=${2:?}; shift 2 ;;
    --url)          HUB_URL=${2:?}; shift 2 ;;
    --key)          ENROLL_KEY=${2:?}; shift 2 ;;
    --insecure-dev) INSECURE="--insecure-dev"; shift ;;
    --no-start)     START=0; shift ;;
    -h|--help)      sed -n '2,10p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
command -v systemctl >/dev/null || die "systemd is required"
command -v sha256sum >/dev/null || die "sha256sum is required"

case "$(uname -m)" in
  x86_64|amd64)  ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) die "unsupported architecture: $(uname -m)" ;;
esac
NAME=cloudops-agent-linux-$ARCH

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

if [ -n "$LOCAL_BIN" ]; then
  [ -f "$LOCAL_BIN" ] || die "no such file: $LOCAL_BIN"
  [ -n "$LOCAL_SUMS" ] && [ -f "$LOCAL_SUMS" ] || die "--binary also needs --sums <SHA256SUMS>"
  cp "$LOCAL_BIN" "$TMP/$NAME"
  cp "$LOCAL_SUMS" "$TMP/SHA256SUMS"
else
  [ -n "$BASE_URL" ] || die "pass --base-url (or CLOUDOPS_AGENT_BASE_URL), or --binary/--sums for a local file"
  BASE_URL=${BASE_URL%/}
  case "$BASE_URL" in https://*) ;; http://*) [ -n "$INSECURE" ] || die "refusing http:// download; use https or --insecure-dev" ;; *) die "bad --base-url" ;; esac
  command -v curl >/dev/null || die "curl is required"
  info "downloading $NAME"
  curl -fsSL --proto '=https,http' --max-time 120 -o "$TMP/$NAME"     "$BASE_URL/$NAME"     || die "download failed: $BASE_URL/$NAME"
  curl -fsSL --proto '=https,http' --max-time 30  -o "$TMP/SHA256SUMS" "$BASE_URL/SHA256SUMS" || die "download failed: $BASE_URL/SHA256SUMS"
fi

info "verifying sha256"
EXPECTED=$(awk -v n="$NAME" '$2==n || $2=="*"n {print $1}' "$TMP/SHA256SUMS" | head -n1)
[ -n "$EXPECTED" ] || die "$NAME not listed in SHA256SUMS"
ACTUAL=$(sha256sum "$TMP/$NAME" | awk '{print $1}')
[ "$EXPECTED" = "$ACTUAL" ] || die "CHECKSUM MISMATCH for $NAME (expected $EXPECTED, got $ACTUAL); nothing installed"
chmod 0755 "$TMP/$NAME"
"$TMP/$NAME" version >/dev/null || die "downloaded binary does not run on this host"

if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  info "creating system user $SVC_USER"
  NOLOGIN=$(command -v nologin || echo /usr/sbin/nologin)
  useradd --system --no-create-home --home-dir /nonexistent --shell "$NOLOGIN" "$SVC_USER"
fi

info "installing $BIN"
install -d -m 0755 "$BIN_DIR"
install -m 0755 "$TMP/$NAME" "$BIN.new"
mv -f "$BIN.new" "$BIN"

install -d -m 0750 -o root -g "$SVC_USER" "$ETC_DIR"
install -d -m 0700 -o "$SVC_USER" -g "$SVC_USER" "$DATA_DIR"

info "writing $UNIT"
cat > "$UNIT" <<'UNITEOF'
[Unit]
Description=CloudOps host agent
Documentation=https://github.com/vinayhirap/monitoring-hub-multi-cloud
After=network-online.target
Wants=network-online.target
ConditionPathExists=/etc/cloudops-agent/agent.json

[Service]
Type=simple
User=cloudops-agent
Group=cloudops-agent
ExecStart=/usr/local/bin/cloudops-agent run
Restart=always
RestartSec=10
# Exit code 3 = the hub answered 401 (host revoked / bad token): stay down, do not hammer the hub.
RestartPreventExitStatus=3
StartLimitIntervalSec=0
Environment=GOMAXPROCS=1 GOMEMLIMIT=40MiB
MemoryMax=64M
CPUQuota=10%
TasksMax=64
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
AmbientCapabilities=
UMask=0077
ReadWritePaths=/var/lib/cloudops-agent
StandardOutput=journal
StandardError=journal
SyslogIdentifier=cloudops-agent

[Install]
WantedBy=multi-user.target
UNITEOF
chmod 0644 "$UNIT"
systemctl daemon-reload

if [ -n "$HUB_URL" ]; then
  [ -n "$ENROLL_KEY" ] || die "--url given but no --key (or CLOUDOPS_AGENT_ENROLL_KEY)"
  info "enrolling with $HUB_URL"
  CLOUDOPS_AGENT_ENROLL_KEY="$ENROLL_KEY" "$BIN" enroll --url "$HUB_URL" --config "$ETC_DIR/agent.json" $INSECURE
  chown "$SVC_USER:$SVC_USER" "$ETC_DIR/agent.json"; chmod 0600 "$ETC_DIR/agent.json"
fi

systemctl enable cloudops-agent >/dev/null 2>&1 || true
if [ -f "$ETC_DIR/agent.json" ]; then
  if [ "$START" -eq 1 ]; then
    info "starting cloudops-agent"
    systemctl restart cloudops-agent
    sleep 1
    systemctl is-active --quiet cloudops-agent && info "active" || { journalctl -u cloudops-agent -n 20 --no-pager || true; die "service did not stay up"; }
  fi
else
  info "installed. Not started: this host is not enrolled yet. Run:"
  echo "    sudo CLOUDOPS_AGENT_ENROLL_KEY=cak_... cloudops-agent enroll --url https://<hub> && sudo systemctl start cloudops-agent"
fi
info "done: $("$BIN" version)"
