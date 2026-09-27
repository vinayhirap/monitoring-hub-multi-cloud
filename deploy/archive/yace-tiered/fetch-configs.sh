#!/bin/bash
# deploy/yace-tiered/fetch-configs.sh
#
# Pulls the 3 tier YACE configs for one account from the FastAPI backend
# and writes them here as yace-critical.yml / yace-standard.yml / yace-trend.yml
# — the exact filenames docker-compose.yml in this directory expects.
#
# Usage:
#   ./fetch-configs.sh <backend-base-url> <account-id> [bearer-token]
#
# Example:
#   ./fetch-configs.sh http://40.192.48.70 12 eyJhbGciOi...
set -e

BASE="${1:?Usage: fetch-configs.sh <backend-base-url> <account-id> [bearer-token]}"
ACCOUNT_ID="${2:?Usage: fetch-configs.sh <backend-base-url> <account-id> [bearer-token]}"
TOKEN="${3:-}"

AUTH_HEADER=()
if [ -n "$TOKEN" ]; then
  AUTH_HEADER=(-H "Authorization: Bearer $TOKEN")
fi

for tier in critical standard trend; do
  echo "Fetching ${tier} tier config for account ${ACCOUNT_ID}..."
  curl -fsSL "${AUTH_HEADER[@]}" \
    "${BASE}/api/account-metrics/${ACCOUNT_ID}/yace-config?tier=${tier}&download=false" \
    -o "yace-${tier}.yml"
  echo "  -> yace-${tier}.yml ($(wc -l < "yace-${tier}.yml") lines)"
done

echo ""
echo "Done. Review the 3 files, then: docker compose up -d"
