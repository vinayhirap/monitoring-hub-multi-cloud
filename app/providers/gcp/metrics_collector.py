# app/providers/gcp/metrics_collector.py
"""
Pulls metric VALUES for a GCP account's enabled metric selection and
writes them DIRECTLY into the local `metrics` last-value cache and
`metric_history` table -- GCP's counterpart to Phase 1 (AWS) and Phase 2
(Azure)'s direct-fetch retargets. See apply_gcp_direct_metrics_fetch.py
for the full story, including a genuine resource-matching bug this fix
found and closed for Compute specifically (Cloud Monitoring's gce_instance
labels are numeric-ID-only; this app's resources.resource_id is name-based
-- they never matched, so GCP Compute alerts likely never fired even
after the earlier fix_azure_gcp_alert_evaluation_gap.py).

Cost note (different from AWS, same as Azure): Cloud Monitoring's
ListTimeSeries read API for GCP-provided ("system") metrics is free --
there is no CloudWatch-GetMetricData-style per-call billing to avoid
here.

Efficiency: unlike Azure (batched by resource, capped at 50/call) or AWS
CloudWatch (one call per metric per resource without YACE), GCP's
list_time_series is naturally fleet-wide -- one filter='metric.type="..."'
call returns that metric for EVERY resource of that type in the project
in a single response. So one GCP account with 30 Compute instances and
6 enabled instance metrics costs exactly 6 API calls per cycle (one per
metric type), regardless of instance count.

Resource resolution: list_time_series() returns Cloud Monitoring's OWN
monitored-resource labels per series, not this app's resource_db_id, so
each series has to be matched back to a `resources` row. Resolvers below
are keyed by this app's `service` catalog field (compute_instance,
gcs_bucket, cloudsql_instance, cloud_run_service -- the 4 "core" GCP
services app/providers/gcp/discovery.py already collects) and reconstruct
the EXACT resource_id string discovery.py builds for that service, or --
for compute_instance only, since Cloud Monitoring's gce_instance type
gives a numeric ID with no name -- look up the numeric ID discovery.py
now also persists into resources.tags (see apply_gcp_direct_metrics_fetch.py).
Services with no resolver (the "extended" tier) are skipped and counted,
not guessed at.
"""
import logging
import time
import json

from app.db import get_connection
from app.credentials import load_credential
from app.collector.metrics_writer import write_metrics_batch, write_metric_history_batch

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 600  # look back 10 min for the latest datapoint


def _point_value(point):
    """Extract a single scalar float from a Cloud Monitoring Point, or None
    if it's a distribution/string value (not a single scalar -- skip)."""
    kind = point.value._pb.WhichOneof("value")
    if kind == "double_value":
        return point.value.double_value
    if kind == "int64_value":
        return float(point.value.int64_value)
    if kind == "bool_value":
        return 1.0 if point.value.bool_value else 0.0
    return None


# ── Resource resolvers ────────────────────────────────────────
# Each takes (project_id, resource_labels, resource_id_map, numeric_id_map)
# and returns a resource_db_id (int) or None if it can't match.
# resource_id_map:  {resources.resource_id string -> resources.id}, scoped
#                    to this account + this one GCP service.
# numeric_id_map:   {numeric instance ID string -> resources.id}, compute
#                    only -- built from resources.tags._gcp_numeric_id.

def _resolve_compute_instance(project_id, labels, resource_id_map, numeric_id_map):
    # Cloud Monitoring's gce_instance type: labels are project_id,
    # instance_id (NUMERIC), zone -- no name. Confirmed against Google's
    # monitored-resource-type reference. Match via the numeric ID
    # discovery.py now persists, not a reconstructed path string.
    numeric_id = labels.get("instance_id")
    if not numeric_id:
        return None
    return numeric_id_map.get(numeric_id)


def _resolve_gcs_bucket(project_id, labels, resource_id_map, numeric_id_map):
    # Cloud Storage's gcs_bucket type includes the bucket name directly.
    # NOT directly confirmed against a live response here (see this
    # script's "WHAT THIS DOES NOT DO" section) -- verify via the live
    # server's skip/error logs before trusting GCS alerts.
    bucket_name = labels.get("bucket_name")
    if not bucket_name:
        return None
    return resource_id_map.get(f"projects/{project_id}/buckets/{bucket_name}")


def _resolve_cloudsql_instance(project_id, labels, resource_id_map, numeric_id_map):
    # Cloud SQL's cloudsql_database type: database_id IS the Cloud SQL
    # instance name directly (confirmed against Google's monitored-
    # resource-type reference -- not a "project:instance" composite).
    instance_name = labels.get("database_id")
    if not instance_name:
        return None
    return resource_id_map.get(f"projects/{project_id}/instances/{instance_name}")


def _resolve_cloud_run_service(project_id, labels, resource_id_map, numeric_id_map):
    # Cloud Run's cloud_run_revision type: service_name + location give
    # exactly what discovery.py's stored resource_id path needs
    # (confirmed against Google's monitored-resource-type reference).
    location = labels.get("location")
    service_name = labels.get("service_name")
    if not (location and service_name):
        return None
    return resource_id_map.get(f"projects/{project_id}/locations/{location}/services/{service_name}")


_RESOLVERS = {
    "compute_instance": _resolve_compute_instance,
    "gcs_bucket": _resolve_gcs_bucket,
    "cloudsql_instance": _resolve_cloudsql_instance,
    "cloud_run_service": _resolve_cloud_run_service,
}


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


def _build_resource_maps(cur, account_id: int, services: set):
    """
    One-time-per-cycle-per-account lookup: for every service this account
    has enabled metrics for, {resources.resource_id: resources.id}, plus
    (compute_instance only) {numeric_id: resources.id} from tags.
    Services with no resolver are skipped -- no point querying resources
    for a service we can't match series back to anyway.
    """
    resource_id_maps = {}
    numeric_id_map = {}
    for service in services:
        if service not in _RESOLVERS:
            continue
        cur.execute("""
            SELECT id, resource_id, tags FROM resources
            WHERE aws_account_id = %s AND resource_type = %s
        """, (account_id, service))
        rows = cur.fetchall()
        resource_id_maps[service] = {r["resource_id"]: r["id"] for r in rows}
        if service == "compute_instance":
            for r in rows:
                try:
                    tags = json.loads(r["tags"]) if r["tags"] else {}
                except (TypeError, ValueError):
                    tags = {}
                nid = tags.get("_gcp_numeric_id")
                if nid:
                    numeric_id_map[nid] = r["id"]
    return resource_id_maps, numeric_id_map


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
        if not enabled:
            return result
        resource_id_maps, numeric_id_map = _build_resource_maps(
            cur, account["id"], {row["service"] for row in enabled}
        )
    finally:
        cur.close(); conn.close()

    for row in enabled:
        metric_type = f"{row['namespace']}/{row['metric_name']}"
        service = row["service"]
        result["metric_types_queried"] += 1

        resolver = _RESOLVERS.get(service)
        if resolver is None:
            result["errors"].append(
                f"{metric_type}: no resource resolver for GCP service '{service}' yet "
                f"(extended-tier gap, not a match failure) -- skipped"
            )
            continue

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

        # metrics_rows: (resource_db_id, metric_name, value) -> latest
        #   value only, upserted into `metrics` for alert_evaluator.py.
        # history_rows: (resource_db_id, metric_name, value, timestamp)
        #   -> every returned datapoint, appended into `metric_history`.
        # metric_name here is row["metric_name"] -- metric_catalog's exact
        # stored name, matching what alert_evaluator.py's join expects
        # (same convention Azure Phase 2 uses).
        metrics_rows = []
        history_rows = []
        unmatched = 0
        resource_id_map = resource_id_maps.get(service, {})
        try:
            for ts in time_series:
                if not ts.points:
                    continue
                resource_db_id = resolver(
                    project_id, dict(ts.resource.labels), resource_id_map, numeric_id_map
                )
                if resource_db_id is None:
                    unmatched += 1
                    continue

                # Cloud Monitoring returns points newest-first; write every
                # point to history, and points[0] (the newest) as the
                # latest value -- same "full history + latest cache" split
                # as AWS/Azure's direct-fetch collectors.
                for point in ts.points:
                    value = _point_value(point)
                    if value is None:
                        continue
                    # proto-plus exposes Timestamp fields as native
                    # datetime.datetime objects on read -- NOT tested
                    # against a live response here, verify on the server.
                    history_rows.append(
                        (resource_db_id, row["metric_name"], value, point.interval.end_time)
                    )
                latest_value = _point_value(ts.points[0])
                if latest_value is not None:
                    metrics_rows.append((resource_db_id, row["metric_name"], latest_value))
        except Exception as e:
            result["errors"].append(f"{metric_type}: error parsing results: {e}")
            continue

        if metrics_rows:
            write_metrics_batch(metrics_rows)
            result["pushed"] += len(metrics_rows)
        if history_rows:
            write_metric_history_batch(history_rows)
        if unmatched:
            result["errors"].append(
                f"{metric_type}: {unmatched} time series had no matching resource in "
                f"DB (resolver ran but found no match -- check discovery has run "
                f"recently, or that resources.tags._gcp_numeric_id is populated for "
                f"compute_instance rows discovered before this fix)"
            )

    return result


def collect_all_gcp_accounts() -> dict:
    """Runs collect_account_metrics() for every active GCP account. Used by the scheduler."""
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT * FROM aws_accounts
            WHERE status = 'active' AND provider = 'gcp'
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
                logger.warning(f"[gcp collector] account={account['id']} {e}")
    return totals
