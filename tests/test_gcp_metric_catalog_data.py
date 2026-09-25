"""
Regression tests for app/providers/gcp/metric_catalog_data.py (audit b19).

Pure data module, no DB/network dependencies -- imported directly.
"""
from app.providers.gcp.metric_catalog_data import CURATED, DIRECTORY


def test_curated_has_no_duplicate_metric_names_per_service():
    for service_key, (_display, _ns, _category, metrics) in CURATED.items():
        names = [m[0] for m in metrics]
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"{service_key} has duplicate metric name(s): {dupes}"


def test_directory_has_no_duplicate_namespaces():
    ns_list = [ns for _display, ns in DIRECTORY]
    dupes = {n for n in ns_list if ns_list.count(n) > 1}
    assert not dupes, f"DIRECTORY has duplicate namespace(s): {dupes}"


def test_directory_entries_are_prefixes_not_full_metric_types():
    """
    Regression test (audit b19): DIRECTORY entries are consumed as
    starts_with() prefixes by app/api/metric_catalog.py's live
    "Discover" flow (_discover_gcp_metrics), not exact metric types.
    A DIRECTORY namespace already contained inside (i.e. more specific
    than) a CURATED service's own namespace prefix -- like the removed
    "Cloud CDN" entry, which was a single full metric type nested under
    CURATED's cloud_lb prefix -- silently discovers just that one metric
    instead of a real service family.
    """
    curated_prefixes = [v[1] for v in CURATED.values()]
    for display_name, ns in DIRECTORY:
        for prefix in curated_prefixes:
            assert not ns.startswith(prefix + "/"), (
                f"DIRECTORY entry {display_name!r} ({ns!r}) is nested under "
                f"CURATED prefix {prefix!r} -- likely a full metric type, "
                f"not a real standalone namespace prefix"
            )


def test_severity_tier_metric_names_exist_in_curated():
    """CRITICAL_METRICS/LOW_METRICS in severity_tiers.py must only ever
    reference metric names that actually exist in CURATED, for 'core'
    services only, with no name claimed by more than one tier."""
    from app.providers.gcp.severity_tiers import (
        CRITICAL_METRICS, LOW_METRICS, build_standard_metrics,
    )

    standard = build_standard_metrics(CURATED)
    for service_key in (k for k, v in CURATED.items() if v[2] == "core"):
        all_names = {m[0] for m in CURATED[service_key][3]}
        crit = CRITICAL_METRICS.get(service_key, set())
        low = LOW_METRICS.get(service_key, set())
        std = standard.get(service_key, set())

        assert crit <= all_names, f"{service_key}: CRITICAL references unknown metric(s) {crit - all_names}"
        assert low <= all_names, f"{service_key}: LOW references unknown metric(s) {low - all_names}"
        assert not (crit & low), f"{service_key}: metric(s) claimed by both CRITICAL and LOW: {crit & low}"
        assert (crit | low | std) == all_names, f"{service_key}: tiers don't fully partition metric set"
