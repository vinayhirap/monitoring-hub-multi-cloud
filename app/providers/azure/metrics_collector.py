# app/providers/azure/metrics_collector.py
"""
Pulls metric VALUES for an Azure account's enabled metric selection and
writes them DIRECTLY into the local `metrics` last-value cache and
`metric_history` table -- Azure's counterpart to Phase 1's AWS direct
GetMetricData revival (see apply_direct_gmd_metrics_revival.py). Before
Phase 2 (apply_azure_direct_metrics_fetch.py) this pushed into
VictoriaMetrics instead, and metrics_vm_sync.py pulled the values back out
again for alert_evaluator.py to read -- an unnecessary VM round-trip once
Azure Monitor is already being called directly here. GCP is unaffected --
see Phase 3.

Why a Python collector loop exists here at all (unlike AWS, which uses
YACE, a standalone Prometheus exporter binary scraping CloudWatch on its
own): there is no YACE-equivalent for Azure, so something has to actively
pull Azure Monitor. This is that something.

Cost note (different from AWS): Azure Monitor's platform-metric READ API
(what MetricsClient.query_resources calls) is NOT billed per-call the way
AWS CloudWatch GetMetricData is, up to a real ceiling: Microsoft's own
pricing page lists platform-metric queries as free for the first
1,000,000 API calls/month/billing account, billed per 1,000 calls above
that (Microsoft-confirmed; exact above-ceiling rate not independently
re-verified here -- check the live Azure Monitor pricing page). At this
app's likely scale (dozens-to-low-hundreds of calls/day across enabled
services) that ceiling isn't close to being hit, so "free in practice"
still holds -- but it is not unconditionally free the way platform-metric
*ingestion* is, and the core/extended split in multicloud_scheduler.py
exists partly to keep it that way as more services get enabled. See
monitoring-hub-metric-audit.md §3.2. The aggressive GMD-avoidance work
done in V4 doesn't apply here the same way; polling on a short interval
isn't a *current* cost problem for Azure the way it was for AWS. (Custom/
non-platform Azure metrics and very high query volume can still incur
charges -- this collector only touches platform metrics from CURATED.)

Batching: MetricsClient.query_resources() accepts up to 50 resource IDs
per call for one metric_namespace + a list of metric_names in a single
request -- so one Azure account with, say, 30 VMs and 6 enabled VM
metrics costs exactly 1 API call per collection cycle for that service,
not 30 or 180.
"""
import logging
import re
from datetime import timedelta

from app.db import get_connection
from app.credentials import load_credential
from app.collector.metrics_writer import write_metrics_batch, write_metric_history_batch

# Real Azure region short-names (eastus2, centralindia, westeurope, ...)
# are pure lowercase letters/digits, never dots/slashes/colons.
_VALID_AZURE_REGION_RE = re.compile(r"^[a-z0-9]+$")

logger = logging.getLogger(__name__)

_BATCH_SIZE = 50  # Azure Monitor Metrics Batch API hard limit per call


def _enabled_azure_metrics(cur, account_id: int, categories=None, only_metric_names=None):
    """{(namespace, service): {metric_name, ...}} for this account's enabled
    selection. categories: optional iterable of metric_catalog.category
    values ('core','extended','directory') to restrict to -- see
    collect_account_metrics()'s docstring for why this exists. None (the
    default) preserves the original behavior of collecting every enabled
    metric regardless of category, for callers that don't opt into tiering.

    only_metric_names: optional {service: {metric_name, ...}} (see
    app/providers/azure/severity_tiers.py) applied as a Python-side filter
    AFTER the category-based SQL query above -- this is the severity-tier
    pass filter multicloud_scheduler.py uses to split 'core' into
    critical/standard/low and 'extended' into fast/slow without any
    metric_catalog schema change. A service key present in `categories`'
    result but absent from only_metric_names is dropped entirely for this
    pass (it belongs to a different severity tier); a service present in
    both is restricted to the intersection of its enabled metrics and this
    tier's allowlist for that service.
    """
    query = """
        SELECT mc.namespace, mc.service, mc.metric_name, mc.category
        FROM metric_catalog mc
        JOIN account_metric_selections ams ON ams.metric_id = mc.id
        WHERE ams.aws_account_id = %s AND ams.enabled = 1
              AND mc.provider = 'azure' AND mc.metric_name IS NOT NULL AND mc.metric_name != ''
    """
    params = [account_id]
    if categories:
        placeholders = ",".join(["%s"] * len(categories))
        query += f" AND mc.category IN ({placeholders})"
        params.extend(categories)
    cur.execute(query, params)
    grouped = {}
    directory = {}
    for row in cur.fetchall():
        key = (row["namespace"], row["service"])
        grouped.setdefault(key, set()).add(row["metric_name"])
        if row.get("category") == "directory":
            directory.setdefault(key, set()).add(row["metric_name"])

    if only_metric_names is not None:
        filtered = {}
        for (namespace, service), names in grouped.items():
            allowed = set(only_metric_names.get(service) or ())
            # Directory-category metrics are user-chosen, not tiered by
            # name -- a pass that asked for 'directory' collects them all
            # (previously the name allowlist silently dropped every one).
            if categories and "directory" in categories:
                allowed |= directory.get((namespace, service), set())
            kept = names & allowed
            if kept:
                filtered[(namespace, service)] = kept
        grouped = filtered

    return grouped


def collect_account_metrics(account: dict, categories=None, only_metric_names=None,
                             window_seconds: int = 600) -> dict:
    """
    account: a row from aws_accounts (dict) for one Azure account. Must have
    id, tenant_id, client_id, subscription_id, default_region.

    window_seconds: how far back the Azure Monitor query looks for each
    resource/metric (the `timespan` passed to query_resources()). Default
    (600s / 10 min) matches this app's fastest tiers (critical=60s,
    standard=300s) with headroom to spare, but multicloud_scheduler.py's
    LOW (900s), EXTENDED (900s) and SLOW_EXTENDED (3600s) passes now pass
    a wider value explicitly -- see that module's per-pass call sites and
    the 2026-09-16 fix note there. Before that fix this was a hardcoded
    timedelta(minutes=10) regardless of which tier called in, so any pass
    polling less often than every 10 minutes (LOW, EXTENDED, SLOW_EXTENDED
    -- i.e. every Azure service except the small critical/standard core
    set) queried a window narrower than its own polling gap and silently
    missed whatever Azure published in between. Same bug class as AWS's
    extended.py fix (see that file's _LOOKBACK_MINUTES docstring).

    categories: optional iterable restricting collection to specific
    metric_catalog.category values ('core', 'extended', 'directory') --
    used by multicloud_scheduler.py to run core/critical Azure services
    (VM, Storage Account, SQL Database, App Service) on a tighter cadence
    than extended ones (VMSS, AKS, Cosmos DB, Redis, etc.), the same
    priority-based tiering principle AWS's scheduler.py already applies,
    adapted to Azure's actual cost shape rather than copied verbatim --
    Azure platform-metric reads are free up to 1,000,000 API calls/month/
    billing account (Microsoft-confirmed), so this tiering isn't chasing a
    per-call bill the way AWS's is; it exists to keep call volume away from
    that ceiling as more extended services are enabled, and to avoid
    polling latency-insensitive services (Key Vault, VPN Gateway trend
    metrics) as often as latency-sensitive ones (VM CPU) for no freshness
    benefit. See monitoring-hub-metric-audit.md §8 flaw #3, §9. None (the
    default) preserves the original untiered behavior.

    only_metric_names: optional per-metric severity filter -- see
    app/providers/azure/severity_tiers.py and _enabled_azure_metrics()'s
    docstring. Lets multicloud_scheduler.py split 'core' into a
    near-real-time critical pass and slower standard/low passes (and
    'extended' into fast/slow passes) without a metric_catalog schema
    change. None preserves the original category-only behavior.

    Returns {"pushed": int, "resources_queried": int, "errors": [str, ...]}.
    Never raises -- collection failures for one account/service shouldn't
    crash the scheduler loop; they're reported back for logging instead.
    """
    result = {"pushed": 0, "resources_queried": 0, "errors": []}

    tenant_id = (account.get("tenant_id") or "").strip()
    client_id = (account.get("client_id") or "").strip()
    subscription_id = (account.get("subscription_id") or "").strip()
    region = (account.get("default_region") or "").strip()
    secret = load_credential(account["id"])

    if not (tenant_id and client_id and subscription_id and secret and region):
        result["errors"].append("missing tenant_id/client_id/subscription_id/region/credential")
        return result

    try:
        from azure.identity import ClientSecretCredential
        from azure.monitor.query import MetricsClient, MetricAggregationType
    except ImportError:
        result["errors"].append("azure-monitor-query not installed (pip install -r requirements.txt)")
        return result

    try:
        cred = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=secret)
    except Exception as e:
        result["errors"].append(f"auth/client setup failed: {e}")
        return result

    # SSRF guard: every region ends up in an endpoint host name, so the
    # account default is validated before anything else happens.
    if not _VALID_AZURE_REGION_RE.match(region):
        result["errors"].append(
            f"default_region {region!r} is not a valid Azure region short-name -- refusing to build a metrics endpoint from it"
        )
        return result

    # The metrics:getBatch data-plane API only accepts resources in ONE
    # subscription + region + resource type per call, against that
    # region's own endpoint (polling audit 2026-09-23: resources outside
    # default_region used to be sent to the default region's endpoint and
    # returned nothing). One client per region, built lazily.
    clients = {}

    def _client_for(res_region):
        if res_region not in clients:
            clients[res_region] = MetricsClient(f"https://{res_region}.metrics.monitor.azure.com", cred)
        return clients[res_region]

    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        by_service = _enabled_azure_metrics(
            cur, account["id"], categories=categories, only_metric_names=only_metric_names
        )
        if not by_service:
            return result

        for (namespace, service), metric_names in by_service.items():
            cur.execute("""
                SELECT id, resource_id, name, region FROM resources
                WHERE aws_account_id = %s AND resource_type = %s
            """, (account["id"], service))
            resources = cur.fetchall()
            if not resources:
                continue
            result["resources_queried"] += len(resources)
            query_namespace = _NAMESPACE_OVERRIDES.get(service, namespace)

            by_region = {}
            for r in resources:
                res_region = _normalize_region(r.get("region")) or region
                if not _VALID_AZURE_REGION_RE.match(res_region):
                    result["errors"].append(f"{service}: resource {r['resource_id']} has invalid region {res_region!r} -- skipped")
                    continue
                by_region.setdefault(res_region, []).append(r)

            for call in _plan_calls(service, metric_names, window_seconds):
                for res_region, region_resources in by_region.items():
                    for start in range(0, len(region_resources), _BATCH_SIZE):
                        chunk = region_resources[start:start + _BATCH_SIZE]
                        _run_call(_client_for(res_region), MetricAggregationType, query_namespace,
                                  service, chunk, call, result)
    finally:
        cur.close(); conn.close()

    return result


# Function App metrics are published on the site resource (Microsoft.Web/
# sites); the catalog's "Microsoft.Web/sites/functions" namespace never
# matches a discovered site id, so every call failed.
_NAMESPACE_OVERRIDES = {"function_app": "Microsoft.Web/sites"}

# Metrics Azure only publishes at a 1-hour time grain.
_HOURLY_GRAIN_METRICS = {("storage_account", "UsedCapacity")}

# kube_pod_status_phase is only meaningful split by its `phase` dimension:
# stored as the number of pods in a non-healthy phase.
_POD_PHASE_METRIC = ("aks_cluster", "kube_pod_status_phase")
_UNHEALTHY_POD_PHASES = {"failed", "pending", "unknown"}

_AGG_ATTR = {"Average": "average", "Total": "total", "Maximum": "maximum",
             "Minimum": "minimum", "Count": "count"}


def _normalize_region(value):
    return (value or "").strip().lower().replace(" ", "")


def _statistic_for(service, metric_name):
    """Catalog statistic for a curated metric; Average for anything else
    (directory-category metrics)."""
    try:
        from app.providers.azure.metric_catalog_data import CURATED
        entry = CURATED.get(service)
        if entry:
            for m in entry[3]:
                if m[0] == metric_name and m[2] in _AGG_ATTR:
                    return m[2]
    except Exception:
        pass
    return "Average"


def _plan_calls(service, metric_names, window_seconds):
    """Group a service's metrics into API calls that share aggregation,
    time grain and dimension filter:
    [{"names", "statistic", "grain_minutes", "window_seconds", "filter"}]."""
    groups = {}
    for name in sorted(metric_names):
        stat = _statistic_for(service, name)
        hourly = (service, name) in _HOURLY_GRAIN_METRICS
        phase = (service, name) == _POD_PHASE_METRIC
        key = (stat, 60 if hourly else 1, "phase eq '*'" if phase else None)
        groups.setdefault(key, []).append(name)
    calls = []
    for (stat, grain, flt), names in groups.items():
        calls.append({
            "names": names, "statistic": stat, "grain_minutes": grain, "filter": flt,
            # an hourly-grain metric needs a window spanning >= 2 buckets
            "window_seconds": max(window_seconds, 3 * 3600) if grain == 60 else window_seconds,
        })
    return calls


def _run_call(client, agg_type, namespace, service, chunk, call, result):
    attr = _AGG_ATTR[call["statistic"]]
    aggregation = getattr(agg_type, call["statistic"].upper(), None) or agg_type.AVERAGE
    kwargs = dict(
        resource_ids=[r["resource_id"] for r in chunk],
        metric_namespace=namespace,
        metric_names=list(call["names"]),
        timespan=timedelta(seconds=call["window_seconds"]),
        granularity=timedelta(minutes=call["grain_minutes"]),
        aggregations=[aggregation],
    )
    if call["filter"]:
        kwargs["filter"] = call["filter"]
    try:
        query_results = client.query_resources(**kwargs)
    except Exception as e:
        result["errors"].append(f"{service} ({namespace}, {call['statistic']}): {e}")
        _record_usage(1)
        return
    _record_usage(1)

    # Results come back in resource_ids order (no resource id on the
    # result object) -- mapped positionally.
    metrics_rows, history_rows = [], []
    for resource_row, query_result in zip(chunk, query_results):
        for metric in query_result.metrics:
            if (service, metric.name) == _POD_PHASE_METRIC:
                _pod_phase_rows(resource_row, metric, attr, metrics_rows, history_rows)
                continue
            for ts_elem in metric.timeseries:
                latest = None
                for point in ts_elem.data or []:
                    value = getattr(point, attr, None)
                    if value is None:
                        continue
                    history_rows.append((resource_row["id"], metric.name, float(value), point.timestamp))
                    latest = value
                # newest NON-null point: Azure's newest minute is usually
                # still null when polled, which used to skip the update
                if latest is not None:
                    metrics_rows.append((resource_row["id"], metric.name, float(latest)))
    if metrics_rows:
        write_metrics_batch(metrics_rows)
        result["pushed"] += len(metrics_rows)
    if history_rows:
        write_metric_history_batch(history_rows)


def _pod_phase_rows(resource_row, metric, attr, metrics_rows, history_rows):
    by_ts = {}
    for ts_elem in metric.timeseries:
        md = {str(k).lower(): str(v).lower() for k, v in (getattr(ts_elem, "metadata_values", None) or {}).items()}
        if md.get("phase") not in _UNHEALTHY_POD_PHASES:
            continue
        for point in ts_elem.data or []:
            value = getattr(point, attr, None)
            if value is not None:
                by_ts[point.timestamp] = by_ts.get(point.timestamp, 0.0) + float(value)
    for ts in sorted(by_ts):
        history_rows.append((resource_row["id"], metric.name, by_ts[ts], ts))
    if by_ts:
        metrics_rows.append((resource_row["id"], metric.name, by_ts[max(by_ts)]))


def _record_usage(calls):
    try:
        from app.collector import api_usage
        api_usage.record("azure", _CURRENT_TIER, calls=calls, units=calls)
    except Exception:
        pass


# Tier label for api_usage (set by collect_all_azure_accounts' caller via
# the tier_label argument; the multicloud loop runs passes sequentially).
_CURRENT_TIER = "unknown"


def collect_all_azure_accounts(categories=None, only_metric_names=None, window_seconds: int = 600,
                               tier_label: str = "unknown") -> dict:
    """Runs collect_account_metrics() for every active Azure account. Used by the scheduler.
    categories, only_metric_names, window_seconds: see collect_account_metrics()'s docstring."""
    global _CURRENT_TIER
    _CURRENT_TIER = tier_label
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT * FROM aws_accounts
            WHERE status = 'active' AND provider = 'azure'
        """)
        accounts = cur.fetchall()
    finally:
        cur.close(); conn.close()

    totals = {"accounts": len(accounts), "pushed": 0, "errors": []}
    for account in accounts:
        r = collect_account_metrics(account, categories=categories, only_metric_names=only_metric_names,
                                     window_seconds=window_seconds)
        totals["pushed"] += r["pushed"]
        if r["errors"]:
            totals["errors"].append({"account_id": account["id"], "errors": r["errors"]})
            for e in r["errors"]:
                logger.warning(f"[azure collector] account={account['id']} {e}")
    return totals
