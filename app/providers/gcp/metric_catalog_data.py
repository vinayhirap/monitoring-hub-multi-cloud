# app/providers/gcp/metric_catalog_data.py
"""
Curated GCP Cloud Monitoring metric catalog, same shape as
app.aws.metric_catalog_data.CURATED. `namespace` here is the Cloud
Monitoring metric-prefix (compute.googleapis.com/..., etc) — see
https://cloud.google.com/monitoring/api/metrics_gcp
"""

CURATED = {
    "compute_instance": ("Compute Engine", "compute.googleapis.com/instance", "core", [
        ("cpu/utilization",                  "Percent",  "Average", True,  "% CPU used"),
        ("network/received_bytes_count",     "Bytes",    "Total",   True,  "Inbound network traffic"),
        ("network/sent_bytes_count",         "Bytes",    "Total",   True,  "Outbound network traffic"),
        ("disk/read_bytes_count",            "Bytes",    "Total",   False, "Disk read throughput"),
        ("disk/write_bytes_count",           "Bytes",    "Total",   False, "Disk write throughput"),
        ("disk/read_ops_count",              "Count",    "Total",   False, "Disk read IOPS"),
        ("disk/write_ops_count",             "Count",    "Total",   False, "Disk write IOPS"),
        ("uptime",                           "Seconds",  "Total",   True,  "Instance uptime"),
    ]),
    "gcs_bucket": ("Cloud Storage", "storage.googleapis.com/storage", "core", [
        ("total_bytes",           "Bytes", "Average", True,  "Total bytes stored"),
        ("object_count",          "Count", "Average", True,  "Number of objects"),
        ("api/request_count",     "Count", "Total",   True,  "Total API requests"),
    ]),
    "cloudsql_instance": ("Cloud SQL", "cloudsql.googleapis.com/database", "core", [
        ("cpu/utilization",           "Percent", "Average", True,  "% CPU used"),
        ("memory/utilization",        "Percent", "Average", True,  "% memory used"),
        ("disk/utilization",          "Percent", "Average", True,  "% storage used"),
        ("network/connections",       "Count",   "Average", True,  "Active connections"),
        ("mysql/replication/seconds_behind_master", "Seconds", "Average", False, "Replica lag"),
    ]),
}

DIRECTORY = [
    ("Cloud Run",             "run.googleapis.com"),
    ("Cloud Functions",       "cloudfunctions.googleapis.com"),
    ("Google Kubernetes Engine", "kubernetes.io"),
    ("Cloud Load Balancing",  "loadbalancing.googleapis.com"),
    ("Pub/Sub",               "pubsub.googleapis.com"),
    ("BigQuery",              "bigquery.googleapis.com"),
]
