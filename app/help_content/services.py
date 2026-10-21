# app/help_content/services.py
"""Per-service help sections GENERATED from the metric catalogs and the polling model, so the
metric lists, units, tiers, thresholds and free/paid labels cannot drift from the code.

Sources: app/aws|providers/*/metric_catalog_data.py (CURATED, DIRECTORY), app/metric_display.polling_info(),
app/threshold_defaults.py, app/collector/polling_model.py.
"""
from functools import lru_cache

from .blocks import h, p, table, note, section, fmt_interval

GROUP = "Services and resource pages"

_CLOUD_LABEL = {"aws": "AWS", "azure": "Azure", "gcp": "GCP"}
_BILLING = {
    "aws": "Paid: CloudWatch GetMetricData, billed per metric requested",
    "azure": "Paid after the free allowance: Azure Monitor, per 1,000 calls (1M free per subscription per month)",
    "gcp": "Paid after the free allowance: Cloud Monitoring ListTimeSeries, per series (1M free per billing account per month)",
}


def _catalogs():
    from app.aws.metric_catalog_data import CURATED as aws, DIRECTORY as aws_dir
    from app.providers.azure.metric_catalog_data import CURATED as az, DIRECTORY as az_dir
    from app.providers.gcp.metric_catalog_data import CURATED as gcp, DIRECTORY as gcp_dir
    return {"aws": (aws, aws_dir), "azure": (az, az_dir), "gcp": (gcp, gcp_dir)}


def _threshold_cell(metric_name, db_name):
    from app import threshold_defaults as td
    if td.is_static_only_metric(db_name) or td.is_static_only_metric(metric_name):
        mode = "static only"
    else:
        mode = "static or dynamic"
    d = td.DEFAULT_THRESHOLDS.get(metric_name)
    if d is None:
        return f"no default (placeholder); {mode}"
    if td.is_placeholder_threshold(*d):
        return f"placeholder (anomaly-only); {mode}"
    w, c, cmp_ = d
    return f"warn {w:g} / crit {c:g} ({cmp_}); {mode}"


def _billing(provider, service, metric_name, tier):
    from app.collector import polling_model as pm
    if tier == "describe":
        return "Free (Describe API)"
    if provider == "aws":
        if (service, metric_name) in pm.AWS_EXTENDED_DERIVED:
            return "Free (computed locally)"
        if (service, metric_name) in pm.AWS_EXTENDED_UNSUPPORTED:
            return "Never queried (not published per resource)"
    return "Paid"


def _service_section(provider, key, entry):
    from app import metric_display as md
    from app.threshold_defaults import resolve_db_metric_name
    display, namespace, category, metrics = entry
    label = _CLOUD_LABEL[provider]
    rows = []
    for (name, unit, stat, is_default, desc) in metrics:
        db = resolve_db_metric_name(key, name) if provider == "aws" else name.lower()
        info = md.polling_info(provider, key, name, db)
        tier = info.get("tier")
        rows.append([
            name, desc, unit, stat, "yes" if is_default else "no",
            f"{fmt_interval(info.get('interval_seconds'))} ({tier})" if tier else "-",
            _billing(provider, key, name, tier),
            _threshold_cell(name, db),
        ])
    blocks = [
        p(f"{display} ({label} namespace {namespace}). Catalog category: {category}. "
          f"{len(metrics)} curated metric(s)."),
        p("Polled every = the interval and tier used once the metric is collected for a resource; "
          "'In default template' = part of the recommended onboarding selection. Choose which metrics "
          "an account collects in Settings, Metrics to Monitor."),
        table(["Metric", "What it measures", "Unit", "Statistic", "In default template", "Polled every",
               "Cloud call", "Default threshold"], rows),
        note(_BILLING[provider] + ". Prices change: check the cloud bill, this page states none."),
    ]
    return section(f"svc-{provider}-{key}", f"{label}: {display}", GROUP, "viewer", "metrics.view", blocks,
                   summary=f"{display} metrics, units, polling interval, thresholds and cost class")


@lru_cache(maxsize=1)
def service_sections():
    out = []
    for provider, (curated, _directory) in _catalogs().items():
        for key, entry in curated.items():
            out.append(_service_section(provider, key, entry))
    return out


@lru_cache(maxsize=1)
def directory_section():
    cats = _catalogs()
    rows = []
    for provider, (curated, directory) in cats.items():
        rows.append([_CLOUD_LABEL[provider], len(curated), len(directory)])
    return section(
        "services-directory", "Services without a curated metric list", GROUP, "viewer", "metrics.view", [
            p("Besides the curated services, each cloud catalog registers more services by namespace only. "
              "Their metric names are not hand-listed: use Discover (Settings, Metrics to Monitor) to list what "
              "a given account publishes."),
            table(["Cloud", "Curated services (own page in this guide)", "Directory-only services"], rows),
            note("AWS discovery uses CloudWatch ListMetrics, which is a paid call per request. Only users with "
                 "the metric_catalog.discover permission can run it (viewers do not have it by default)."),
            p("AWS Shield Advanced (AWS/DDoSProtection) is a directory-only entry: there is no dedicated Shield page "
              "or curated metric list in CloudOps."),
        ], summary="Directory-only services and Discover")
