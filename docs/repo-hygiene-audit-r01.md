# Repo hygiene audit -- root patch scripts, stale copies, leaked dumps (audit R01)

Base: `4a4588b`. All checks were scripted (`ast`, `git log --diff-filter=A`, grep); the
46 scripts were not read line by line. Nothing was deleted except `emoji-to-icons-update/`
(recoverable from git history); everything else moved with `git mv`.

## 1. The 46 root `apply_*.py` / `fix_*.py` scripts

**21 are LIVE** -- executed by `setup.sh`, `update.sh`, `deploy/deploy.sh` or
`deploy/update.sh` through `run_migration`. They stay at the root. **25 are not
executed anywhere** (no shell call, import, subprocess or glob; comment/docstring
mentions only) and were archived. "Already applied" is mechanical: the code a script
embeds is searched for in today's tree -- a partial match means applied-then-evolved.

| script | added | LOC | status | already applied? (evidence) | dangerous ops | recommendation |
|---|---|---:|---|---|---|---|
| `apply_access_scopes_migration.py` | 2026-08-24 | 185 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh, update.sh) | DB/DDL or idempotent patch; run on every deploy | - | keep at root |
| `apply_add_cwagent_disk_threshold.py` | 2026-09-09 | 384 | not executed | inserted collector functions present in tree | - | archive -> `scripts/archive/` |
| `apply_add_cwagent_mem_threshold.py` | 2026-09-09 | 331 | not executed | `_collect_ec2_cwagent_mem` present (rest superseded by the later mem-dimension fix) | - | archive -> `scripts/archive/` |
| `apply_add_extended_service_discovery.py` | 2026-09-10 | 387 | not executed | 6/6 inserted code blocks still present in the tree | - | archive -> `scripts/archive/` |
| `apply_add_warning_threshold_line.py` | 2026-09-10 | 433 | not executed | applied (frontend); rm of its own .bak only | rm of own .bak | archive -> `scripts/archive/` |
| `apply_alert_evaluation_hardening_migration.py` | 2026-08-25 | 194 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh, update.sh) | DB/DDL or idempotent patch; run on every deploy | - | keep at root |
| `apply_azure_direct_metrics_fetch.py` | 2026-09-08 | 548 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh) | DB/DDL or idempotent patch; run on every deploy | - | keep at root |
| `apply_check_thresholds_local_metrics.py` | 2026-09-08 | 436 | not executed | 5/6 inserted code blocks still present in the tree | - | archive -> `scripts/archive/` |
| `apply_cleanup_disk_mount_noise.py` | 2026-09-10 | 247 | not executed | one-time DB data cleanup (disk_mounts.py filter is in place). DESTRUCTIVE if re-run: DELETEs from 5 tables by a lossy heuristic | DELETE FROM | archive -> `scripts/archive/` |
| `apply_dashboard_charts_metric_history.py` | 2026-09-08 | 578 | not executed | 15/23 inserted code blocks still present in the tree | - | archive -> `scripts/archive/` |
| `apply_db_pool_leak_and_leader_election_fix.py` | 2026-09-05 | 976 | **LIVE** (deploy/deploy.sh) | DB/DDL or idempotent patch; run on every deploy | root123 = anchor text for patching app/db.py (intentional); DELETE FROM | keep at root |
| `apply_default_org_groups_seed.py` | 2026-09-04 | 284 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh) | DB/DDL or idempotent patch; run on every deploy | root123 fallback (fixed) | keep at root |
| `apply_direct_gmd_metrics_revival.py` | 2026-09-08 | 603 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh) | DB/DDL or idempotent patch; run on every deploy | DELETE FROM | keep at root |
| `apply_drop_dead_tables.py` | 2026-09-06 | 205 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh) | DB/DDL or idempotent patch; run on every deploy | DROP TABLE on a fixed list; `roles` false alarm/latent risk (fixed) | keep at root |
| `apply_ensure_metric_catalog_base_table.py` | 2026-09-06 | 121 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh) | DB/DDL or idempotent patch; run on every deploy | root123 fallback (fixed in patch 037) | keep at root |
| `apply_final_cleanup.py` | 2026-09-08 | 474 | not executed | 7/12 inserted code blocks still present in the tree | - | archive -> `scripts/archive/` |
| `apply_fix_alb_healthy_hosts.py` | 2026-09-09 | 510 | not executed | 12/20 inserted code blocks still present in the tree | - | archive -> `scripts/archive/` |
| `apply_fix_alb_healthy_hosts_history.py` | 2026-09-09 | 182 | not executed | 5/5 inserted code blocks still present in the tree | - | archive -> `scripts/archive/` |
| `apply_fix_alb_nlb_threshold_resource_type.py` | 2026-09-09 | 417 | **LIVE** (deploy/update.sh) | DB/DDL or idempotent patch; run on every deploy | - | keep at root |
| `apply_fix_cwagent_mem_dimensions.py` | 2026-09-09 | 278 | not executed | 2 of 3 inserted functions present | - | archive -> `scripts/archive/` |
| `apply_fix_getthreshold_scope.py` | 2026-09-09 | 260 | not executed | `getThreshold` defined in ServiceDetail.jsx | - | archive -> `scripts/archive/` |
| `apply_fix_nlb_ghost_thresholds.py` | 2026-09-10 | 350 | not executed | 6/6 inserted code blocks still present in the tree | - | archive -> `scripts/archive/` |
| `apply_fix_stale_alerts_for_stopped_instances.py` | 2026-09-09 | 312 | not executed | `_auto_resolve_stale_alerts` present in alert_evaluator.py (code evolved since) | - | archive -> `scripts/archive/` |
| `apply_fix_stale_cutoff_too_aggressive.py` | 2026-09-09 | 189 | not executed | 1/1 inserted code blocks still present in the tree | - | archive -> `scripts/archive/` |
| `apply_fix_threshold_resource_type_everywhere.py` | 2026-09-09 | 391 | not executed | 8/9 inserted code blocks still present in the tree | - | archive -> `scripts/archive/` |
| `apply_fresh_schema_migrations.py` | 2026-08-27 | 437 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh, update.sh) | DB/DDL or idempotent patch; run on every deploy | root123 fallback (fixed); DROP INDEX (index swap) | keep at root |
| `apply_fresh_schema_migrations_fk_type_fix.py` | 2026-08-26 | 111 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh, update.sh) | DB/DDL or idempotent patch; run on every deploy | - | keep at root |
| `apply_gcp_direct_metrics_fetch.py` | 2026-09-08 | 952 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh) | DB/DDL or idempotent patch; run on every deploy | - | keep at root |
| `apply_group_level_role_fix.py` | 2026-09-05 | 86 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh) | DB/DDL or idempotent patch; run on every deploy | - | keep at root |
| `apply_hide_no_data_metrics.py` | 2026-09-09 | 460 | not executed | 12/19 inserted code blocks still present in the tree | - | archive -> `scripts/archive/` |
| `apply_list_view_snapshots_metrics.py` | 2026-09-08 | 291 | not executed | 7/14 inserted code blocks still present in the tree | - | archive -> `scripts/archive/` |
| `apply_metrics_dedup_fix.py` | 2026-08-26 | 231 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh, update.sh) | DB/DDL or idempotent patch; run on every deploy | DELETE FROM | keep at root |
| `apply_metrics_to_monitor_cleanup.py` | 2026-09-09 | 411 | not executed | 8/9 inserted code blocks still present in the tree | - | archive -> `scripts/archive/` |
| `apply_multi_cloud_credentials.py` | 2026-08-23 | 164 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh, update.sh) | DB/DDL or idempotent patch; run on every deploy | root123 fallback (fixed) | keep at root |
| `apply_multi_cloud_migration.py` | 2026-08-22 | 209 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh, update.sh) | DB/DDL or idempotent patch; run on every deploy | root123 fallback (fixed) | keep at root |
| `apply_multicloud_full_build.py` | 2026-08-23 | 1983 | not executed | UNVERIFIED whether ever run; wholly superseded (earliest script). DANGEROUS: overwrites 18 files from 2026-08-23 snapshots -- guarded on archive | **overwrites 18 tracked files** (guarded) | archive -> `scripts/archive/` |
| `apply_org_group_rbac.py` | 2026-09-03 | 1455 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh, update.sh) | DB/DDL or idempotent patch; run on every deploy | DELETE FROM | keep at root |
| `apply_org_groups_ui_and_role_sync_fix.py` | 2026-09-04 | 684 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh) | DB/DDL or idempotent patch; run on every deploy | - | keep at root |
| `apply_permission_rbac_migration.py` | 2026-09-04 | 101 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh, update.sh) | DB/DDL or idempotent patch; run on every deploy | - | keep at root |
| `apply_permission_rbac_system.py` | 2026-09-04 | 897 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh, update.sh) | DB/DDL or idempotent patch; run on every deploy | root123 in printed hints (fixed) | keep at root |
| `apply_resources_region_column_fix.py` | 2026-08-26 | 158 | **LIVE** (deploy/deploy.sh, deploy/update.sh, setup.sh, update.sh) | DB/DDL or idempotent patch; run on every deploy | root123 fallback (fixed) | keep at root |
| `apply_simplify_s3_charts.py` | 2026-09-10 | 218 | not executed | applied (frontend); rm of its own .bak only | rm of own .bak | archive -> `scripts/archive/` |
| `fix_azure_extended_resource_discovery.py` | 2026-09-06 | 360 | not executed | 10/10 inserted code blocks still present in the tree | - | archive -> `scripts/archive/` |
| `fix_azure_gcp_alert_evaluation_gap.py` | 2026-09-06 | 473 | not executed | inserted sync functions present in tree | - | archive -> `scripts/archive/` |
| `fix_gcp_extended_service_detection.py` | 2026-09-06 | 360 | not executed | 8/8 inserted code blocks still present in the tree | - | archive -> `scripts/archive/` |
| `fix_onboarding_autodetect_parity.py` | 2026-09-06 | 761 | not executed | 12/12 inserted code blocks still present in the tree | - | archive -> `scripts/archive/` |

## 2. Corrections to earlier audit claims (read this)

- **Patch 037, finding F2 was wrong.** I reported that no migration creates the modern
  `thresholds` table and that a fresh install would fail at migration 020. In fact
  `db/migrations/005b_thresholds_table.sql` (audit b12, 2026-09-24) already existed, and
  fresh installs import `db_schema_only.sql` -- a **UTF-16** mysqldump my text greps could
  not read -- which already contains the modern table with both unique keys. The script
  I added (`apply_ensure_thresholds_modern_columns.py`) was redundant, and on a live table
  its `MODIFY COLUMN metric_name/environment_id/warning/critical` would have failed with
  "Unknown column" on every deploy. Both dry-runs on dev and prod printed
  `ADD UNIQUE KEY uniq_threshold_scope` -- a name-based check that could never see the
  real key `uniq_threshold`. It had only ever been dry-run. **This patch removes the
  script and its three wire-ins.** The other parts of 037 stand.
- **Fresh-install gap (real, statically established, not yet executed).** `setup.sh` and
  `deploy.sh` run `migrate.py baseline --all-except-rollbacks`, recording migrations as
  applied *without running them*. Of 37 tables in numbered migrations, **23 have no
  creation path on a fresh install** (not in `db_schema_only.sql`, not created by any
  live apply script or app code): `metric_baseline`, `resource_relationships`, `op_events`,
  `escalation_policies`, `incidents`, `cloud_events`, `incident_alerts`, `resource_health`,
  `synthetic_checks`, `synthetic_check_results`, `slo_definitions`, `security_findings`,
  `maintenance_windows`, `status_page_components`, `rbac_service_catalog`, `rbac_scopes`,
  `permission_overrides`, `role_permissions_v2`, `role_bindings`, `access_reviews`,
  `report_jobs`, `reports`, `revoked_sessions` (migrations 020-052). Existing dev/prod are
  unaffected (they got these via `migrate.py apply`). Not patched: the fix (apply instead
  of baseline, or regenerate `db_schema_only.sql`) must be rehearsed on a scratch MySQL.

## 3. `db/backups/*.sql` -- already untracked, still in history

`db/backups/` is untracked upstream (commit `2af2ae6`) and `.gitignore`d, but two dumps
(`pre_access_scopes_20260824_101632.sql`, `pre_alert_hardening_20260825_104729.sql`) remain
in history of a publicly cloneable repo.

**Exposed:** 3 users (`admin`, `viewer`, `editor`) with bcrypt cost-12 hashes; one AWS
account id and 3 account names; 46-54 audit rows; 85-90 alert rows. **Not exposed
(verified: zero matches):** role ARNs, `client_secret`, service-account JSON, access keys.
The `cookies*.txt`/`login*.json` files named by `fix_p0_credential_leak.py` were never in
this repo's history. A scan of all 395 revisions found no real AWS key or PEM block (the one
match is AWS's documented placeholder `AKIAIOSFODNN7EXAMPLE`).

### Purge (rehearsed on a throwaway mirror: commits touching the dumps 3 -> 0, total 395
### unchanged, no reachable blob or hash string afterwards)

```
git clone --mirror https://github.com/vinayhirap/monitoring-hub-multi-cloud.git mh-purge.git
cd mh-purge.git
pip install git-filter-repo
git filter-repo --invert-paths \
  --path db/backups/pre_access_scopes_20260824_101632.sql \
  --path db/backups/pre_alert_hardening_20260825_104729.sql
git log --all --oneline -- db/backups | wc -l          # expect 0
git remote add origin https://github.com/vinayhirap/monitoring-hub-multi-cloud.git
git push origin --force --all && git push origin --force --tags
```

Then, **on dev and prod**, before the next patch: make sure nothing is unpushed, then
`git fetch origin && git reset --hard origin/main` (every commit SHA changes). Patch files
are unaffected. GitHub keeps unreachable commits reachable by SHA and in cached views --
open a "sensitive data removal" request with GitHub Support and ask them to run a GC; a
force-push alone does not remove it. Anyone who cloned earlier still has the dumps.

### Credential rotation checklist (proportionate to what was actually exposed)

1. Rotate the `admin`, `viewer` and `editor` passwords (cost-12 bcrypt: only weak
   passwords are realistically crackable, but treat all three as exposed).
   `scripts/security/fix_p0_credential_leak.py --apply` rotates admin and editor only;
   do viewer by hand.
2. `SELECT id, username, role, created_at FROM users;` -- confirm no other/unknown users.
3. Invalidate live sessions (password change bumps `token_version`; confirm).
4. Review `audit_logs` for logins since 2026-08-24 from unknown addresses.
5. **No cloud-credential rotation is needed for this leak** (none were in the dumps).
   `JWT_SECRET` was not in them either.

## 4. Other findings

- **Server IPs in tracked files.** The real dev/prod public IPs appear in 9
  tracked files (docstrings/comments of archived scripts and docs). Not secret, but
  fingerprints the infrastructure; left for a decision:
- `setup.sh`
- `deploy/enable_tls.sh`
- `preflight_check.sh`
- `app/api/admin/users.py`
- `scripts/archive/apply_fix_create_user_connection_leak.py`
- `scripts/archive/apply_fix_alb_nlb_backfill_robustness.py`
- `scripts/archive/apply_group_role_sync_and_smtp.py`
- `scripts/security/fix_p0_credential_leak.py`
- `scripts/security/fix_db_password_rotation.py`
- **`apply_multicloud_full_build.py`** -- see the table and the `MANIFEST.md` guard note.
- **Nine archived `.patch` files are tracked** despite the (later) `*.patch` ignore rule.
- **`deploy.sh` runs `apply_db_pool_leak_and_leader_election_fix.py`**, whose anchors no
  longer exist (`app/db.py` is already fixed): harmless "already patched", but stale.

## 5. Proposed root layout (only the safe parts are done in this patch)

```
setup.sh update.sh migrate.py verify_deployment.py preflight_check.sh   # entry points: keep
apply_*.py  (21 live)      # keep while the 4 shell scripts reference them; later move to
                           # scripts/migrations/ together with those edits, in one change
db_schema_only.sql seed_thresholds.sql  ->  db/base/   # needs installer edits: not done
docs/  docs/incidents/     # RCA moved here (account id redacted)
scripts/archive/ deploy/archive/                        # + MANIFEST.md in each
add_test_suite.py          # archive once tests/README.md stops pointing at it
```
Already handled by earlier patches: the six `yace-*.yml` (036 v2) and
`010_multi_cloud_credentials.sql` (this patch).
