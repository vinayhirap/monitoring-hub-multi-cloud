# Archived scripts

These 53 files were moved out of the repo root on 2026-09-06 because a
repo-wide search (`grep -rl -- "<filename>" .`, all file types, excluding
.git) found zero references to any of them anywhere in the tracked repo --
not called by setup.sh/deploy.sh/update.sh (root or deploy/), not imported
or shelled out to by any other script, not mentioned in any doc or config.

They are one-off historical fixes/patches, already applied by hand to
whatever environment they were written for, at some point in the past.
Nothing is deleted -- full git history is preserved via this move, and a
local backup was also taken before moving (see the fix script's own
console output for the backup path).

A second, smaller cluster of scripts that reference EACH OTHER (but are
still not called from any live deploy path) was deliberately left at the
repo root -- confirming those are truly dead requires tracing each small
reference chain individually, which wasn't done as part of this pass.

## Archived files
- `add_ap_south_2_region.py`
- `apply-metric-selector-redesign.ps1`
- `apply_alert_deeplink_fix.py`
- `apply_alert_stale_tab_fix.py`
- `apply_alert_toast_account_region_fix.py`
- `apply_backend_shutdown_partition_fix.py`
- `apply_branding.py`
- `apply_cloud_selector_onboarding_shell.py`
- `apply_console_account_locked_signin.py`
- `apply_console_fix.py`
- `apply_console_scope_and_dynamic_resources_fix_1.py`
- `apply_console_self_federation.py`
- `apply_console_url_consolidation_frontend.py`
- `apply_core_only_tiles.py`
- `apply_dashboard_data_cache.py`
- `apply_directory_tier_resource_counts_fix.py`
- `apply_ec2_memory_disk_metrics.py`
- `apply_editor_account_scoping.py`
- `apply_fast_resource_counts_fix.py`
- `apply_fix_slow_s3_bucket_detail_calls.py`
- `apply_group_role_sync_and_smtp.py`
- `apply_icons_sidebar_noc_removal.py`
- `apply_multi_cloud_providers_backend.py`
- `apply_multicloud_step4_7_collectors.py`
- `apply_overview_polish_and_user_groups.py`
- `apply_overview_reliability_fix.py`
- `apply_phase0b_frontend_session_auth.py`
- `apply_provider_abstraction_layer.py`
- `apply_region_bug_and_discovery_expansion.py`
- `apply_remove_group_hint_text.py`
- `apply_service_route_fix.py`
- `apply_servicelist_reconcile.py`
- `apply_stale_alert_fix.ps1`
- `apply_strict_resource_filter.py`
- `apply_timezone_selector.py`
- `auto-detect-metrics.patch`
- `check_metrics.ps1`
- `cleanup_backend.py`
- `fix-broken-main.patch`
- `fix_mojibake.py`
- `fix_org_group_and_permission_rbac.py`
- `full-multicloud-discovery.patch`
- `metric-catalog-bugfixes.patch`
- `metric-catalog-feature.patch`
- `metric-catalog-seed-fix2.patch`
- `multi-cloud-auto-detect-resolved.patch`
- `multi-cloud-auto-detect.patch`
- `onboarding-wizard-auto-detect-ui.patch`
- `patch_ec2_network_stat.py`
- `patch_seed_script_dotenv.py`
- `sync_cloudwatch_alarm_thresholds_.py`
- `test_query.ps1`
- `validate_yace_namespaces.sh`

---

## Second archiving pass — 2026-09-12

51 more files moved out of the repo root/scripts/ during a full security
audit, using the same methodology as the first pass above, tightened in
two ways after discovering gaps in the original approach:

1. Reference search widened to the WHOLE repo (all file types, excluding
   .git and this scripts/archive/ folder itself, since MANIFEST.md and
   the archiving scripts legitimately mention already-archived
   filenames) -- not just `grep -rl`, which had previously been
   silently matching `.git/index` (a binary file that lists every
   tracked path, making everything look "referenced").
2. A file is kept at its current location if referenced by LIVE
   application code (`app/`, `frontend/src/`, `db/`) OR by anything
   that documents its own location by relative path (`tests/`,
   `deploy/`, root-level `*.md`/`README*`) -- moving those would make
   an existing "see X.py" reference point at nothing. ~35 files fit
   this and were deliberately left in place; see e.g.
   `apply_permission_rbac_system.py`, `apply_db_pool_leak_and_leader_
   election_fix.py`, `migrate.py`.

A handful of scripts/-level files were pulled OUT of the initial
zero-live-reference candidate list after reading their actual content,
because their names were misleading relative to their real purpose:
`verify_deployment.py` (a genuinely general-purpose post-deploy sanity
check, not tied to one incident) and
`scripts/sync_cloudwatch_alarm_thresholds.py` (designed to be re-run
any time an operator adds a CloudWatch alarm externally, not a
one-time historical fix) were both kept in place despite having zero
inbound references anywhere.

### Archived files (second pass)
- `apply_account_health_rollup_fix.py`
- `apply_add_gcp_extended_metric_resolvers.py`
- `apply_alb_key_and_service_tile_fix.py`
- `apply_alert_evaluation_hardening_code_fix.py`
- `apply_alert_window_consistency_fix.py`
- `apply_console_direct_link_fix.py`
- `apply_console_link_revert_to_manual_signin.py`
- `apply_console_scope_and_dynamic_resources_fix.py`
- `apply_dynamic_service_tiles_and_console_fix.py`
- `apply_extended_service_resource_counts_fix.py`
- `apply_fix_alb_nlb_backfill_robustness.py`
- `apply_fix_create_user_connection_leak.py`
- `apply_fix_hardcoded_chart_thresholds.py`
- `apply_fix_has_data_case_bug.py`
- `apply_fix_has_data_naming_map.py`
- `apply_fix_self_assume_check.py`
- `apply_fix_slow_account_summary_credentials.py`
- `apply_fix_stale_metrics_cache.py`
- `apply_fix_stale_yace_documentation.py`
- `apply_fix_upsert_threshold_nameerror.py`
- `apply_harden_group_policy_scope_check.py`
- `apply_multicloud_step3_4.py`
- `apply_phase0_jwt_auth.py`
- `apply_phase1_authorization_service.py`
- `apply_resource_counts_duplicate_route_fix.py`
- `apply_restore_console_federation.py`
- `apply_same_account_role_fix.py`
- `apply_same_account_role_fix_v2.py`
- `apply_unify_chart_titles_with_catalog.py`
- `fix_archive_orphaned_root_scripts.py` (the script that performed the FIRST archiving pass, above -- now historical itself)
- `fix_console_link_multicloud_gaps.py`
- `fix_deploy_script_drift.py`
- `fix_drop_dead_legacy_tables.py`
- `fix_env_hygiene.py`
- `fix_gcp_console_url_dispatch.py`
- `fix_onboarding_hint_text_parity.py`
- `fix_overview_aws_branding.py`
- `fix_restore_authz_privilege_escalation_warning.py`
- `fix_settings_yace_provider_gating.py`
- `fix_user_access_scope_cloud_field.py`
- `apply_ebs_fix.py` (was `scripts/apply_ebs_fix.py`)
- `apply_global_region_fix.py` (was `scripts/apply_global_region_fix.py`)
- `audit_all_metrics.py` (was `scripts/audit_all_metrics.py`)
- `check_catalog_metrics.py` (was `scripts/check_catalog_metrics.py`)
- `disable_unsupported_metrics_v2.py` (was `scripts/disable_unsupported_metrics_v2.py`)
- `enable_ebs_bytes.py` (was `scripts/enable_ebs_bytes.py`)
- `find_unsupported_namespaces.py` (was `scripts/find_unsupported_namespaces.py`)
- `import_health_rules.py` (was `scripts/import_health_rules.py` -- note: this file has a hardcoded `"password": "YOUR_PASSWORD"` placeholder in a DB_CONFIG dict; it's a template value, not a real leaked credential, but is flagged here since it's inconsistent with this codebase's normal environment-variable-only convention)
- `resource_inventory_check.py` (was `scripts/resource_inventory_check.py`)
- `verify_account_health_fix.py` (was `scripts/verify_account_health_fix.py`)
- `verify_ebs_metrics.py` (was `scripts/verify_ebs_metrics.py`)

## Added 2026-09-25 (audit chat 25/39, security fix scripts A)
- `security/fix_wire_up_permissions_and_scope.py` (was
  `scripts/security/fix_wire_up_permissions_and_scope.py`) -- one-shot
  patcher that wired `require_permission`/scope checks into
  admin/accounts.py, alerts.py, settings.py, metric_catalog.py,
  audit_logs.py and live_data.py. Confirmed fully applied at both
  `8a25a2f` (this slice's base commit) and again here at current
  `main`: every route in all 6 target files carries the intended
  dependency, and every account/resource-keyed route calls a scope
  check. `grep -rl` for the filename found zero references outside
  `.git/index` at either point.
- `security/fix_rbac_decouple_role_from_groups.py` (was
  `scripts/security/fix_rbac_decouple_role_from_groups.py`) -- one-shot
  patcher that removed the `UPDATE users SET role = ...`
  privilege-escalation bug from add_group_members() and the matching
  `GROUP_LEVEL_ROLE` auto-sync in authorization.py and
  UserManagement.jsx. Confirmed fully applied, same double-check as
  above -- no `GROUP_LEVEL_ROLE` or role-sync UPDATE remains anywhere
  in the current tree, zero external references.

## Third pass -- 2026-09-28 (audit chat 39/39, R01)

Criterion for THIS pass (different from the two above, which required zero
references): a script is archived when it is **not executed anywhere** --
no `run_migration`/shell call in setup.sh, update.sh, deploy/deploy.sh,
deploy/update.sh; no Python import, subprocess or runpy; no glob -- and is
mentioned only in comments/docstrings (including inside other archived
scripts). This is the "scripts that reference each other" cluster the first
pass deliberately left; each was traced. 21 root `apply_*.py` scripts ARE
executed by the install/deploy/update scripts and stay at the root: those
scripts still say new migrations ship as root-level `apply_*.py`, so moving
them means editing all four shell scripts in one reviewed change.

"Applied" evidence is mechanical: the code each script embeds is looked up in
the current tree. A partial match means the code was applied and has since
evolved, not that it is unapplied. Nothing here is deleted -- `git mv` keeps
history. Do not re-run these against the current tree.

### Moved

- `apply_add_cwagent_disk_threshold.py` (was `apply_add_cwagent_disk_threshold.py` at repo root; added 2026-09-09) -- inserted collector functions present in tree
- `apply_add_cwagent_mem_threshold.py` (was `apply_add_cwagent_mem_threshold.py` at repo root; added 2026-09-09) -- `_collect_ec2_cwagent_mem` present (rest superseded by the later mem-dimension fix)
- `apply_add_extended_service_discovery.py` (was `apply_add_extended_service_discovery.py` at repo root; added 2026-09-10) -- 6/6 inserted code blocks still present in the tree
- `apply_add_warning_threshold_line.py` (was `apply_add_warning_threshold_line.py` at repo root; added 2026-09-10) -- applied (frontend); rm of its own .bak only
- `apply_check_thresholds_local_metrics.py` (was `apply_check_thresholds_local_metrics.py` at repo root; added 2026-09-08) -- 5/6 inserted code blocks still present in the tree
- `apply_cleanup_disk_mount_noise.py` (was `apply_cleanup_disk_mount_noise.py` at repo root; added 2026-09-10) -- one-time DB data cleanup (disk_mounts.py filter is in place). DESTRUCTIVE if re-run: DELETEs from 5 tables by a lossy heuristic
- `apply_dashboard_charts_metric_history.py` (was `apply_dashboard_charts_metric_history.py` at repo root; added 2026-09-08) -- 15/23 inserted code blocks still present in the tree
- `apply_final_cleanup.py` (was `apply_final_cleanup.py` at repo root; added 2026-09-08) -- 7/12 inserted code blocks still present in the tree
- `apply_fix_alb_healthy_hosts.py` (was `apply_fix_alb_healthy_hosts.py` at repo root; added 2026-09-09) -- 12/20 inserted code blocks still present in the tree
- `apply_fix_alb_healthy_hosts_history.py` (was `apply_fix_alb_healthy_hosts_history.py` at repo root; added 2026-09-09) -- 5/5 inserted code blocks still present in the tree
- `apply_fix_cwagent_mem_dimensions.py` (was `apply_fix_cwagent_mem_dimensions.py` at repo root; added 2026-09-09) -- 2 of 3 inserted functions present
- `apply_fix_getthreshold_scope.py` (was `apply_fix_getthreshold_scope.py` at repo root; added 2026-09-09) -- `getThreshold` defined in ServiceDetail.jsx
- `apply_fix_nlb_ghost_thresholds.py` (was `apply_fix_nlb_ghost_thresholds.py` at repo root; added 2026-09-10) -- 6/6 inserted code blocks still present in the tree
- `apply_fix_stale_alerts_for_stopped_instances.py` (was `apply_fix_stale_alerts_for_stopped_instances.py` at repo root; added 2026-09-09) -- `_auto_resolve_stale_alerts` present in alert_evaluator.py (code evolved since)
- `apply_fix_stale_cutoff_too_aggressive.py` (was `apply_fix_stale_cutoff_too_aggressive.py` at repo root; added 2026-09-09) -- 1/1 inserted code blocks still present in the tree
- `apply_fix_threshold_resource_type_everywhere.py` (was `apply_fix_threshold_resource_type_everywhere.py` at repo root; added 2026-09-09) -- 8/9 inserted code blocks still present in the tree
- `apply_hide_no_data_metrics.py` (was `apply_hide_no_data_metrics.py` at repo root; added 2026-09-09) -- 12/19 inserted code blocks still present in the tree
- `apply_list_view_snapshots_metrics.py` (was `apply_list_view_snapshots_metrics.py` at repo root; added 2026-09-08) -- 7/14 inserted code blocks still present in the tree
- `apply_metrics_to_monitor_cleanup.py` (was `apply_metrics_to_monitor_cleanup.py` at repo root; added 2026-09-09) -- 8/9 inserted code blocks still present in the tree
- `apply_multicloud_full_build.py` (was `apply_multicloud_full_build.py` at repo root; added 2026-08-23) -- UNVERIFIED whether ever run; wholly superseded (earliest script). DANGEROUS: overwrites 18 files from 2026-08-23 snapshots -- guarded on archive
- `apply_simplify_s3_charts.py` (was `apply_simplify_s3_charts.py` at repo root; added 2026-09-10) -- applied (frontend); rm of its own .bak only
- `fix_azure_extended_resource_discovery.py` (was `fix_azure_extended_resource_discovery.py` at repo root; added 2026-09-06) -- 10/10 inserted code blocks still present in the tree
- `fix_azure_gcp_alert_evaluation_gap.py` (was `fix_azure_gcp_alert_evaluation_gap.py` at repo root; added 2026-09-06) -- inserted sync functions present in tree
- `fix_gcp_extended_service_detection.py` (was `fix_gcp_extended_service_detection.py` at repo root; added 2026-09-06) -- 8/8 inserted code blocks still present in the tree
- `fix_onboarding_autodetect_parity.py` (was `fix_onboarding_autodetect_parity.py` at repo root; added 2026-09-06) -- 12/12 inserted code blocks still present in the tree
- `010_multi_cloud_credentials.sql` (was at repo root) -- reference copy (its own header says "do not run directly"); DDL now covered by tracked migration 071 and live `apply_multi_cloud_credentials.py`.

### Safety guard added on archive

`apply_multicloud_full_build.py` embeds whole-file snapshots (base64) and used
to OVERWRITE 18 files by default (only `--dry-run` was opt-in). Those files have
since been changed by up to 23 commits each, including security fixes, so running
it silently reverts them. It now refuses to write unless
`--i-understand-this-overwrites-current-files` is passed; `--dry-run` is still safe.

### Deleted (not archived)

- `emoji-to-icons-update/` (9 files) -- byte-identical to the real `frontend/src` files
  at commit `5685bd8` (the commit that added them); the real files have since evolved.
  Fully recoverable from git history.
