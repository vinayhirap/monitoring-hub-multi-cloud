# app/providers/gcp/metrics_extended.py
"""
Resource resolvers for GCP's 12 extended-tier services (see Section 3
of the Sep 6-10 2026 handover: "GCP extended tier -- not started").

CONTEXT -- what already existed vs. what this file adds:
  app/providers/gcp/discovery.py already discovers real `resources`
  rows for all 12 extended services (dedicated describe/list calls,
  same as the 4 core services -- NOT the generic Asset Inventory
  fallback, which only covers services with no dedicated discoverer).
  app/providers/gcp/metrics_collector.py already fetches metric values
  for these fleet-wide via list_time_series() -- GCP's Cloud Monitoring
  API returns every resource of a given metric type in one call,
  unlike AWS's per-resource GetMetricData. The actual gap (identified
  mid-session by re-reading metrics_collector.py before writing any
  new code) was narrower than "GCP extended tier needs discovery +
  collection built from scratch": it was JUST resource resolution --
  matching each returned time series' Cloud Monitoring `resource.labels`
  back to a specific `resources.id` row, which only existed for the 4
  core services (see metrics_collector.py's _RESOLVERS). Every
  extended-tier time series was being fetched and then silently
  skipped ("no resource resolver for GCP service '<x>' yet") --
  exactly what this closes.

Resolvers below reconstruct the exact resource_id path string
discovery.py writes for that service, then look it up in the
resource_id_map built by metrics_collector.py._build_resource_maps
(same contract as the 4 core resolvers already there).

CONFIDENCE -- every monitored-resource type/label combination below
was checked against Google's published Cloud Monitoring
monitored-resource-type reference
(https://cloud.google.com/monitoring/api/resources), not guessed --
but NONE of it has been exercised against a live GCP project from this
environment (no credentials available in this sandbox). Same caveat as
app/collector/discovery/extended.py's AWS equivalent. Two bands:

  RESOLVED      -- single resource type, resource labels alone are
                   sufficient to reconstruct discovery.py's exact
                   resource_id: gke_cluster, cloudfunctions_function,
                   pubsub_topic, pubsub_subscription, cloud_lb,
                   redis_instance, spanner_instance,
                   firestore_database, nat_gateway. bigquery_project is
                   RESOLVED for its one dataset-scoped metric
                   (storage/stored_bytes) only -- see its own note.

  NOT RESOLVED (documented gap, not guessed at) --
    - gke_node:            Cloud Monitoring's k8s_node monitored
                            resource carries project_id/location/
                            cluster_name/node_name -- NOT node_pool.
                            discovery.py's gke_node rows are per-POOL
                            (matching the curated metric semantics,
                            which are pool-level), so there is no
                            label-only path from an individual node's
                            time series back to a specific pool row.
                            Doing this correctly needs a
                            node_name -> pool mapping (e.g. via each
                            node's "cloud.google.com/gke-nodepool"
                            Kubernetes label, fetched separately) --
                            out of scope for this pass. Left
                            unresolved rather than guessed via node
                            naming conventions (fragile and
                            unverified).
    - gce_persistent_disk: compute.googleapis.com/instance/disk/*
                            metrics are reported against the
                            **gce_instance** monitored resource (the
                            VM), with the specific disk identified by
                            a METRIC label (device_name), not a
                            resource label -- there is no
                            disk-as-monitored-resource in Cloud
                            Monitoring for this metric family. Matching
                            back to a specific gce_persistent_disk row
                            needs the instance's attached-disk-name
                            list (not currently captured by
                            discovery.py's _discover_persistent_disks,
                            which only lists disks, not instance
                            attachments) -- left unresolved rather than
                            attributing a device_name to the wrong disk
                            resource.
    - bigquery_project:    three of its four curated metrics
                            (query/count, query/execution_times,
                            slots/allocated_for_project) are
                            published against the **bigquery_project**
                            monitored resource (project_id only -- no
                            dataset_id label exists at all for these),
                            so they can never be attributed to one of
                            discovery.py's per-dataset resource rows --
                            this is a genuine mismatch between "how
                            BigQuery reports these metrics" and
                            "the closest thing to a monitorable
                            BigQuery resource" (this app's own
                            per-dataset modeling choice), not a missing
                            resolver. Only storage/stored_bytes
                            (bigquery_dataset resource, HAS dataset_id)
                            resolves. As of monitoring-hub-metric-audit.md
                            §8/§10, these three are skipped by name in
                            metrics_collector.py's collect_account_metrics()
                            BEFORE the list_time_series() call (see
                            _BIGQUERY_PROJECT_UNRESOLVABLE_METRICS there),
                            rather than queried and discarded after --
                            avoids paying for/counting series against
                            GCP's per-series-returned read pricing that
                            can never be used.
"""
import logging

logger = logging.getLogger(__name__)


def _resolve_gke_cluster(project_id, labels, resource_id_map, numeric_id_map):
    # k8s_container resource: project_id, location, cluster_name,
    # namespace_name, pod_name, container_name. discovery.py's
    # gke_cluster rows are per-cluster, so only location+cluster_name
    # matter here -- namespace/pod/container granularity is collapsed
    # up to the cluster (matches the curated metrics, which are
    # container-level values Cloud Monitoring itself doesn't
    # pre-aggregate; per-pod breakdown isn't modeled by this app).
    location = labels.get("location")
    cluster_name = labels.get("cluster_name")
    if not (location and cluster_name):
        return None
    return resource_id_map.get(f"projects/{project_id}/locations/{location}/clusters/{cluster_name}")


def _resolve_cloudfunctions_function(project_id, labels, resource_id_map, numeric_id_map):
    # cloud_function resource: project_id, region, function_name --
    # confirmed against Google's monitored-resource-type reference,
    # applies to both 1st- and 2nd-gen Cloud Functions.
    region = labels.get("region")
    function_name = labels.get("function_name")
    if not (region and function_name):
        return None
    return resource_id_map.get(f"projects/{project_id}/locations/{region}/functions/{function_name}")


def _resolve_pubsub_topic(project_id, labels, resource_id_map, numeric_id_map):
    # pubsub_topic resource: project_id, topic_id.
    topic_id = labels.get("topic_id")
    if not topic_id:
        return None
    return resource_id_map.get(f"projects/{project_id}/topics/{topic_id}")


def _resolve_pubsub_subscription(project_id, labels, resource_id_map, numeric_id_map):
    # pubsub_subscription resource: project_id, subscription_id.
    subscription_id = labels.get("subscription_id")
    if not subscription_id:
        return None
    return resource_id_map.get(f"projects/{project_id}/subscriptions/{subscription_id}")


def _resolve_cloud_lb(project_id, labels, resource_id_map, numeric_id_map):
    # Several LB-family monitored resources share a forwarding_rule_name
    # label (https_lb_rule for global HTTP(S) LBs; tcp_lb_rule/
    # udp_lb_rule/internal_* for regional network/internal LBs, which
    # additionally carry a `region` label). discovery.py stores global
    # forwarding rules under .../global/forwardingRules/{name} and
    # regional ones under .../regions/{region}/forwardingRules/{name} --
    # try whichever shape this series' labels indicate.
    rule_name = labels.get("forwarding_rule_name")
    if not rule_name:
        return None
    region = labels.get("region")
    if region:
        hit = resource_id_map.get(f"projects/{project_id}/regions/{region}/forwardingRules/{rule_name}")
        if hit:
            return hit
    return resource_id_map.get(f"projects/{project_id}/global/forwardingRules/{rule_name}")


def _resolve_redis_instance(project_id, labels, resource_id_map, numeric_id_map):
    # redis_instance resource: project_id, region, instance_id.
    region = labels.get("region")
    instance_id = labels.get("instance_id")
    if not (region and instance_id):
        return None
    return resource_id_map.get(f"projects/{project_id}/locations/{region}/instances/{instance_id}")


def _resolve_bigquery_project(project_id, labels, resource_id_map, numeric_id_map):
    # Only the bigquery_dataset resource (storage/stored_bytes) carries
    # a dataset_id label -- bigquery_project-scoped metrics
    # (query/count, query/execution_times, slots/allocated_for_project)
    # have no dataset_id at all and can never resolve to one of
    # discovery.py's per-dataset rows (see module docstring).
    dataset_id = labels.get("dataset_id")
    if not dataset_id:
        return None
    return resource_id_map.get(f"projects/{project_id}/datasets/{dataset_id}")


def _resolve_spanner_instance(project_id, labels, resource_id_map, numeric_id_map):
    # spanner_instance resource: project_id, instance_id.
    instance_id = labels.get("instance_id")
    if not instance_id:
        return None
    return resource_id_map.get(f"projects/{project_id}/instances/{instance_id}")


def _resolve_firestore_database(project_id, labels, resource_id_map, numeric_id_map):
    # firestore_instance resource: project_id, database (the database
    # ID, e.g. "(default)").
    database = labels.get("database")
    if not database:
        return None
    return resource_id_map.get(f"projects/{project_id}/databases/{database}")


def _resolve_nat_gateway(project_id, labels, resource_id_map, numeric_id_map):
    # nat_gateway resource: project_id, region, router_id, gateway_name.
    region = labels.get("region")
    router_id = labels.get("router_id")
    gateway_name = labels.get("gateway_name")
    if not (region and router_id and gateway_name):
        return None
    return resource_id_map.get(
        f"projects/{project_id}/regions/{region}/routers/{router_id}/nats/{gateway_name}"
    )


# service_key -> resolver. Merged into metrics_collector.py's _RESOLVERS.
# gke_node and gce_persistent_disk deliberately absent -- see module
# docstring for why (documented gap, not an oversight).
EXTENDED_RESOLVERS = {
    "gke_cluster":             _resolve_gke_cluster,
    "cloudfunctions_function": _resolve_cloudfunctions_function,
    "pubsub_topic":            _resolve_pubsub_topic,
    "pubsub_subscription":     _resolve_pubsub_subscription,
    "cloud_lb":                _resolve_cloud_lb,
    "redis_instance":          _resolve_redis_instance,
    "bigquery_project":        _resolve_bigquery_project,
    "spanner_instance":        _resolve_spanner_instance,
    "firestore_database":      _resolve_firestore_database,
    "nat_gateway":             _resolve_nat_gateway,
}
