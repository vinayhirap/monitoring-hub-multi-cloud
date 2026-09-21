#!/usr/bin/env bash
# scripts/next_migration_number.sh
#
# Prints the next free migration number, computed against a freshly
# fetched origin/main -- not against whatever db/migrations/ happens
# to look like in your local working copy, which may be stale by the
# time you're ready to name a file. This is the actual fix for the
# "two sessions both grab 050" collision: it's cheap for every audit
# session/patch author to re-run this right before naming a migration
# file, since a local clone can go stale in the minutes between
# starting work and finishing it while other sessions push in
# parallel.
#
# This does NOT take a lock and cannot fully prevent two people
# running it in the same few seconds and getting the same answer --
# scripts/check_migration_numbers.py is the backstop that catches
# that at push/PR time. Use both.
#
# Usage:
#   scripts/next_migration_number.sh
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

echo "Fetching origin/main..." >&2
git fetch origin main --quiet

highest=$(git ls-tree -r --name-only origin/main -- db/migrations \
  | grep -oE '[0-9]+' \
  | sort -n | tail -1)

if [ -z "${highest:-}" ]; then
  echo "Could not determine the highest migration number from origin/main." >&2
  exit 1
fi

next=$((10#$highest + 1))
printf "%03d\n" "$next"

echo "" >&2
echo "(Highest on origin/main right now: ${highest}. This is a race-reducer," >&2
echo " not a lock -- re-run right before you push, and expect" >&2
echo " scripts/check_migration_numbers.py in CI to catch a genuine collision.)" >&2
