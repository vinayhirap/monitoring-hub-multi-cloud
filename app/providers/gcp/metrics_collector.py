# app/providers/gcp/metrics_collector.py
"""
Pulls metric VALUES for a GCP account's enabled metric selection and
pushes them into VictoriaMetrics. Same rationale as the Azure collector
in this package's sibling module -- no YACE-equivalent exists for GCP,
so this actively pulls Cloud Monitoring and pushes to VM.

Cost note (different from AWS, same as Azure): Cloud Monitoring's
ListTimeSeries read API for GCP-provided ("system") metrics is free --
there is no CloudWatch-GetMetricData-style per-call billing to avoid
here. The V4 cost-avoidance patterns (describe_* polling instead of GMD,
tiered intervals to cut call volume) were specifically about AWS's
billing model; they don't need to be replicated for GCP reads.

Efficiency: unlike Azure (batched by resource, capped at 50/call) or AWS
CloudWatch (one call per metric per resource without YACE), GCP's
list_time_series is naturally fleet-wide -- one filter='metric.type="..."'
call returns that metric for EVERY resource of that type in the project
in a single response. So one GCP account with 30 Compute instances and
6 enabled instance metrics costs exactly 6 API calls per cycle (one per
metric type), regardless of instance count.
"""
import logging
import re
import time
import json

from app.db import get_connection
from app.credentials import load_credential
from app.clients.vm_client import vm_write_batch

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 600  # look back 10 min for the latest datapoint


def _slug(name: str) -> str:
    """'cpu/utilization' -> 'cpu_utilization' for the VM metric name suffix."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return s or "value"


def _enabled_gcp_metrics(cur, account_id: int):
    """[(namespace, service, metric_name), ...] for this account's enabled selection."""
    cur.execute("""
        SELECT mc.namespace, mc.service, mc.metric_name
        FROM metric_catalog mc
        JOIN account_metric_selections ams ON ams.metric_id = mc.id
        WHERE ams.aws_account_id = %s AND ams.enabled = 1
              AND mc.provider = 'gcp' AND mc.metric_name IS NOT NULL AND mc.metric_name != ''
    """, (account_id,))
    return cur.fetchall()


def collect_account_metrics(account: dict) -> dict:
    """
    account: a row from aws_accounts (dict) for one GCP account. Must have
    id, project_id, and a service-account key stored via app.credentials.

    Returns {"pushed": int, "metric_types_queried": int, "errors": [str, ...]}.
    Never raises -- see the Azure collector's docstring for why.
    """
    result = {"pushed": 0, "metric_types_queried": 0, "errors": []}

    project_id = (account.get("project_id") or "").strip()
    sa_key_json = load_credential(account["id"])
    if not (project_id and sa_key_json):
        result["errors"].append("missing project_id/service account credential")
        return result

    try:
        from google.cloud import monitoring_v3
        from google.oauth2 import service_account as gcp_service_account
    except ImportError:
        result["errors"].append("google-cloud-monitoring not installed (pip install -r requirements.txt)")
        return result

    try:
        info = json.loads(sa_key_json)
        creds = gcp_service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/monitoring.read"]
        )
        client = monitoring_v3.MetricServiceClient(credentials=creds)
    except Exception as e:
        result["errors"].append(f"auth/client setup failed: {e}")
        return result

    project_name = f"projects/{project_id}"
    now = time.time()
    interval = monitoring_v3.TimeInterval({
        "end_time": {"seconds": int(now)},
        "start_time": {"seconds": int(now - _WINDOW_SECONDS)},
    })

    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        enabled = _enabled_gcp_metrics(cur, account["id"])
    finally:
        cur.close(); conn.close()

    if not enabled:
        return result

    for row in enabled:
        metric_type = f"{row['namespace']}/{row['metric_name']}"
        result["metric_types_queried"] += 1
        try:
            time_series = client.list_time_series(
                request={
                    "name": project_name,
                    "filter": f'metric.type = "{metric_type}"',
                    "interval": interval,
                    "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                }
            )
        except Exception as e:
            result["errors"].append(f"{metric_type}: {e}")
            continue

        series = []
        try:
            for ts in time_series:
                if not ts.points:
                    continue
                latest = ts.points[0]  # Cloud Monitoring returns points newest-first
                kind = latest.value._pb.WhichOneof("value")
                if kind == "double_value":
                    value = latest.value.double_value
                elif kind == "int64_value":
                    value = float(latest.value.int64_value)
                elif kind == "bool_value":
                    value = 1.0 if latest.value.bool_value else 0.0
                else:
                    continue  # distribution/string values aren't a single scalar -- skip

                labels = {"account_id": str(account["id"]), "project_id": project_id}
                for k, v in dict(ts.resource.labels).items():
                    labels[k] = str(v)

                series.append({
                    "metric": f"gcp_{row['service']}_{_slug(row['metric_name'])}",
                    "labels": labels,
                    "value": value,
                })
        except Exception as e:
            result["errors"].append(f"{metric_type}: error parsing results: {e}")
            continue

        if series:
            if vm_write_batch(series):
                result["pushed"] += len(series)
            else:
                result["errors"].append(f"{metric_type}: VM write failed for {len(series)} points")

    return result


def collect_all_gcp_accounts() -> dict:
    """Runs collect_account_metrics() for every active GCP account. Used by the scheduler."""
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT * FROM aws_accounts
        WHERE status = 'active' AND provider = 'gcp'
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
                logger.warning(f"[gcp collector] account={account['id']} {e}")
    return totals
