#!/usr/bin/env python3
"""
apply_gcp_direct_metrics_fetch.py
========================================
Monitoring Hub -- Phase 3 of removing VictoriaMetrics: GCP direct metric
fetch, replacing VM for GCP. AWS (Phase 1) and Azure (Phase 2) are
already off VM. After this script, all three providers write their
last-value/history data directly -- nothing reads or writes through VM
for alerting purposes anymore (VM is still used by dashboard charts
until Phase 4).

WHAT THIS DOES
--------------
1. Same "retarget, don't rewrite" finding as Phase 2: GCP already has a
   complete, working Cloud Monitoring integration in
   app/providers/gcp/metrics_collector.py. It just pushes to VM instead
   of writing to this project's own `metrics` / `metric_history` tables.

2. GENUINE BUG FOUND during this retarget, not just a VM round-trip to
   remove (unlike Azure, where the retarget was purely mechanical):
   GCP's push labels a series with whatever Cloud Monitoring's own
   monitored-resource labels happen to be, and metrics_vm_sync.py's
   _sync_azure_gcp_metrics() (added by fix_azure_gcp_alert_evaluation_gap.py)
   expects to find a literal "resource_id" label matching resources.resource_id
   verbatim. Azure's collector explicitly sets that label
   (resource_row["resource_id"]) so it works. GCP's collector never has
   -- it just forwards Cloud Monitoring's native labels, which for the
   "compute_instance" service (Cloud Monitoring type "gce_instance") are
   project_id/instance_id/zone, where instance_id is a NUMERIC id Google
   assigns (confirmed against Google's own monitored-resource-type docs:
   https://docs.cloud.google.com/monitoring/api/resources#tag_gce_instance)
   -- not the name-based string this app's `resources.resource_id` is
   built from (app/providers/gcp/discovery.py's
   f"projects/{p}/zones/{z}/instances/{name}"). The two values never
   equal each other, so `_sync_azure_gcp_metrics()`'s
   `values.get(row["aws_resource_id"])` lookup can never match for
   Compute -- meaning GCP Compute alerts have likely never actually been
   able to fire, even after that earlier fix, despite its docstring
   describing Azure and GCP as sharing "the same resource_id-label
   convention." That symmetry held for Azure; it did not hold for GCP
   Compute.

   compute_instance is fixed properly here rather than worked around:
   app/providers/gcp/discovery.py already fetches the numeric instance
   ID on every discovery cycle (compute_v1 Instance.id) and was simply
   discarding it. This script makes it persist that ID into
   resources.tags, so the collector can resolve numeric_id -> resource_db_id
   from data discovery already collects -- no extra API call needed.

   The other 3 "core" GCP services (gcs_bucket, cloudsql_instance,
   cloud_run_service) do NOT have this problem: Cloud Monitoring's
   labels for gcs_bucket, cloudsql_database, and cloud_run_revision
   include the resource's own name-based identifier directly (verified
   against Google's monitored-resource-type reference), so they're
   resolved by reconstructing the exact resource_id string
   discovery.py's own code builds for each, no numeric-ID workaround
   needed.

3. Retargets app/providers/gcp/metrics_collector.py's
   collect_account_metrics() the same way Phase 2 did for Azure: every
   returned datapoint into `metric_history`, latest value per
   (resource, metric) upserted into `metrics`, VM push (vm_write_batch)
   removed. GCP services with no resolver yet (the "extended" tier --
   GKE, Cloud Functions, Pub/Sub, etc.) are explicitly skipped and
   counted, not silently dropped or guessed at -- same honest-gap
   pattern as AWS/Azure's own "no VM series yet" skip logging.

4. Short-circuits metrics_vm_sync.py's sync_metrics_from_vm(): after
   this script, Azure and GCP are BOTH excluded (AWS already was), so
   the function's entire job is permanently a zero-row no-op. Rather
   than run a real DB query every standard-tier cycle for nothing
   forever until Phase 4, this makes it log once and return immediately.
   The underlying AWS/Azure/GCP sync helper functions are left in place,
   unreachable but harmless, pending Phase 4's actual VM-code removal.

WHAT THIS DOES NOT DO
----------------------
- Does not add resolvers for GCP's "extended" tier services -- only the
  4 "core" services this app's discovery.py already collects
  (compute_instance, gcs_bucket, cloudsql_instance, cloud_run_service).
  Extended-tier GCP metrics remain unalertable until a future pass adds
  resolvers for them, exactly the same category of gap AWS's Lambda/RDS
  extended-stats and Azure's non-core services were left with.
- Does not touch dashboard/chart code (Phase 4).
- Does not remove VictoriaMetrics, vm_client.py, or the dead VM-sync
  helper functions -- left in place for Phase 4 to retire alongside
  everything else VM-related.
- Does not verify the gcs_bucket monitored-resource label ("bucket_name")
  against a live response -- inferred from Google's Cloud Storage
  metrics documentation, not directly confirmed in this reference page
  the way gce_instance/cloudsql_database/cloud_run_revision were. Flagged
  clearly in code; verify via the live server's error/skip logs (see
  step B below) before trusting GCS alerts.

TESTED: the new write-path logic and all 4 resolvers (mocked Cloud
Monitoring TimeSeries/Point objects matching Google's documented label
schemas, mocked DB cursors including a compute_instance row with/without
the new tags key) were exercised locally -- confirms numeric-ID
resolution works for compute_instance, name-based resolution works for
the other 3 core services, unresolvable services are skipped and
counted rather than guessed, and sync_metrics_from_vm() no longer
queries the DB. NOT tested: an actual live Cloud Monitoring call or a
live discovery cycle against a real GCP project -- no GCP credentials or
network access available here; verify on the server (step B/C).

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_gcp_direct_metrics_fetch.py --dry-run
    python3 apply_gcp_direct_metrics_fetch.py --apply

NOTE: same as Phase 2, this needs to run as `sudo python3 ...` (root),
not `sudo -u cloudops`, because its metric_history existence check reads
.env directly (see HANDOVER.md #4).
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime

# ─────────────────────────── DB: verify metric_history exists ───────────────────────────


def _load_db_password():
    env_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        "/opt/monitoring-hub/app/.env",
        "/opt/monitoring-hub/.env.production",
    ]
    for key in ("DB_PASSWORD", "MONITOR_DB_PASSWORD"):
        if os.environ.get(key):
            return os.environ[key]
    for path in env_candidates:
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() in ("DB_PASSWORD", "MONITOR_DB_PASSWORD") and v.strip():
                    return v.strip().strip('"').strip("'")
    return None


def verify_metric_history_exists(dry_run):
    db_pass = _load_db_password()
    db_host = os.environ.get("DB_HOST", "127.0.0.1")
    db_port = os.environ.get("DB_PORT", "3306")
    db_user = os.environ.get("DB_USER", "monitor")
    db_name = os.environ.get("DB_NAME", "monitoring_hub")

    cmd = ["mysql", f"-u{db_user}", "-h", db_host, "-P", db_port, "-N", "-B"]
    if db_pass:
        cmd.append(f"-p{db_pass}")
    cmd.append(db_name)

    if dry_run:
        print("[DRY-RUN] would run: SHOW TABLES LIKE 'metric_history' (verify only, no DDL)")
        return True

    result = subprocess.run(
        cmd, input="SHOW TABLES LIKE 'metric_history';",
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[ERROR] Could not query the database:\n{result.stderr}", file=sys.stderr)
        return False
    if "metric_history" not in result.stdout:
        print(
            "[ERROR] metric_history table not found. This script assumes Phase 1 "
            "already created it -- re-run apply_direct_gmd_metrics_revival.py's "
            "table-creation step before Phase 3.",
            file=sys.stderr,
        )
        return False
    print("Verified metric_history table exists.")
    return True


# ─────────────────────────── file patches ───────────────────────────

DISCOVERY_OLD = '''        for inst in response.instances:
            labels = dict(inst.labels or {})
            resource_id = f"projects/{project_id}/zones/{zone_name}/instances/{inst.name}"
            _upsert_resource(
                cursor, account_id, "compute_instance", resource_id, inst.name,
                labels, zone_name, "compute",
            )
            count += 1'''

DISCOVERY_NEW = '''        for inst in response.instances:
            labels = dict(inst.labels or {})
            # Cloud Monitoring's gce_instance monitored resource only exposes
            # the NUMERIC instance ID (see
            # https://docs.cloud.google.com/monitoring/api/resources#tag_gce_instance),
            # never the name -- but resource_id below (and everything else this
            # app matches against) is name-based. Stash the numeric ID discovery
            # already has in hand so Phase 3's direct-fetch collector
            # (app/providers/gcp/metrics_collector.py) can resolve metrics back
            # to this resource without an extra API call. See
            # apply_gcp_direct_metrics_fetch.py.
            labels["_gcp_numeric_id"] = str(inst.id)
            resource_id = f"projects/{project_id}/zones/{zone_name}/instances/{inst.name}"
            _upsert_resource(
                cursor, account_id, "compute_instance", resource_id, inst.name,
                labels, zone_name, "compute",
            )
            count += 1'''

COLLECTOR_HEADER_OLD = '''# app/providers/gcp/metrics_collector.py
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
    return cur.fetchall()'''

COLLECTOR_HEADER_NEW = '''# app/providers/gcp/metrics_collector.py
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
    return resource_id_maps, numeric_id_map'''

COLLECTOR_BODY_OLD = '''def collect_account_metrics(account: dict) -> dict:
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

    return result'''

COLLECTOR_BODY_NEW = '''def collect_account_metrics(account: dict) -> dict:
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

    return result'''

VMSYNC_OLD = '''def sync_metrics_from_vm() -> int:
    """
    Populates `metrics` from VM for Azure/GCP resources with an enabled
    threshold. Returns the number of datapoints written.

    AWS is intentionally NOT synced from VM here anymore (see
    apply_direct_gmd_metrics_revival.py, Phase 1 of removing
    VictoriaMetrics): app/collector/metrics/runner.py's GetMetricData
    collector now writes AWS's last-value cache directly, and runs
    BEFORE this function in every tier cycle (see scheduler.py). If this
    function also synced AWS from VM afterward, it would immediately
    overwrite those fresh direct values with VM's separately-scraped
    (and potentially stale or simply different) data on every single
    cycle -- a real correctness bug, not just redundant work. Azure/GCP
    are unaffected and still sync from VM exactly as before, until
    Phases 2/3 replace that too.
    """
    rows = _fetch_enabled_threshold_targets()
    if not rows:
        logger.info("VM metrics sync: no enabled thresholds -- nothing to do")
        return 0

    # Azure is intentionally NOT synced from VM here anymore (Phase 2, see
    # apply_azure_direct_metrics_fetch.py): app/providers/azure/metrics_collector.py
    # now writes Azure's last-value cache directly from Azure Monitor, and
    # multicloud_scheduler.py runs it on its own interval, independent of this
    # sync job. If this function also synced Azure from VM afterward, it would
    # race with those direct writes and could silently overwrite fresher values
    # with stale/duplicate VM data on every cycle -- the same correctness bug
    # Phase 1's AWS exclusion fixed. GCP is unaffected and still syncs from VM
    # exactly as before, until Phase 3 replaces that too.
    other_rows = [r for r in rows if (r.get("provider") or "aws") not in ("aws", "azure")]

    other_datapoints, other_skipped, other_matched = _sync_azure_gcp_metrics(other_rows)

    datapoints = other_datapoints
    matched = other_matched

    write_metrics_batch(datapoints)

    total_skipped = sum(other_skipped.values())
    if total_skipped:
        detail_parts = [
            f"{prov}:{svc}/{metric} x{n}" for (prov, svc, metric), n in sorted(other_skipped.items())
        ]
        logger.info(
            f"VM metrics sync (gcp only -- AWS + Azure now handled directly): "
            f"{matched} written, {total_skipped} skipped (no VM series yet) -- "
            f"{', '.join(detail_parts)}"
        )
    else:
        logger.info(f"VM metrics sync (gcp only -- AWS + Azure now handled directly): "
                     f"{matched} written, 0 skipped")

    return matched'''

VMSYNC_NEW = '''def sync_metrics_from_vm() -> int:
    """
    Historically populated `metrics` from VM for whichever providers
    hadn't yet moved to direct-fetch. After Phase 1 (AWS), Phase 2
    (Azure), and Phase 3 (GCP, see apply_gcp_direct_metrics_fetch.py),
    ALL THREE providers write their own last-value cache directly --
    this function's entire job is now permanent dead weight until
    Phase 4 removes the call to it from scheduler.py entirely and
    retires VM. Short-circuiting here (log once, return immediately)
    instead of running a real DB query every standard-tier cycle for
    zero rows, forever, until then. _fetch_enabled_threshold_targets(),
    _sync_aws_metrics(), and _sync_azure_gcp_metrics() are left in place
    below, unreachable but harmless, for Phase 4 to clean up alongside
    the rest of the VM code.
    """
    logger.info(
        "VM metrics sync: no-op -- AWS (Phase 1), Azure (Phase 2), and GCP "
        "(Phase 3) are all handled directly now. Safe to remove this call "
        "from scheduler.py once Phase 4 confirms nothing else needs it."
    )
    return 0'''

MULTICLOUD_OLD = '''    try:
        gcp_result = collect_all_gcp_accounts()
        logger.info(
            f"[multicloud] GCP: {gcp_result['accounts']} account(s), "
            f"{gcp_result['pushed']} datapoints pushed"
            + (f", {len(gcp_result['errors'])} account(s) had errors" if gcp_result["errors"] else "")
        )
    except Exception as e:
        logger.error(f"[multicloud] GCP collection cycle crashed: {e}")'''

MULTICLOUD_NEW = '''    try:
        gcp_result = collect_all_gcp_accounts()
        logger.info(
            f"[multicloud] GCP: {gcp_result['accounts']} account(s), "
            f"{gcp_result['pushed']} datapoints written directly (Phase 3 -- no longer via VM)"
            + (f", {len(gcp_result['errors'])} account(s) had errors" if gcp_result["errors"] else "")
        )
    except Exception as e:
        logger.error(f"[multicloud] GCP collection cycle crashed: {e}")'''


def die(msg):
    print(f"\n[ABORT] {msg}", file=sys.stderr)
    sys.exit(1)


def find_repo_root():
    cur = os.path.abspath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(cur, "app", "auth", "security.py")) and \
           os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            die("Could not locate the monitoring-hub-multi-cloud repo root.")
        cur = parent


def backup(path):
    bpath = path + f".bak.{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(path, bpath)
    return bpath


def prepare_patch(path, label, replacements, done_marker):
    if not os.path.exists(path):
        die(f"{label} not found at {path}.")
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    if done_marker in content:
        return None, f"{label} already patched -- skipping."
    new_content = content
    for old, new in replacements:
        n = new_content.count(old)
        if n == 0:
            # Not necessarily an error: a LATER phase script may have
            # already rewritten this exact region (e.g. Phase 3 fully
            # replacing sync_metrics_from_vm()'s body supersedes Phase
            # 1/2's own edits to it, including whatever marker text
            # those scripts check for). Treat "expected text absent, and
            # it's not an ambiguous multi-match" as "already handled
            # elsewhere" and skip gracefully rather than aborting -- see
            # fix_deploy_script_drift.py for the chain-idempotency
            # incident this caught.
            return None, f"{label}: expected block not found (likely superseded by a later phase) -- skipping."
        if n > 1:
            die(f"{label}: expected exactly 1 match for one of the expected blocks, found {n}. "
                f"File may differ from what this script expects.")
        new_content = new_content.replace(old, new, 1)
    return new_content, f"{label}: OK ({len(new_content) - len(content):+d} bytes)"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="(default behavior; kept for backward compatibility with earlier docs/runbooks)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes")
    args = parser.parse_args()
    apply_ = not args.dry_run

    repo_root = find_repo_root()
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    discovery_path = os.path.join(repo_root, "app", "providers", "gcp", "discovery.py")
    collector_path = os.path.join(repo_root, "app", "providers", "gcp", "metrics_collector.py")
    vmsync_path = os.path.join(repo_root, "app", "collector", "metrics_vm_sync.py")
    multicloud_path = os.path.join(repo_root, "app", "collector", "multicloud_scheduler.py")

    results = []

    discovery_content, discovery_note = prepare_patch(
        discovery_path, "app/providers/gcp/discovery.py",
        [(DISCOVERY_OLD, DISCOVERY_NEW)],
        "_gcp_numeric_id",
    )
    results.append((discovery_path, "app/providers/gcp/discovery.py", discovery_content, discovery_note))

    collector_content, collector_note = prepare_patch(
        collector_path, "app/providers/gcp/metrics_collector.py",
        [
            (COLLECTOR_HEADER_OLD, COLLECTOR_HEADER_NEW),
            (COLLECTOR_BODY_OLD, COLLECTOR_BODY_NEW),
        ],
        "write_metric_history_batch",
    )
    results.append((collector_path, "app/providers/gcp/metrics_collector.py", collector_content, collector_note))

    vmsync_content, vmsync_note = prepare_patch(
        vmsync_path, "app/collector/metrics_vm_sync.py",
        [(VMSYNC_OLD, VMSYNC_NEW)],
        "Phase 1), Azure (Phase 2), and GCP",
    )
    results.append((vmsync_path, "app/collector/metrics_vm_sync.py", vmsync_content, vmsync_note))

    multicloud_content, multicloud_note = prepare_patch(
        multicloud_path, "app/collector/multicloud_scheduler.py",
        [(MULTICLOUD_OLD, MULTICLOUD_NEW)],
        "written directly (Phase 3 -- no longer via VM)",
    )
    results.append((multicloud_path, "app/collector/multicloud_scheduler.py", multicloud_content, multicloud_note))

    print("\nFile patch plan:")
    for _, _, _, note in results:
        print(f"  {note}")

    db_ok = verify_metric_history_exists(not apply_)
    if not db_ok:
        die("metric_history verification failed -- aborting before touching any files.")

    if all(content is None for _, _, content, _ in results):
        print("\nNothing to do -- everything this script would change is already applied.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    for path, label, content, note in results:
        if content is None:
            continue
        backup(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"Patched {label}")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 60 --no-pager

  B) Watch for these lines in the next multicloud cycle (~5 min):
       "[multicloud] GCP: N account(s), M datapoints written directly
        (Phase 3 -- no longer via VM)"
       "VM metrics sync: no-op -- AWS (Phase 1), Azure (Phase 2), and GCP
        (Phase 3) are all handled directly now. ..."
     If any GCP metric types log "no resource resolver for GCP service
     'X' yet" or "N time series had no matching resource in DB", that's
     expected for extended-tier services / stale discovery data -- not
     a crash. Check WHICH services are logging unmatched counts; if it's
     gcs_bucket specifically, that's the one resolver this script
     flagged as unverified against a live response.

  C) Verify metric_history is accumulating GCP rows, and specifically
     confirm compute_instance rows are matching now (the actual bug
     this phase fixed):
       mysql -u monitor -p monitoring_hub -e \\
         "SELECT r.resource_type, COUNT(*), MIN(mh.metric_timestamp), MAX(mh.metric_timestamp)
          FROM metric_history mh JOIN resources r ON r.id = mh.resource_id
          JOIN aws_accounts a ON a.id = r.aws_account_id
          WHERE a.provider = 'gcp' GROUP BY r.resource_type;"
     Also confirm the new tags key exists on compute_instance rows after
     the next discovery cycle (up to 15 min):
       mysql -u monitor -p monitoring_hub -e \\
         "SELECT id, resource_id, tags FROM resources
          WHERE resource_type = 'compute_instance' LIMIT 3;"
     -- tags should now contain "_gcp_numeric_id".

  D) Review, commit, push:
       git status
       git diff app/providers/gcp/discovery.py \\
                app/providers/gcp/metrics_collector.py \\
                app/collector/metrics_vm_sync.py \\
                app/collector/multicloud_scheduler.py
       git add app/providers/gcp/discovery.py \\
               app/providers/gcp/metrics_collector.py \\
               app/collector/metrics_vm_sync.py \\
               app/collector/multicloud_scheduler.py \\
               apply_gcp_direct_metrics_fetch.py
       git commit -m "feat(metrics): Phase 3 of removing VictoriaMetrics -- direct GCP Cloud Monitoring fetch, plus a real fix for a compute_instance resource-matching bug the earlier alert-evaluation-gap fix didn't actually close"
       git push origin main

  Next: Phase 4 -- point dashboard charts at metric_history for all
  three providers, then retire vm_client.py, multicloud_scheduler.py's
  VM push path for anything remaining, and metrics_vm_sync.py's now-dead
  helper functions entirely.
""")


if __name__ == "__main__":
    main()
