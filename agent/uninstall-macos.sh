#!/usr/bin/env bash
# Remove the CloudOps host agent from macOS. Add --purge to also delete config/token, queue, logs and the service account.
# This does NOT revoke the host on the hub; do that in the CloudOps UI (hostagents.manage).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
PURGE=0; [ "${1:-}" = "--purge" ] && PURGE=1
launchctl bootout system/com.cloudops.agent 2>/dev/null || true
rm -f /Library/LaunchDaemons/com.cloudops.agent.plist /usr/local/bin/cloudops-agent /etc/newsyslog.d/cloudops-agent.conf
if [ "$PURGE" -eq 1 ]; then
  rm -rf /etc/cloudops-agent /var/lib/cloudops-agent
  dscl . -read /Users/_cloudops-agent >/dev/null 2>&1 && dscl . -delete /Users/_cloudops-agent || true
  dscl . -read /Groups/_cloudops-agent >/dev/null 2>&1 && dscl . -delete /Groups/_cloudops-agent || true
  echo "purged config, token, queue, logs and the service account."
else
  echo "kept /etc/cloudops-agent (token) and /var/lib/cloudops-agent (queue, log). Use --purge to delete them."
fi
echo "Reminder: revoke this host on the hub so the token cannot be reused."
