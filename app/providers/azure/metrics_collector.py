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
from datetime import timedelta

from app.db import get_connection
from app.credentials import load_credential
from app.collector.metrics_writer import write_metrics_batch, write_metric_history_batch

logger = logging.getLogger(__name__)

_BATCH_SIZE = 50  # Azure Monitor Metrics Batch API hard limit per call


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
                #
                # metrics_rows: (resource_db_id, metric_name, value) -> latest
                #   value only, upserted into `metrics` for alert_evaluator.py.
                # history_rows: (resource_db_id, metric_name, value, timestamp)
                #   -> every returned datapoint, appended into `metric_history`.
                # metric_name here is metric.name, the SDK's echo of the exact
                # string this account's metric_catalog row requested -- matches
                # what metrics_vm_sync.py's _sync_azure_gcp_metrics() used to
                # write into `metrics` from VM, and what alert_evaluator.py's
                # join against metric_catalog expects.
                metrics_rows = []
                history_rows = []
                for resource_row, query_result in zip(chunk, query_results):
                    for metric in query_result.metrics:
                        for ts_elem in metric.timeseries:
                            if not ts_elem.data:
                                continue
                            for point in ts_elem.data:
                                value = point.average
                                if value is None:
                                    continue
                                history_rows.append((
                                    resource_row["id"], metric.name,
                                    float(value), point.timestamp,
                                ))
                            latest = ts_elem.data[-1]  # most recent datapoint in the window
                            value = latest.average
                            if value is None:
                                continue
                            metrics_rows.append((resource_row["id"], metric.name, float(value)))
                if metrics_rows:
                    write_metrics_batch(metrics_rows)
                    result["pushed"] += len(metrics_rows)
                if history_rows:
                    write_metric_history_batch(history_rows)
    finally:
        cur.close(); conn.close()

    return result


def collect_all_azure_accounts() -> dict:
    """Runs collect_account_metrics() for every active Azure account. Used by the scheduler."""
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
        r = collect_account_metrics(account)
        totals["pushed"] += r["pushed"]
        if r["errors"]:
            totals["errors"].append({"account_id": account["id"], "errors": r["errors"]})
            for e in r["errors"]:
                logger.warning(f"[azure collector] account={account['id']} {e}")
    return totals
