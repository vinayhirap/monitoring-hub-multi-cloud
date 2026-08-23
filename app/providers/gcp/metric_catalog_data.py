# app/providers/gcp/metric_catalog_data.py
"""
Curated GCP Cloud Monitoring metric catalog, same shape as
app.aws.metric_catalog_data.CURATED. `namespace` here is the Cloud
Monitoring metric-prefix (compute.googleapis.com/..., etc) — see
https://cloud.google.com/monitoring/api/metrics_gcp

Two tiers, mirroring the AWS file:

  CURATED   — services with a hand-enumerated metric list (name, unit,
              statistic, whether it's part of the recommended onboarding
              default, short description). 'core' = already collected by
              this app's resource collectors (Compute Engine, Cloud
              Storage, Cloud SQL, Cloud Run); 'extended' = the next tier
              of commonly-monitored GCP services.

  DIRECTORY — remaining GCP services that publish Cloud Monitoring
              metrics, registered by metric-prefix only. Individual
              metric types are fetched on demand per project via the
              Cloud Monitoring ListMetricDescriptors API ("Discover" in
              the UI) rather than hand-typed here.

Run `python scripts/seed_multicloud_metric_catalog.py` after editing
this file to push changes into the `metric_catalog` table.
"""

# service key -> (display name, metric-prefix, category, [ (metric_type_suffix, unit, statistic, is_default, description), ... ])
CURATED = {

    # ── Core (already collected today) ───────────────────────────
    "compute_instance": ("Compute Engine", "compute.googleapis.com/instance", "core", [
        ("cpu/utilization",              "Percent",      "Average", True,  "% CPU used"),
        ("cpu/usage_time",                "Seconds",      "Total",   False, "CPU seconds consumed"),
        ("network/received_bytes_count",  "Bytes",        "Total",   True,  "Inbound network traffic"),
        ("network/sent_bytes_count",      "Bytes",        "Total",   True,  "Outbound network traffic"),
        ("disk/read_bytes_count",         "Bytes",        "Total",   False, "Disk read throughput"),
        ("disk/write_bytes_count",        "Bytes",        "Total",   False, "Disk write throughput"),
        ("disk/read_ops_count",           "Count",        "Total",   False, "Disk read IOPS"),
        ("disk/write_ops_count",          "Count",        "Total",   False, "Disk write IOPS"),
        ("uptime",                        "Seconds",      "Total",   True,  "Instance uptime"),
        ("uptime_total",                  "Seconds",      "Total",   False, "Cumulative uptime since creation"),
    ]),
    "gcs_bucket": ("Cloud Storage", "storage.googleapis.com/storage", "core", [
        ("total_bytes",             "Bytes", "Average", True,  "Total bytes stored"),
        ("object_count",            "Count", "Average", True,  "Number of objects"),
        ("api/request_count",       "Count", "Total",   True,  "Total API requests"),
        ("network/sent_bytes_count", "Bytes", "Total",  False, "Egress traffic served"),
        ("total_byte_seconds",      "Bytes", "Average", False, "Byte-hours of storage (billing input)"),
    ]),
    "cloudsql_instance": ("Cloud SQL", "cloudsql.googleapis.com/database", "core", [
        ("cpu/utilization",           "Percent", "Average", True,  "% CPU used"),
        ("memory/utilization",        "Percent", "Average", True,  "% memory used"),
        ("disk/utilization",          "Percent", "Average", True,  "% storage used"),
        ("disk/read_ops_count",       "Count",   "Total",   False, "Disk read IOPS"),
        ("disk/write_ops_count",      "Count",   "Total",   False, "Disk write IOPS"),
        ("network/connections",       "Count",   "Average", True,  "Active connections"),
        ("mysql/replication/seconds_behind_master", "Seconds", "Average", False, "Replica lag (MySQL)"),
        ("postgresql/replication/replica_byte_lag", "Bytes",   "Average", False, "Replica lag (PostgreSQL)"),
        ("up",                        "Count",   "Average", True,  "Instance up/down health signal"),
    ]),
    "cloud_run_service": ("Cloud Run", "run.googleapis.com/container", "core", [
        ("cpu/utilizations",           "Percent",      "Average", True,  "% CPU used (per container instance)"),
        ("memory/utilizations",        "Percent",      "Average", True,  "% memory used (per container instance)"),
        ("request_count",              "Count",        "Total",   True,  "Total requests served"),
        ("request_latencies",          "MilliSeconds", "Average", True,  "Request latency"),
        ("instance_count",             "Count",        "Average", True,  "Active container instances"),
        ("billable_instance_time",     "Seconds",      "Total",   False, "Billable instance-time"),
        ("startup_latencies",          "MilliSeconds", "Average", False, "Cold-start latency"),
    ]),

    # ── Extended (common GCP services) ────────────────────────────
    "gke_cluster": ("Google Kubernetes Engine", "kubernetes.io/container", "extended", [
        ("cpu/core_usage_time",              "Seconds", "Total",   True,  "CPU core-seconds consumed per container"),
        ("cpu/limit_utilization",            "Percent", "Average", True,  "% of CPU limit used"),
        ("cpu/request_utilization",          "Percent", "Average", False, "% of CPU request used"),
        ("memory/used_bytes",                "Bytes",   "Average", True,  "Memory used per container"),
        ("memory/limit_utilization",         "Percent", "Average", True,  "% of memory limit used"),
        ("restart_count",                    "Count",   "Total",   True,  "Container restart count"),
        ("uptime",                           "Seconds", "Total",   False, "Container uptime"),
    ]),
    "gke_node": ("GKE Nodes", "kubernetes.io/node", "extended", [
        ("cpu/allocatable_utilization", "Percent", "Average", True,  "% of allocatable CPU used"),
        ("cpu/core_usage_time",         "Seconds", "Total",   False, "CPU core-seconds consumed per node"),
        ("memory/allocatable_utilization", "Percent", "Average", True, "% of allocatable memory used"),
        ("ephemeral_storage/used_bytes", "Bytes",  "Average", False, "Ephemeral storage used"),
    ]),
    "cloudfunctions_function": ("Cloud Functions", "cloudfunctions.googleapis.com/function", "extended", [
        ("execution_count",       "Count",        "Total",   True,  "Function invocations"),
        ("execution_times",       "MilliSeconds", "Average", True,  "Function execution duration"),
        ("active_instances",      "Count",        "Average", False, "Concurrently active instances"),
        ("user_memory_bytes",     "Bytes",        "Average", False, "Memory used by the function"),
        ("network_egress",        "Bytes",        "Total",   False, "Network egress from the function"),
    ]),
    "pubsub_topic": ("Pub/Sub", "pubsub.googleapis.com/topic", "extended", [
        ("send_message_operation_count", "Count", "Total",   True,  "Publish operations"),
        ("message_sizes",                "Bytes", "Average", False, "Published message size"),
        ("byte_cost",                    "Bytes", "Total",   False, "Billable message bytes"),
    ]),
    "pubsub_subscription": ("Pub/Sub Subscriptions", "pubsub.googleapis.com/subscription", "extended", [
        ("num_undelivered_messages",     "Count",   "Average", True,  "Backlog — undelivered messages"),
        ("oldest_unacked_message_age",   "Seconds", "Average", True,  "Age of oldest unacked message"),
        ("ack_message_count",            "Count",   "Total",   True,  "Messages acknowledged"),
        ("pull_ack_message_operation_count", "Count", "Total", False, "Pull-ack operations"),
        ("unacked_bytes_by_region",      "Bytes",   "Average", False, "Backlog bytes"),
    ]),
    "cloud_lb": ("Cloud Load Balancing", "loadbalancing.googleapis.com/https", "extended", [
        ("request_count",              "Count",        "Total",   True,  "Requests handled by the load balancer"),
        ("total_latencies",            "MilliSeconds", "Average", True,  "End-to-end request latency"),
        ("backend_latencies",          "MilliSeconds", "Average", True,  "Backend response latency"),
        ("backend_request_count",      "Count",        "Total",   False, "Requests forwarded to backends"),
    ]),
    "redis_instance": ("Memorystore for Redis", "redis.googleapis.com", "extended", [
        ("stats/memory/usage_ratio",   "Percent", "Average", True,  "% of memory used"),
        ("stats/cpu_utilization",      "Percent", "Average", True,  "% CPU used"),
        ("clients/connected",          "Count",   "Average", True,  "Connected clients"),
        ("stats/cache_hit_ratio",      "Percent", "Average", False, "Cache hit ratio"),
        ("keyspace/avg_ttl",           "Seconds", "Average", False, "Average key TTL"),
        ("replication/role",           "Count",   "Average", False, "Primary/replica role signal"),
    ]),
    "bigquery_project": ("BigQuery", "bigquery.googleapis.com", "extended", [
        ("query/count",                     "Count",        "Total",   True,  "Queries executed"),
        ("query/execution_times",           "MilliSeconds", "Average", True,  "Query execution duration"),
        ("storage/stored_bytes",            "Bytes",        "Average", True,  "Bytes stored across datasets"),
        ("slots/allocated_for_project",     "Count",        "Average", False, "Slots allocated (reservations)"),
    ]),
    "spanner_instance": ("Cloud Spanner", "spanner.googleapis.com", "extended", [
        ("api/request_count",          "Count",        "Total",   True,  "API requests"),
        ("api/request_latencies",      "MilliSeconds", "Average", True,  "API request latency"),
        ("instance/cpu/utilization",   "Percent",      "Average", True,  "% CPU used"),
        ("instance/storage/used_bytes", "Bytes",       "Average", False, "Storage used"),
    ]),
    "firestore_database": ("Firestore", "firestore.googleapis.com", "extended", [
        ("document/read_count",     "Count", "Total", True,  "Document reads"),
        ("document/write_count",    "Count", "Total", True,  "Document writes"),
        ("document/delete_count",   "Count", "Total", False, "Document deletes"),
        ("network/active_connections", "Count", "Average", False, "Active connections"),
    ]),
    "nat_gateway": ("Cloud NAT", "router.googleapis.com/nat", "extended", [
        ("sent_bytes_count",         "Bytes", "Total",   True,  "Bytes sent through NAT"),
        ("received_bytes_count",     "Bytes", "Total",   True,  "Bytes received through NAT"),
        ("nat_allocation_failed",    "Count", "Total",   True,  "NAT port allocation failures"),
        ("port_usage",               "Count", "Average", False, "Allocated NAT ports in use"),
        ("dropped_sent_packets_count", "Count", "Total", False, "Packets dropped on send"),
    ]),
    "gce_persistent_disk": ("Persistent Disk", "compute.googleapis.com/instance/disk", "extended", [
        ("read_bytes_count",   "Bytes", "Total", True,  "Disk read throughput"),
        ("write_bytes_count",  "Bytes", "Total", True,  "Disk write throughput"),
        ("read_ops_count",     "Count", "Total", False, "Read IOPS"),
        ("write_ops_count",    "Count", "Total", False, "Write IOPS"),
    ]),
}

DIRECTORY = [
    ("Google Kubernetes Engine (control plane)", "kubernetes.io/anthos"),
    ("Cloud CDN",              "loadbalancing.googleapis.com/https/backend_request_bytes_count"),
    ("Cloud DNS",              "dns.googleapis.com"),
    ("Cloud Interconnect",     "interconnect.googleapis.com"),
    ("Vertex AI",              "aiplatform.googleapis.com"),
    ("Cloud Tasks",            "cloudtasks.googleapis.com"),
    ("Cloud Composer",         "composer.googleapis.com"),
    ("Filestore",              "file.googleapis.com"),
    ("Dataflow",               "dataflow.googleapis.com"),
    ("Dataproc",               "dataproc.googleapis.com"),
    ("App Engine",             "appengine.googleapis.com"),
    ("Cloud Armor",            "networksecurity.googleapis.com"),
    ("Artifact Registry",      "artifactregistry.googleapis.com"),
    ("Cloud KMS",              "cloudkms.googleapis.com"),
    ("VPC Service Controls",   "vpcaccess.googleapis.com"),
    ("Cloud Trace",            "trace.googleapis.com"),
    ("Secret Manager",         "secretmanager.googleapis.com"),
    ("Cloud Scheduler",        "cloudscheduler.googleapis.com"),
    ("Cloud Bigtable",         "bigtable.googleapis.com"),
    ("Cloud Monitoring uptime checks", "monitoring.googleapis.com/uptime_check"),
]
