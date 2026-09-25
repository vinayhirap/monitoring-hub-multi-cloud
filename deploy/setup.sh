#!/bin/bash
set -e

# AUDIT FIX (i01/073, CRITICAL): this script is superseded by
# deploy/deploy.sh, which does everything this one did (and much more:
# app clone, venv, systemd unit, nginx site, hardening, verification)
# EXCEPT this one also created the MySQL 'monitor' user with the literal,
# hardcoded, public-repo-visible password "root123" -- the exact
# known-weak-default landmine deploy/deploy.sh's own history documents
# fixing. A short, easy-to-mistake-for-harmless script like this one
# re-introducing that exact landmine is worse than not having it at all.
# Refuse to run rather than silently create a weak-password DB user.
echo "ERROR: deploy/setup.sh is deprecated and disabled -- it created the"
echo "MySQL user with a hardcoded weak password. Use deploy/deploy.sh"
echo "instead; it performs this same MySQL setup step (with a securely"
echo "generated password) plus the full app install."
exit 1