# app/providers/azure/metrics_collector.py
"""
Pulls metric VALUES for an Azure account's enabled metric selection and
pushes them into VictoriaMetrics.

Why this exists: AWS's tiered pipeline is YACE (a standalone Prometheus
exporter binary) scraping CloudWatch and pushing to VM on its own -- no
Python collector loop is involved for AWS metric *values* anymore (see
app/collector/scheduler.py's comment: "GMD collection skipped -- VM/YACE
migration in progress"). There is no YACE-equivalent for Azure, so
something has to actively pull Azure Monitor and push to VM. This is that
something.

Cost note (different from AWS): Azure Monitor's platform-metric READ API
(what MetricsClient.query_resources calls) is NOT billed per-call the way
AWS CloudWatch GetMetricData is -- platform metrics are included at no
extra charge. The aggressive GMD-avoidance work done in V4 doesn't apply
here the same way; polling on a short interval isn't a cost problem for
Azure the way it was for AWS. (Custom/non-platform Azure metrics and very
high query volume can still incur charges -- this collector only touches
platform metrics from CURATED, which are free reads.)

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
from app.clients.vm_client import vm_write_batch

logger = logging.getLogger(__name__)

_BATCH_SIZE = 50  # Azure Monitor Metrics Batch API hard limit per call


def _slug(name: str) -> str:
    """'Percentage CPU' -> 'percentage_cpu' for the VM metric name suffix."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return s or "value"


def _enabled_azure_metrics(cur, account_id: int):
    """{(namespace, service): {metric_name, ...}} for this account's enabled selection."""
    cur.execute("""
        SELECT mc.namespace, mc.service, mc.metric_name
        FROM metric_catalog mc
        JOIN account_metric_selections ams ON ams.metric_id = mc.id
        WHERE ams.aws_account_id = %s AND ams.enabled = 1
              AND mc.provider = 'azure' AND mc.metric_name IS NOT NULL AND mc.metric_name != ''
    """, (account_id,))
    grouped = {}
    for row in cur.fetchall():
        key = (row["namespace"], row["service"])
        grouped.setdefault(key, set()).add(row["metric_name"])
    return grouped


def collect_account_metrics(account: dict) -> dict:
    """
    account: a row from aws_accounts (dict) for one Azure account. Must have
    id, tenant_id, client_id, subscription_id, default_region.

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
        # Azure Monitor's Metrics data-plane endpoint is regional, matching
        # the account's own default_region (Azure region short-name, e.g.
        # "centralindia" -- NOT an AWS-style region code).
        endpoint = f"https://{region}.metrics.monitor.azure.com"
        client = MetricsClient(endpoint, cred)
    except Exception as e:
        result["errors"].append(f"auth/client setup failed: {e}")
        return result

    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        by_service = _enabled_azure_metrics(cur, account["id"])
        if not by_service:
            return result

        for (namespace, service), metric_names in by_service.items():
            cur.execute("""
                SELECT id, resource_id, name FROM resources
                WHERE aws_account_id = %s AND resource_type = %s
            """, (account["id"], service))
            resources = cur.fetchall()
            if not resources:
                continue
            result["resources_queried"] += len(resources)

            for start in range(0, len(resources), _BATCH_SIZE):
                chunk = resources[start:start + _BATCH_SIZE]
                chunk_uris = [r["resource_id"] for r in chunk]
                try:
                    query_results = client.query_resources(
                        resource_ids=chunk_uris,
                        metric_namespace=namespace,
                        metric_names=list(metric_names),
                        timespan=timedelta(minutes=10),
                        granularity=timedelta(minutes=1),
                        aggregations=[MetricAggregationType.AVERAGE],
                    )
                except Exception as e:
                    result["errors"].append(f"{service} ({namespace}): {e}")
                    continue

                # MetricsClient.query_resources returns results in the same
                # order as resource_ids -- there's no resource_id field on
                # the result object itself (verified against the SDK's
                # MetricsQueryResult dataclass), so map back positionally.
                series = []
                for resource_row, query_result in zip(chunk, query_results):
                    for metric in query_result.metrics:
                        for ts_elem in metric.timeseries:
                            if not ts_elem.data:
                                continue
                            latest = ts_elem.data[-1]  # most recent datapoint in the window
                            value = latest.average
                            if value is None:
                                continue
                            series.append({
                                "metric": f"azure_{service}_{_slug(metric.name)}",
                                "labels": {
                                    "account_id": str(account["id"]),
                                    "resource_id": resource_row["resource_id"],
                                    "resource_name": resource_row["name"] or "",
                                    "region": region,
                                },
                                "value": float(value),
                            })
                if series:
                    if vm_write_batch(series):
                        result["pushed"] += len(series)
                    else:
                        result["errors"].append(f"{service}: VM write failed for {len(series)} points")
    finally:
        cur.close(); conn.close()

    return result


def collect_all_azure_accounts() -> dict:
    """Runs collect_account_metrics() for every active Azure account. Used by the scheduler."""
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT * FROM aws_accounts
        WHERE status = 'active' AND provider = 'azure'
    """)
    accounts = cur.fetchall()
    cur.close(); conn.close()

    totals = {"accounts": len(accounts), "pushed": 0, "errors": []}
    for account in accounts:
        r = collect_account_metrics(account)
        totals["pushed"] += r["pushed"]
        if r["errors"]:
            totals["errors"].append({"account_id": account["id"], "errors": r["errors"]})
            for e in r["errors"]:
                logger.warning(f"[azure collector] account={account['id']} {e}")
    return totals
