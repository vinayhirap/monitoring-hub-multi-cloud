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
