#!/usr/bin/env python3
r"""
apply_multicloud_step4_7_collectors.py

Steps 4-7 of the V5 multi-cloud plan: actual metric COLLECTION for
Azure/GCP accounts. Until this script, nothing pushed Azure/GCP metric
values into VictoriaMetrics at all -- the catalog and selector work from
apply_multicloud_step3_4.py let you pick metrics, but there was no
collector to actually go get their values. AWS doesn't need an
equivalent here: YACE (a separate Prometheus-exporter binary) scrapes
CloudWatch and pushes to VictoriaMetrics on its own.

What this adds:

  1. db/migrations/011_widen_resource_id.sql -- REAL BUG FIX, found while
     testing this script against a live DB. resources.resource_id was
     VARCHAR(100). Azure ARM resource IDs
     (/subscriptions/{guid}/resourceGroups/.../providers/Microsoft.Compute/
     virtualMachines/{name}) run 140-160+ characters. Under MySQL's default
     strict mode this means Azure resource-discovery INSERTs for
     VM/Storage/SQL/App Service resources have likely been failing outright
     with 'Data too long for column resource_id', not just silently
     truncating. Verified: a 143-char real-shaped ARM ID inserts cleanly
     after this migration, and fails before it.

  2. app/clients/vm_client.py -- adds vm_write_batch(), using
     VictoriaMetrics' /api/v1/import JSON-lines endpoint. There was no
     write path into VM at all before this (only read helpers existed).

  3. app/providers/azure/metrics_collector.py (new) -- real Azure Monitor
     collector via azure-monitor-query's MetricsClient.query_resources(),
     which batches up to 50 resources per call for one metric namespace.
     Cost note: Azure Monitor platform-metric reads are NOT billed per-call
     (unlike AWS CloudWatch GetMetricData) -- the aggressive GMD-avoidance
     work from V4 doesn't need replicating here.

  4. app/providers/gcp/metrics_collector.py (new) -- real GCP Cloud
     Monitoring collector via list_time_series(), which is fleet-wide per
     metric type (1 call = every resource of that type in the project, not
     1 call per resource). Same free-reads cost note as Azure.

  5. app/collector/multicloud_scheduler.py (new) + a 3rd background thread
     in app/main.py, alongside the existing AWS collector and describe-poll
     threads. Single flat 5-min interval, not AWS's tiered critical/
     standard/low split -- that tiering exists specifically to cut
     CloudWatch call volume for cost; Azure/GCP reads being free removes
     the reason to tier them apart.

  6. requirements.txt -- adds azure-monitor-query. IMPORTANT: pinned to
     >=1.2,<2 specifically -- version 2.0+ removed MetricsClient entirely
     into a separate package. Verified against the actual installed 1.4.1.

How this was verified (no live Azure/GCP account was available, so this
is as far as verification can go without one):
  - Every file py_compiles.
  - Installed a real local MariaDB, applied your actual schema.sql +
    migrations 003/009/010/011, ran your actual
    scripts/seed_multicloud_metric_catalog.py against it (102 Azure + 86
    GCP metrics seeded correctly).
  - Ran collect_account_metrics() for both providers for real, against that
    real DB, with ONLY the cloud SDK network calls mocked (Azure
    MetricsClient / GCP MetricServiceClient) -- every DB query, the
    metric-grouping logic, label building, and the final VM write payload
    are exercised for real. Confirmed correct metric-name slugging,
    correct latest-value selection, and confirmed the full 143-char ARM ID
    survives the round trip (proving migration 011 is actually needed).
  - Confirmed against a real mock HTTP server that vm_write_batch() sends
    the exact JSON-lines format VictoriaMetrics' /api/v1/import expects.
  - Confirmed error resilience: a missing credential and a simulated SDK
    exception (401) both degrade to a logged error, never raise, never
    crash the loop -- and a broken account doesn't stop a working one from
    being processed in the same collection cycle.

NOT verified (no live cloud credentials in the environment this was built
in): the ACTUAL shape of a real Azure Monitor / GCP Cloud Monitoring API
response. The mocks were built to match the SDKs' own documented/installed
data-model classes as closely as possible, but a live account is the only
way to be fully sure. Recommend testing against one real account of each
provider before trusting this for alerting.

SEPARATE, NOT AUTO-FIXED: found a second real bug while testing --
db/migrations/010_provider_credentials_table.sql declares
provider_credentials.aws_account_id as INT, but aws_accounts.id is BIGINT.
That's a foreign-key type mismatch (MySQL errno 150) if that migration is
ever applied fresh. Flagging rather than silently patching a file this
script wasn't asked to touch -- worth checking whether your live DB's
actual column type already diverges from what's committed (consistent with
the schema-drift pattern already seen elsewhere in this repo).

Usage:
    python apply_multicloud_step4_7_collectors.py --dry-run
    python apply_multicloud_step4_7_collectors.py

Run from the repo root (D:\Project\monitoring-tool\monitoring-hub-V5-multi-cloud).
After applying: run the DB migration manually if the script can't reach
your DB from this machine, then pip install -r requirements.txt and
restart the backend so the new collector thread starts.
"""

import argparse
import py_compile
import shutil
import sys
from pathlib import Path

AZURE_COLLECTOR = '# app/providers/azure/metrics_collector.py\n"""\nPulls metric VALUES for an Azure account\'s enabled metric selection and\npushes them into VictoriaMetrics.\n\nWhy this exists: AWS\'s tiered pipeline is YACE (a standalone Prometheus\nexporter binary) scraping CloudWatch and pushing to VM on its own -- no\nPython collector loop is involved for AWS metric *values* anymore (see\napp/collector/scheduler.py\'s comment: "GMD collection skipped -- VM/YACE\nmigration in progress"). There is no YACE-equivalent for Azure, so\nsomething has to actively pull Azure Monitor and push to VM. This is that\nsomething.\n\nCost note (different from AWS): Azure Monitor\'s platform-metric READ API\n(what MetricsClient.query_resources calls) is NOT billed per-call the way\nAWS CloudWatch GetMetricData is -- platform metrics are included at no\nextra charge. The aggressive GMD-avoidance work done in V4 doesn\'t apply\nhere the same way; polling on a short interval isn\'t a cost problem for\nAzure the way it was for AWS. (Custom/non-platform Azure metrics and very\nhigh query volume can still incur charges -- this collector only touches\nplatform metrics from CURATED, which are free reads.)\n\nBatching: MetricsClient.query_resources() accepts up to 50 resource IDs\nper call for one metric_namespace + a list of metric_names in a single\nrequest -- so one Azure account with, say, 30 VMs and 6 enabled VM\nmetrics costs exactly 1 API call per collection cycle for that service,\nnot 30 or 180.\n"""\nimport logging\nimport re\nfrom datetime import timedelta\n\nfrom app.db import get_connection\nfrom app.credentials import load_credential\nfrom app.clients.vm_client import vm_write_batch\n\nlogger = logging.getLogger(__name__)\n\n_BATCH_SIZE = 50  # Azure Monitor Metrics Batch API hard limit per call\n\n\ndef _slug(name: str) -> str:\n    """\'Percentage CPU\' -> \'percentage_cpu\' for the VM metric name suffix."""\n    s = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()\n    return s or "value"\n\n\ndef _enabled_azure_metrics(cur, account_id: int):\n    """{(namespace, service): {metric_name, ...}} for this account\'s enabled selection."""\n    cur.execute("""\n        SELECT mc.namespace, mc.service, mc.metric_name\n        FROM metric_catalog mc\n        JOIN account_metric_selections ams ON ams.metric_id = mc.id\n        WHERE ams.aws_account_id = %s AND ams.enabled = 1\n              AND mc.provider = \'azure\' AND mc.metric_name IS NOT NULL AND mc.metric_name != \'\'\n    """, (account_id,))\n    grouped = {}\n    for row in cur.fetchall():\n        key = (row["namespace"], row["service"])\n        grouped.setdefault(key, set()).add(row["metric_name"])\n    return grouped\n\n\ndef collect_account_metrics(account: dict) -> dict:\n    """\n    account: a row from aws_accounts (dict) for one Azure account. Must have\n    id, tenant_id, client_id, subscription_id, default_region.\n\n    Returns {"pushed": int, "resources_queried": int, "errors": [str, ...]}.\n    Never raises -- collection failures for one account/service shouldn\'t\n    crash the scheduler loop; they\'re reported back for logging instead.\n    """\n    result = {"pushed": 0, "resources_queried": 0, "errors": []}\n\n    tenant_id = (account.get("tenant_id") or "").strip()\n    client_id = (account.get("client_id") or "").strip()\n    subscription_id = (account.get("subscription_id") or "").strip()\n    region = (account.get("default_region") or "").strip()\n    secret = load_credential(account["id"])\n\n    if not (tenant_id and client_id and subscription_id and secret and region):\n        result["errors"].append("missing tenant_id/client_id/subscription_id/region/credential")\n        return result\n\n    try:\n        from azure.identity import ClientSecretCredential\n        from azure.monitor.query import MetricsClient, MetricAggregationType\n    except ImportError:\n        result["errors"].append("azure-monitor-query not installed (pip install -r requirements.txt)")\n        return result\n\n    try:\n        cred = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=secret)\n        # Azure Monitor\'s Metrics data-plane endpoint is regional, matching\n        # the account\'s own default_region (Azure region short-name, e.g.\n        # "centralindia" -- NOT an AWS-style region code).\n        endpoint = f"https://{region}.metrics.monitor.azure.com"\n        client = MetricsClient(endpoint, cred)\n    except Exception as e:\n        result["errors"].append(f"auth/client setup failed: {e}")\n        return result\n\n    conn = get_connection(); cur = conn.cursor(dictionary=True)\n    try:\n        by_service = _enabled_azure_metrics(cur, account["id"])\n        if not by_service:\n            return result\n\n        for (namespace, service), metric_names in by_service.items():\n            cur.execute("""\n                SELECT id, resource_id, name FROM resources\n                WHERE aws_account_id = %s AND resource_type = %s\n            """, (account["id"], service))\n            resources = cur.fetchall()\n            if not resources:\n                continue\n            result["resources_queried"] += len(resources)\n\n            for start in range(0, len(resources), _BATCH_SIZE):\n                chunk = resources[start:start + _BATCH_SIZE]\n                chunk_uris = [r["resource_id"] for r in chunk]\n                try:\n                    query_results = client.query_resources(\n                        resource_ids=chunk_uris,\n                        metric_namespace=namespace,\n                        metric_names=list(metric_names),\n                        timespan=timedelta(minutes=10),\n                        granularity=timedelta(minutes=1),\n                        aggregations=[MetricAggregationType.AVERAGE],\n                    )\n                except Exception as e:\n                    result["errors"].append(f"{service} ({namespace}): {e}")\n                    continue\n\n                # MetricsClient.query_resources returns results in the same\n                # order as resource_ids -- there\'s no resource_id field on\n                # the result object itself (verified against the SDK\'s\n                # MetricsQueryResult dataclass), so map back positionally.\n                series = []\n                for resource_row, query_result in zip(chunk, query_results):\n                    for metric in query_result.metrics:\n                        for ts_elem in metric.timeseries:\n                            if not ts_elem.data:\n                                continue\n                            latest = ts_elem.data[-1]  # most recent datapoint in the window\n                            value = latest.average\n                            if value is None:\n                                continue\n                            series.append({\n                                "metric": f"azure_{service}_{_slug(metric.name)}",\n                                "labels": {\n                                    "account_id": str(account["id"]),\n                                    "resource_id": resource_row["resource_id"],\n                                    "resource_name": resource_row["name"] or "",\n                                    "region": region,\n                                },\n                                "value": float(value),\n                            })\n                if series:\n                    if vm_write_batch(series):\n                        result["pushed"] += len(series)\n                    else:\n                        result["errors"].append(f"{service}: VM write failed for {len(series)} points")\n    finally:\n        cur.close(); conn.close()\n\n    return result\n\n\ndef collect_all_azure_accounts() -> dict:\n    """Runs collect_account_metrics() for every active Azure account. Used by the scheduler."""\n    conn = get_connection(); cur = conn.cursor(dictionary=True)\n    cur.execute("""\n        SELECT * FROM aws_accounts\n        WHERE status = \'active\' AND provider = \'azure\'\n    """)\n    accounts = cur.fetchall()\n    cur.close(); conn.close()\n\n    totals = {"accounts": len(accounts), "pushed": 0, "errors": []}\n    for account in accounts:\n        r = collect_account_metrics(account)\n        totals["pushed"] += r["pushed"]\n        if r["errors"]:\n            totals["errors"].append({"account_id": account["id"], "errors": r["errors"]})\n            for e in r["errors"]:\n                logger.warning(f"[azure collector] account={account[\'id\']} {e}")\n    return totals\n'

GCP_COLLECTOR = '# app/providers/gcp/metrics_collector.py\n"""\nPulls metric VALUES for a GCP account\'s enabled metric selection and\npushes them into VictoriaMetrics. Same rationale as the Azure collector\nin this package\'s sibling module -- no YACE-equivalent exists for GCP,\nso this actively pulls Cloud Monitoring and pushes to VM.\n\nCost note (different from AWS, same as Azure): Cloud Monitoring\'s\nListTimeSeries read API for GCP-provided ("system") metrics is free --\nthere is no CloudWatch-GetMetricData-style per-call billing to avoid\nhere. The V4 cost-avoidance patterns (describe_* polling instead of GMD,\ntiered intervals to cut call volume) were specifically about AWS\'s\nbilling model; they don\'t need to be replicated for GCP reads.\n\nEfficiency: unlike Azure (batched by resource, capped at 50/call) or AWS\nCloudWatch (one call per metric per resource without YACE), GCP\'s\nlist_time_series is naturally fleet-wide -- one filter=\'metric.type="..."\'\ncall returns that metric for EVERY resource of that type in the project\nin a single response. So one GCP account with 30 Compute instances and\n6 enabled instance metrics costs exactly 6 API calls per cycle (one per\nmetric type), regardless of instance count.\n"""\nimport logging\nimport re\nimport time\nimport json\n\nfrom app.db import get_connection\nfrom app.credentials import load_credential\nfrom app.clients.vm_client import vm_write_batch\n\nlogger = logging.getLogger(__name__)\n\n_WINDOW_SECONDS = 600  # look back 10 min for the latest datapoint\n\n\ndef _slug(name: str) -> str:\n    """\'cpu/utilization\' -> \'cpu_utilization\' for the VM metric name suffix."""\n    s = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()\n    return s or "value"\n\n\ndef _enabled_gcp_metrics(cur, account_id: int):\n    """[(namespace, service, metric_name), ...] for this account\'s enabled selection."""\n    cur.execute("""\n        SELECT mc.namespace, mc.service, mc.metric_name\n        FROM metric_catalog mc\n        JOIN account_metric_selections ams ON ams.metric_id = mc.id\n        WHERE ams.aws_account_id = %s AND ams.enabled = 1\n              AND mc.provider = \'gcp\' AND mc.metric_name IS NOT NULL AND mc.metric_name != \'\'\n    """, (account_id,))\n    return cur.fetchall()\n\n\ndef collect_account_metrics(account: dict) -> dict:\n    """\n    account: a row from aws_accounts (dict) for one GCP account. Must have\n    id, project_id, and a service-account key stored via app.credentials.\n\n    Returns {"pushed": int, "metric_types_queried": int, "errors": [str, ...]}.\n    Never raises -- see the Azure collector\'s docstring for why.\n    """\n    result = {"pushed": 0, "metric_types_queried": 0, "errors": []}\n\n    project_id = (account.get("project_id") or "").strip()\n    sa_key_json = load_credential(account["id"])\n    if not (project_id and sa_key_json):\n        result["errors"].append("missing project_id/service account credential")\n        return result\n\n    try:\n        from google.cloud import monitoring_v3\n        from google.oauth2 import service_account as gcp_service_account\n    except ImportError:\n        result["errors"].append("google-cloud-monitoring not installed (pip install -r requirements.txt)")\n        return result\n\n    try:\n        info = json.loads(sa_key_json)\n        creds = gcp_service_account.Credentials.from_service_account_info(\n            info, scopes=["https://www.googleapis.com/auth/monitoring.read"]\n        )\n        client = monitoring_v3.MetricServiceClient(credentials=creds)\n    except Exception as e:\n        result["errors"].append(f"auth/client setup failed: {e}")\n        return result\n\n    project_name = f"projects/{project_id}"\n    now = time.time()\n    interval = monitoring_v3.TimeInterval({\n        "end_time": {"seconds": int(now)},\n        "start_time": {"seconds": int(now - _WINDOW_SECONDS)},\n    })\n\n    conn = get_connection(); cur = conn.cursor(dictionary=True)\n    try:\n        enabled = _enabled_gcp_metrics(cur, account["id"])\n    finally:\n        cur.close(); conn.close()\n\n    if not enabled:\n        return result\n\n    for row in enabled:\n        metric_type = f"{row[\'namespace\']}/{row[\'metric_name\']}"\n        result["metric_types_queried"] += 1\n        try:\n            time_series = client.list_time_series(\n                request={\n                    "name": project_name,\n                    "filter": f\'metric.type = "{metric_type}"\',\n                    "interval": interval,\n                    "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,\n                }\n            )\n        except Exception as e:\n            result["errors"].append(f"{metric_type}: {e}")\n            continue\n\n        series = []\n        try:\n            for ts in time_series:\n                if not ts.points:\n                    continue\n                latest = ts.points[0]  # Cloud Monitoring returns points newest-first\n                kind = latest.value._pb.WhichOneof("value")\n                if kind == "double_value":\n                    value = latest.value.double_value\n                elif kind == "int64_value":\n                    value = float(latest.value.int64_value)\n                elif kind == "bool_value":\n                    value = 1.0 if latest.value.bool_value else 0.0\n                else:\n                    continue  # distribution/string values aren\'t a single scalar -- skip\n\n                labels = {"account_id": str(account["id"]), "project_id": project_id}\n                for k, v in dict(ts.resource.labels).items():\n                    labels[k] = str(v)\n\n                series.append({\n                    "metric": f"gcp_{row[\'service\']}_{_slug(row[\'metric_name\'])}",\n                    "labels": labels,\n                    "value": value,\n                })\n        except Exception as e:\n            result["errors"].append(f"{metric_type}: error parsing results: {e}")\n            continue\n\n        if series:\n            if vm_write_batch(series):\n                result["pushed"] += len(series)\n            else:\n                result["errors"].append(f"{metric_type}: VM write failed for {len(series)} points")\n\n    return result\n\n\ndef collect_all_gcp_accounts() -> dict:\n    """Runs collect_account_metrics() for every active GCP account. Used by the scheduler."""\n    conn = get_connection(); cur = conn.cursor(dictionary=True)\n    cur.execute("""\n        SELECT * FROM aws_accounts\n        WHERE status = \'active\' AND provider = \'gcp\'\n    """)\n    accounts = cur.fetchall()\n    cur.close(); conn.close()\n\n    totals = {"accounts": len(accounts), "pushed": 0, "errors": []}\n    for account in accounts:\n        r = collect_account_metrics(account)\n        totals["pushed"] += r["pushed"]\n        if r["errors"]:\n            totals["errors"].append({"account_id": account["id"], "errors": r["errors"]})\n            for e in r["errors"]:\n                logger.warning(f"[gcp collector] account={account[\'id\']} {e}")\n    return totals\n'

SCHEDULER = '# app/collector/multicloud_scheduler.py\n"""\nBackground loop that periodically calls the Azure and GCP metrics\ncollectors (app/providers/{azure,gcp}/metrics_collector.py) and pushes\nresults into VictoriaMetrics.\n\nThis is intentionally a single flat interval, not the AWS tiered\ncritical/standard/low split in app/collector/scheduler.py. That tiering\nexists specifically to cut CloudWatch GetMetricData call *volume* because\nAWS bills per call. Azure Monitor and GCP Cloud Monitoring platform-metric\nreads are free (see the cost-note docstrings in each collector module) --\nthere\'s no billing reason to tier them apart, so one interval keeps this\nsimpler until real usage data says otherwise.\n\nDefault interval matches the AWS "standard" tier (5 min) as a reasonable\nmiddle ground: tight enough to be useful, loose enough not to hammer\neither cloud\'s API rate limits across ~20 accounts.\n"""\nimport time\nimport logging\nimport threading\n\nlogger = logging.getLogger(__name__)\n\n_stop_event = threading.Event()\n\nINTERVAL_SECONDS = 300  # 5 min\n\n\ndef run_once():\n    from app.providers.azure.metrics_collector import collect_all_azure_accounts\n    from app.providers.gcp.metrics_collector import collect_all_gcp_accounts\n\n    try:\n        azure_result = collect_all_azure_accounts()\n        logger.info(\n            f"[multicloud] Azure: {azure_result[\'accounts\']} account(s), "\n            f"{azure_result[\'pushed\']} datapoints pushed"\n            + (f", {len(azure_result[\'errors\'])} account(s) had errors" if azure_result["errors"] else "")\n        )\n    except Exception as e:\n        logger.error(f"[multicloud] Azure collection cycle crashed: {e}")\n\n    try:\n        gcp_result = collect_all_gcp_accounts()\n        logger.info(\n            f"[multicloud] GCP: {gcp_result[\'accounts\']} account(s), "\n            f"{gcp_result[\'pushed\']} datapoints pushed"\n            + (f", {len(gcp_result[\'errors\'])} account(s) had errors" if gcp_result["errors"] else "")\n        )\n    except Exception as e:\n        logger.error(f"[multicloud] GCP collection cycle crashed: {e}")\n\n\ndef run_loop(interval: int = INTERVAL_SECONDS):\n    logger.info(f"Multi-cloud (Azure/GCP) metrics scheduler started (interval={interval}s)")\n    while not _stop_event.is_set():\n        run_once()\n        _stop_event.wait(interval)\n\n\ndef stop():\n    _stop_event.set()\n'

MIGRATION_011 = '-- db/migrations/011_widen_resource_id.sql\n--\n-- resources.resource_id was VARCHAR(100). AWS resource IDs (i-xxxx,\n-- vol-xxxx, ARNs for Lambda) fit comfortably. GCP resource paths\n-- (projects/{p}/zones/{z}/instances/{name}) mostly fit too.\n--\n-- Azure ARM resource IDs do NOT fit:\n--   /subscriptions/{36-char-guid}/resourceGroups/{rg}/providers/\n--     Microsoft.Compute/virtualMachines/{name}\n-- routinely runs 120-160+ characters. Under MySQL 8\'s default strict\n-- mode, every Azure resource discovery INSERT for VM/Storage/SQL/App\n-- Service resources has been failing outright with "Data too long for\n-- column \'resource_id\'" since app/providers/azure/discovery.py started\n-- writing full ARM IDs. This widens the column so those inserts (and the\n-- Step 4 Azure metrics collector, which needs the full ARM ID to query\n-- Azure Monitor) actually work.\n--\n-- Safe to run repeatedly / on a table that already has this width.\n\nALTER TABLE resources\n  MODIFY COLUMN resource_id VARCHAR(512) NOT NULL;\n'

MIGRATION_011_ROLLBACK = '-- db/migrations/011_widen_resource_id_rollback.sql\n-- Only safe to run if no resource_id values currently exceed 100 chars.\nALTER TABLE resources\n  MODIFY COLUMN resource_id VARCHAR(100) NOT NULL;\n'

REQ_OLD = 'azure-mgmt-monitor>=6,<7\n'

REQ_NEW = 'azure-mgmt-monitor>=6,<7\nazure-monitor-query>=1.2,<2\n'

VM1_OLD = 'import os\nimport datetime\nimport logging\nimport requests'

VM1_NEW = 'import os\nimport json\nimport datetime\nimport logging\nimport requests'

VM2_OLD = '    except Exception as e:\n        logger.warning(f"VM query_range failed [{promql}]: {e}")\n        return []'

VM2_NEW = '    except Exception as e:\n        logger.warning(f"VM query_range failed [{promql}]: {e}")\n        return []\n\n\ndef vm_write_batch(series: list[dict]) -> bool:\n    """\n    Push datapoints into VictoriaMetrics. Used by the Azure/GCP metric\n    collectors (app/providers/{azure,gcp}/metrics_collector.py) -- there is\n    no YACE-equivalent Prometheus exporter for those clouds, so unlike AWS\n    (where YACE scrapes CloudWatch and pushes to VM on its own), something\n    has to actively write this data in.\n\n    Uses VM\'s /api/v1/import JSON-lines endpoint (see\n    https://docs.victoriametrics.com/#how-to-import-data-in-json-line-format)\n    rather than remote_write/protobuf, since it\'s a plain HTTP+JSON POST with\n    no extra client dependency.\n\n    series: [\n      {\n        "metric": "azure_vm_percentage_cpu",   # becomes __name__\n        "labels": {"account_id": "5", "resource_id": "...", "region": "..."},\n        "value": 42.1,\n        "timestamp": 1735000000,   # unix seconds; omit for "now"\n      },\n      ...\n    ]\n    Returns True if the whole batch was accepted.\n    """\n    if not series:\n        return True\n\n    lines = []\n    now_ms = int(datetime.datetime.utcnow().timestamp() * 1000)\n    for point in series:\n        metric = {"__name__": point["metric"], **point.get("labels", {})}\n        ts_ms = int(point["timestamp"] * 1000) if point.get("timestamp") else now_ms\n        lines.append(json.dumps({\n            "metric": metric,\n            "values": [point["value"]],\n            "timestamps": [ts_ms],\n        }))\n\n    payload = "\\n".join(lines)\n    try:\n        r = requests.post(\n            f"{VM_URL}/api/v1/import",\n            data=payload.encode("utf-8"),\n            headers={"Content-Type": "application/json"},\n            timeout=15,\n        )\n        r.raise_for_status()\n        return True\n    except Exception as e:\n        logger.error(f"VM write_batch failed ({len(series)} datapoints): {e}")\n        return False'

MAIN1_OLD = 'def _run_describe_poll_loop():\n    """\n    Free EC2 status + ALB target health via Describe APIs — not CloudWatch,\n    zero GetMetricData cost either way, so this runs on its own tight loop\n    (default 30s) independent of the tiered scheduler\'s cadence, for the\n    lowest latency the AWS Describe APIs can give us.\n    """\n    import time\n    from app.aws.describe_polling import poll_all\n    interval = 30\n    while True:\n        try:\n            poll_all()\n        except Exception as e:\n            logger.warning(f"Describe-poll loop error: {e}")\n        time.sleep(interval)'

MAIN1_NEW = 'def _run_describe_poll_loop():\n    """\n    Free EC2 status + ALB target health via Describe APIs — not CloudWatch,\n    zero GetMetricData cost either way, so this runs on its own tight loop\n    (default 30s) independent of the tiered scheduler\'s cadence, for the\n    lowest latency the AWS Describe APIs can give us.\n    """\n    import time\n    from app.aws.describe_polling import poll_all\n    interval = 30\n    while True:\n        try:\n            poll_all()\n        except Exception as e:\n            logger.warning(f"Describe-poll loop error: {e}")\n        time.sleep(interval)\n\n\ndef _run_multicloud_collector():\n    """Azure/GCP metric collection — see app/collector/multicloud_scheduler.py for why\n    this is a separate loop from the AWS tiered scheduler rather than folded into it."""\n    try:\n        from app.collector.multicloud_scheduler import run_loop\n        run_loop()\n    except Exception as e:\n        logger.error(f"Multi-cloud collector crashed: {e}")'

MAIN2_OLD = '    threading.Thread(target=_run_collector, daemon=True, name="collector").start()\n    threading.Thread(target=_run_describe_poll_loop, daemon=True, name="describe-poll").start()'

MAIN2_NEW = '    threading.Thread(target=_run_collector, daemon=True, name="collector").start()\n    threading.Thread(target=_run_describe_poll_loop, daemon=True, name="describe-poll").start()\n    threading.Thread(target=_run_multicloud_collector, daemon=True, name="multicloud-collector").start()'


FULL_WRITES = [
    ("app/providers/azure/metrics_collector.py", AZURE_COLLECTOR, "python"),
    ("app/providers/gcp/metrics_collector.py", GCP_COLLECTOR, "python"),
    ("app/collector/multicloud_scheduler.py", SCHEDULER, "python"),
    ("db/migrations/011_widen_resource_id.sql", MIGRATION_011, "text"),
    ("db/migrations/011_widen_resource_id_rollback.sql", MIGRATION_011_ROLLBACK, "text"),
]

SURGICAL_PATCHES = [
    ("requirements.txt", [(REQ_OLD, REQ_NEW)], "text"),
    ("app/clients/vm_client.py", [(VM1_OLD, VM1_NEW), (VM2_OLD, VM2_NEW)], "python"),
    ("app/main.py", [(MAIN1_OLD, MAIN1_NEW), (MAIN2_OLD, MAIN2_NEW)], "python"),
]


def log(msg):
    print(msg, flush=True)


def apply_full_write(path_str, content, kind, dry_run):
    path = Path(path_str)
    if not path.parent.exists():
        log(f"  ABORT: parent directory {path.parent} does not exist -- wrong repo root?")
        return False
    existed = path.exists()
    if existed and path.read_text(encoding="utf-8") == content:
        log(f"  No changes needed for {path_str} (already up to date).")
        return True
    if dry_run:
        log(f"  [dry-run] would {'overwrite' if existed else 'create'} {path_str} "
            f"({len(content)} bytes){' (backup would be made)' if existed else ''}")
        return True

    backup_path = None
    if existed:
        backup_path = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup_path)

    path.write_text(content, encoding="utf-8")

    if kind == "python":
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as e:
            log(f"  COMPILE FAILED for {path_str}: {e}")
            if backup_path:
                shutil.copy2(backup_path, path)
                log(f"  Reverted {path_str} from backup.")
            else:
                path.unlink(missing_ok=True)
                log(f"  Removed newly-created {path_str} (no prior version to revert to).")
            return False

    log(f"  OK: wrote {path_str} ({len(content)} bytes){' [.bak made]' if backup_path else ''}")
    return True


def apply_surgical_patches(path_str, patch_pairs, kind, dry_run):
    path = Path(path_str)
    if not path.exists():
        log(f"  ABORT: {path_str} does not exist -- wrong repo root, or the file was moved/"
            f"renamed since this script was written. Skipping.")
        return False

    original = path.read_text(encoding="utf-8")
    working = original
    problems = []
    for i, (old, new) in enumerate(patch_pairs, start=1):
        if new in working:
            log(f"  Patch {i}/{len(patch_pairs)} for {path_str}: already applied, skipping.")
            continue
        count = working.count(old)
        if count == 0:
            problems.append(f"patch {i}: anchor not found (local file has drifted from what "
                             f"this script expects -- re-check the file content before retrying)")
            continue
        if count > 1:
            problems.append(f"patch {i}: anchor matches {count} times, expected exactly 1 "
                             f"(ambiguous -- aborting this file to avoid a wrong replacement)")
            continue
        working = working.replace(old, new, 1)

    if problems:
        log(f"  ABORT {path_str}: not applying any changes to this file because:")
        for p in problems:
            log(f"    - {p}")
        return False

    if working == original:
        log(f"  No changes needed for {path_str} (already up to date).")
        return True

    if dry_run:
        log(f"  [dry-run] would apply {len(patch_pairs)} patch(es) to {path_str} "
            f"(backup would be made)")
        return True

    backup_path = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup_path)
    path.write_text(working, encoding="utf-8")

    if kind == "python":
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as e:
            log(f"  COMPILE FAILED for {path_str}: {e}")
            shutil.copy2(backup_path, path)
            log(f"  Reverted {path_str} from backup.")
            return False

    log(f"  OK: patched {path_str} [.bak made]")
    return True


def try_apply_db_migration(dry_run):
    """Best-effort: apply migration 011 directly if a DB connection works, using
    the same env-var convention as app/db.py. Falls back to printing manual
    instructions -- this is NOT required for the file changes above to succeed."""
    import os
    try:
        import mysql.connector
    except ImportError:
        log("  mysql-connector-python not importable from this interpreter -- skipping "
            "auto-apply. Run the migration manually (see below).")
        return

    cfg = dict(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", 3307)),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", "root123"),
        database=os.getenv("DB_NAME", "monitoring_hub"),
    )
    if dry_run:
        log(f"  [dry-run] would attempt to connect to {cfg['host']}:{cfg['port']}/{cfg['database']} "
            f"and apply db/migrations/011_widen_resource_id.sql")
        return

    try:
        conn = mysql.connector.connect(**cfg)
        cur = conn.cursor()
        cur.execute("ALTER TABLE resources MODIFY COLUMN resource_id VARCHAR(512) NOT NULL")
        conn.commit()
        cur.close(); conn.close()
        log(f"  OK: applied migration 011 directly against {cfg['host']}:{cfg['port']}/{cfg['database']}")
    except Exception as e:
        log(f"  Could not auto-apply migration 011 ({e}).")
        log(f"  Apply manually: mysql -u{cfg['user']} -p {cfg['database']} < "
            f"db/migrations/011_widen_resource_id.sql")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Preview changes without writing anything")
    ap.add_argument("--skip-db", action="store_true", help="Skip the DB migration auto-apply attempt")
    args = ap.parse_args()

    if not Path("app").exists() or not Path("frontend").exists():
        log("ERROR: run this from the repo root (both ./app and ./frontend must exist here).")
        sys.exit(1)

    log(f"{'DRY RUN -- ' if args.dry_run else ''}Applying multi-cloud step 4-7 (collectors) changes...\n")

    ok = True

    log("== New files ==")
    for path_str, content, kind in FULL_WRITES:
        ok &= apply_full_write(path_str, content, kind, args.dry_run)

    log("\n== Surgical patches to existing files ==")
    for path_str, pairs, kind in SURGICAL_PATCHES:
        ok &= apply_surgical_patches(path_str, pairs, kind, args.dry_run)

    if not args.skip_db:
        log("\n== DB migration 011 (resources.resource_id width fix) ==")
        try_apply_db_migration(args.dry_run)

    log("")
    if args.dry_run:
        log("Dry run complete. Re-run without --dry-run to apply.")
    elif ok:
        log("All file changes applied successfully.")
        log("")
        log("Next steps:")
        log("  1. If the DB migration above wasn't auto-applied, run it manually:")
        log("       mysql -uroot -p monitoring_hub < db/migrations/011_widen_resource_id.sql")
        log("  2. pip install -r requirements.txt --break-system-packages")
        log("     (adds azure-monitor-query -- confirm it resolves to 1.x, not 2.x)")
        log("  3. Restart the backend. Watch the logs for:")
        log("       'Multi-cloud (Azure/GCP) metrics scheduler started (interval=300s)'")
        log("     and, after ~5 min, either '[multicloud] Azure: N account(s), M datapoints")
        log("     pushed' or an error per account -- that error text tells you exactly what's")
        log("     missing (credential, region, resource discovery not run yet, etc).")
        log("  4. Test against ONE real Azure and ONE real GCP account before trusting this")
        log("     for alerting -- see the big caveat at the top of this script's docstring.")
    else:
        log("Some files were NOT changed -- see ABORT/FAILED messages above. Nothing else was "
            "rolled back; every file that succeeded is already applied with a .bak next to it.")
        sys.exit(1)


if __name__ == "__main__":
    main()
