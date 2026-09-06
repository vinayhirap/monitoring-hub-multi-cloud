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
