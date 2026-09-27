# Archived: VictoriaMetrics + tiered YACE deployment

Archived 2026-09-26 at the operator's explicit direction: this
deployment does not run VictoriaMetrics or YACE at all -- dev and prod
both rely entirely on the direct cloud-API collection path
(CloudWatch/Azure Monitor/GCP Monitoring -> app/collector -> MySQL) for
every metric. See app/aws/collector_direct.py and the scheduler's
describe-poll/multicloud-collector threads for the collection path
that's actually used instead.

Nothing here is called by any live script (setup.sh/deploy.sh/
update.sh), referenced by any systemd unit, or imported/consulted by
any app code path with one narrow exception: `VM_URL` in
.env.example/.env.production.example is still read by a few
describe-polling code paths as an optional, gracefully-degrading
legacy fallback (see those files' own comments) -- that env var itself
was intentionally left in place (pointed at a placeholder, not
removed), only this directory and the stray root-level generated-YAML
files were archived.

Moved with `git mv`, so full history is preserved -- nothing is
deleted. A filesystem backup was also taken on both servers before
this change (see the audit chat's deploy commands for the exact tar
command and destination path).

## What moved here

- `deploy/yace-tiered/` (whole directory: README.md, docker-compose.yml,
  fetch-configs.sh, vmagent-scrape.yml) -- unmodified from their last
  committed state, aside from a short "archived, not used" banner
  added to the top of README.md.
- Six root-level `yace-*.yml` / `yace-config-*.yml` files, into
  `yace-tiered/old-generated-configs/` -- these were one-time,
  real debug output from a manual run against a real account (name/id
  redacted before archiving), not templates and not regenerated or
  consumed by anything; a repo-wide search
  (`grep -rl -- "<filename>" .`, all file types, excluding .git) found
  zero references to any of the six anywhere in the tracked repo.
