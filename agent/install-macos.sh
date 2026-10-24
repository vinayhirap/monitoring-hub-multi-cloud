#!/usr/bin/env bash
# CloudOps host agent installer for macOS (Intel + Apple Silicon, launchd). Works with the stock bash 3.2.
#
#   sudo ./install-macos.sh --binary ./cloudops-agent-darwin-arm64 --sums ./SHA256SUMS --url https://<hub> --key cak_...
#   sudo ./install-macos.sh --base-url https://<where-binaries-are-hosted>/cloudops-agent --url https://<hub> --key cak_... [--insecure-dev]
#
# The binary is ALWAYS verified against SHA256SUMS. Runs as the unprivileged system account _cloudops-agent.
set -euo pipefail

BIN_DIR=/usr/local/bin
BIN=$BIN_DIR/cloudops-agent
ETC_DIR=/etc/cloudops-agent
DATA_DIR=/var/lib/cloudops-agent
LABEL=com.cloudops.agent
PLIST=/Library/LaunchDaemons/$LABEL.plist
NEWSYSLOG=/etc/newsyslog.d/cloudops-agent.conf
SVC_USER=_cloudops-agent

BASE_URL=${CLOUDOPS_AGENT_BASE_URL:-}
LOCAL_BIN=""
LOCAL_SUMS=""
HUB_URL=""
ENROLL_KEY=${CLOUDOPS_AGENT_ENROLL_KEY:-}
INSECURE=""
START=1

die()  { echo "install-macos.sh: $*" >&2; exit 1; }
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
    -h|--help)      sed -n '2,8p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[ "$(uname -s)" = "Darwin" ] || die "this installer is for macOS; use install.sh on Linux"
[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
command -v shasum >/dev/null || die "shasum is required"

case "$(uname -m)" in
  x86_64)        ARCH=amd64 ;;
  arm64|aarch64) ARCH=arm64 ;;
  *) die "unsupported architecture: $(uname -m)" ;;
esac
NAME=cloudops-agent-darwin-$ARCH

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
  info "downloading $NAME"
  curl -fsSL --proto '=https,http' --max-time 120 -o "$TMP/$NAME"     "$BASE_URL/$NAME"     || die "download failed: $BASE_URL/$NAME"
  curl -fsSL --proto '=https,http' --max-time 30  -o "$TMP/SHA256SUMS" "$BASE_URL/SHA256SUMS" || die "download failed: $BASE_URL/SHA256SUMS"
fi

info "verifying sha256"
EXPECTED=$(awk -v n="$NAME" '$2==n || $2=="*"n {print $1}' "$TMP/SHA256SUMS" | head -n1)
[ -n "$EXPECTED" ] || die "$NAME not listed in SHA256SUMS"
ACTUAL=$(shasum -a 256 "$TMP/$NAME" | awk '{print $1}')
[ "$EXPECTED" = "$ACTUAL" ] || die "CHECKSUM MISMATCH for $NAME (expected $EXPECTED, got $ACTUAL); nothing installed"
chmod 0755 "$TMP/$NAME"
xattr -d com.apple.quarantine "$TMP/$NAME" 2>/dev/null || true   # a browser/AirDrop download is quarantined; curl/scp are not
"$TMP/$NAME" version >/dev/null || die "downloaded binary does not run on this host"

# A free UID/GID in the system-account range (200-400), the same number for user and group.
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  info "creating system account $SVC_USER"
  USED=" $(dscl . -list /Users UniqueID | awk '{print $2}' | tr '\n' ' ') $(dscl . -list /Groups PrimaryGroupID | awk '{print $2}' | tr '\n' ' ') "
  ID=""
  for n in $(seq 300 399); do
    case "$USED" in *" $n "*) ;; *) ID=$n; break ;; esac
  done
  [ -n "$ID" ] || die "no free system UID/GID in 300-399"
  dscl . -create /Groups/$SVC_USER
  dscl . -create /Groups/$SVC_USER PrimaryGroupID "$ID"
  dscl . -create /Groups/$SVC_USER RealName "CloudOps host agent"
  dscl . -create /Users/$SVC_USER
  dscl . -create /Users/$SVC_USER UniqueID "$ID"
  dscl . -create /Users/$SVC_USER PrimaryGroupID "$ID"
  dscl . -create /Users/$SVC_USER UserShell /usr/bin/false
  dscl . -create /Users/$SVC_USER RealName "CloudOps host agent"
  dscl . -create /Users/$SVC_USER NFSHomeDirectory /var/empty
  dscl . -create /Users/$SVC_USER IsHidden 1
fi

info "installing $BIN"
install -d -m 0755 "$BIN_DIR"
install -m 0755 "$TMP/$NAME" "$BIN.new"
mv -f "$BIN.new" "$BIN"

install -d -m 0750 -o root -g "$SVC_USER" "$ETC_DIR"
install -d -m 0700 -o "$SVC_USER" -g "$SVC_USER" "$DATA_DIR"

INSECURE_XML=""
[ -n "$INSECURE" ] && INSECURE_XML="<string>--insecure-dev</string>"
info "writing $PLIST"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN</string>
    <string>run</string>
    <string>--log-file</string>
    <string>$DATA_DIR/agent.log</string>
    $INSECURE_XML
  </array>
  <key>UserName</key><string>$SVC_USER</string>
  <key>GroupName</key><string>$SVC_USER</string>
  <key>RunAtLoad</key><true/>
  <!-- Restart on any non-zero exit. A revoked host (exit 3) therefore retries about once a minute, one cheap 401
       each time, until the agent is uninstalled: launchd cannot exclude a single exit code. -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>5</integer>
  <key>Umask</key><integer>63</integer>
  <key>EnvironmentVariables</key>
  <dict><key>GOMAXPROCS</key><string>1</string><key>GOMEMLIMIT</key><string>40MiB</string></dict>
</dict>
</plist>
PLISTEOF
chown root:wheel "$PLIST"; chmod 0644 "$PLIST"

# Rotate the agent's own log file (the agent also caps it at 5 MiB; this is belt and braces).
install -d -m 0755 /etc/newsyslog.d
printf '%s\n' "# logfilename [owner:group] mode count size when flags" \
  "$DATA_DIR/agent.log $SVC_USER:$SVC_USER 600 2 5120 * J" > "$NEWSYSLOG"

if [ -n "$HUB_URL" ]; then
  [ -n "$ENROLL_KEY" ] || die "--url given but no --key (or CLOUDOPS_AGENT_ENROLL_KEY)"
  info "enrolling with $HUB_URL"
  CLOUDOPS_AGENT_ENROLL_KEY="$ENROLL_KEY" "$BIN" enroll --url "$HUB_URL" --config "$ETC_DIR/agent.json" $INSECURE
  chown "$SVC_USER:$SVC_USER" "$ETC_DIR/agent.json"; chmod 0600 "$ETC_DIR/agent.json"
fi

launchctl bootout system/$LABEL >/dev/null 2>&1 || true
if [ -f "$ETC_DIR/agent.json" ]; then
  launchctl enable system/$LABEL >/dev/null 2>&1 || true
  if [ "$START" -eq 1 ]; then
    info "starting $LABEL"
    launchctl bootstrap system "$PLIST"
    sleep 2
    if launchctl print system/$LABEL 2>/dev/null | grep -q "state = running"; then info "running"; else
      tail -n 20 "$DATA_DIR/agent.log" 2>/dev/null || true
      die "service did not stay up (see $DATA_DIR/agent.log)"
    fi
  fi
else
  info "installed. Not started: this host is not enrolled yet. Run:"
  echo "    sudo CLOUDOPS_AGENT_ENROLL_KEY=cak_... cloudops-agent enroll --url https://<hub> && sudo launchctl bootstrap system $PLIST"
fi
info "done: $("$BIN" version)"
