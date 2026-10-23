#!/usr/bin/env bash
# Remove the CloudOps host agent. Add --purge to also delete config/token, queue and the service user.
# This does NOT revoke the host on the hub; do that in the CloudOps UI (hostagents.manage).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
PURGE=0; [ "${1:-}" = "--purge" ] && PURGE=1
systemctl disable --now cloudops-agent 2>/dev/null || true
rm -f /etc/systemd/system/cloudops-agent.service /usr/local/bin/cloudops-agent
systemctl daemon-reload
if [ "$PURGE" -eq 1 ]; then
  rm -rf /etc/cloudops-agent /var/lib/cloudops-agent
  id -u cloudops-agent >/dev/null 2>&1 && userdel cloudops-agent || true
  echo "purged config, token, queue and user."
else
  echo "kept /etc/cloudops-agent (token) and /var/lib/cloudops-agent (queue). Use --purge to delete them."
fi
echo "Reminder: revoke this host on the hub so the token cannot be reused."
