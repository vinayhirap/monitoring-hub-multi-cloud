# tests/test_polling_model_audit.py
"""
Regression tests for the 2026-09-23 metric/polling audit:
polling model, GetMetricData paging/SEARCH chunking, extended per-metric
tiers + namespace/region/dimension fixes, Azure aggregation/regions/null
handling, GCP aggregation, metric-aware alert windows, time-gated breach
counting, P1 evaluation filter, API-usage counters, attached-EBS status.
"""
import sys
from datetime import datetime
from unittest.mock import MagicMock

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub, install_polling_modules


def _real(dotted, path):
    mod = load_module(path)
    sys.modules[dotted] = mod
    parent, _, leaf = dotted.rpartition(".")
    install_stub(parent, **{leaf: mod})
    return mod


def _real_catalogs():
    _real("app.threshold_defaults", "app/threshold_defaults.py")
    _real("app.aws.metric_catalog_data", "app/aws/metric_catalog_data.py")
    _real("app.providers.azure.metric_catalog_data", "app/providers/azure/metric_catalog_data.py")
    _real("app.providers.gcp.metric_catalog_data", "app/providers/gcp/metric_catalog_data.py")
    _real("app.providers.azure.severity_tiers", "app/providers/azure/severity_tiers.py")
    _real("app.providers.gcp.severity_tiers", "app/providers/gcp/severity_tiers.py")


# ── polling model ─────────────────────────────────────────────────────

def test_core_metric_definitions_are_consistent():
    _real_catalogs()
    pm, _ = install_polling_modules()
    seen = set()
    for m in pm.AWS_CORE_METRICS:
        assert m.tier in ("critical", "standard", "low"), m
        key = (m.resource_type, m.gate, m.cw_name)
        assert key not in seen, f"duplicate definition {key}"
        seen.add(key)
        # look-back must cover the poll interval
        assert m.lookback_min * 60 >= pm.TIER_SECONDS[m.tier], m
    # EC2 basic monitoring: 5-min points visible 5-10 min late
    ec2 = [m for m in pm.AWS_CORE_METRICS if m.resource_type == "ec2" and m.tier == "standard"]
    assert ec2 and all(m.lookback_min >= 15 for m in ec2)
    names = {m.cw_name for m in pm.AWS_CORE_METRICS}
    assert "DiskReadBytes" not in names
    assert {"HTTPCode_ELB_5XX_Count", "TargetConnectionErrorCount", "ActiveFlowCount",
            "ReplicaLag", "DiskQueueDepth", "ConcurrentExecutions", "IteratorAge",
            "CPUCreditBalance"} <= names


def test_interval_overrides_follow_real_cadence():
    _real_catalogs()
    pm, _ = install_polling_modules()
    ov = pm.metric_interval_overrides()
    assert ov[("aws", "ebs", "volumereadops")] == 900
    assert ov[("aws", "sqs", "approximateageofoldestmessage")] == 300
    assert ov[("aws", "certificatemanager", "daystoexpiry")] == 86400
    assert ov[("azure", "vm", "disk read bytes")] == 900
    assert ov[("gcp", "gcs_bucket", "total_bytes")] == 900
    assert ("aws", "ec2", "cpuutilization") not in ov   # 5 min == class default
    keys = pm.p1_metric_keys()
    assert ("rds", "cpuutilization") in keys and ("app_service", "http5xx") in keys
    assert ("ec2", "cpuutilization") not in keys


def test_alert_rules_metric_aware_sql():
    _real_catalogs()
    install_polling_modules()
    install_stub("app.alert_visibility", hidden_metrics_sql=lambda: "''",
                 HIDDEN_FROM_ALERTS_UI_METRICS=())
    ar = load_module("app/alert_rules.py")
    sql = ar.eval_window_sql("r", "aa", "m.metric_name")
    assert "('aws', 'ebs', 'volumereadops')" in sql and "THEN 25" in sql
    assert chr(92) * 2 + "_" in sql                       # escaped LIKE underscore
    assert ar.eval_window_sql("r", "aa") == ar._by_class_sql(ar.EVAL_WINDOW_MINUTES, "r", "aa")
    assert "a.metric_name" in ar.is_stale_sql("a", "r", "acc")
    assert ar._sql_str("o'x") == "'o''x'"


# ── runner GetMetricData ──────────────────────────────────────────────

def _load_runner():
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.aws.sts", get_boto3_session=lambda a: MagicMock())
    install_stub("app.aws.boto_config", STANDARD_RETRY=None)
    writes = {"latest": [], "history": []}
    install_stub("app.collector.metrics_writer",
                 write_metrics_batch=lambda rows: writes["latest"].extend(rows),
                 write_metric_history_batch=lambda rows: writes["history"].extend(rows))
    install_stub("app.collector.disk_mounts", all_cwagent_disk_dims=lambda cw, iid: [],
                 ensure_disk_mount_metric_registered=lambda *a, **k: None)
    pm, au = install_polling_modules()
    return load_module("app/collector/metrics/runner.py"), writes, au


def test_gmd_follows_next_token_and_records_usage():
    mod, writes, au = _load_runner()
    calls = []

    class CW:
        def get_metric_data(self, **kw):
            calls.append(kw.get("NextToken"))
            if "NextToken" not in kw:
                return {"MetricDataResults": [{"Id": "q0", "Timestamps": [2], "Values": [2.0]}],
                        "NextToken": "p2"}
            return {"MetricDataResults": [{"Id": "q0", "Timestamps": [1], "Values": [1.0]}]}

    q = [{"Id": "q0", "MetricStat": {}, "ReturnData": True}]
    assert mod._execute_gmd(CW(), q, {"q0": (7, "m")}) == 1
    assert calls == [None, "p2"]
    assert writes["latest"] == [(7, "m", 2.0)]
    assert len(writes["history"]) == 2
    assert sum(v[1] for v in au.snapshot().values()) == 1


def test_gmd_paging_never_loops_on_non_string_token():
    mod, _, _ = _load_runner()
    cw = MagicMock()   # .get("NextToken") -> MagicMock, not a str
    mod._execute_gmd(cw, [{"Id": "q0", "MetricStat": {}, "ReturnData": True}], {"q0": (1, "m")})
    assert cw.get_metric_data.call_count == 1


def test_search_expressions_isolated_in_chunks_of_five():
    mod, _, _ = _load_runner()
    plain = [{"Id": f"p{i}", "MetricStat": {}, "ReturnData": True} for i in range(3)]
    search = [{"Id": f"s{i}", "Expression": "SUM(SEARCH('x','Sum',300))", "ReturnData": True}
              for i in range(12)]
    chunks = mod._request_chunks(plain + search)
    assert [len(c) for c in chunks] == [3, 5, 5, 2]
    assert all(all(mod._is_search(q) for q in c) for c in chunks[1:])


def test_gates_route_alb_nlb_tclass_replica():
    mod, _, _ = _load_runner()
    alb = {"resource_type": "elb", "resource_id": "arn:...:loadbalancer/app/a/1", "tags": "{}"}
    nlb = {"resource_type": "elb", "resource_id": "arn:...:loadbalancer/net/n/2", "tags": "{}"}
    assert mod._passes_gate(alb, "alb") and not mod._passes_gate(nlb, "alb")
    assert mod._passes_gate(nlb, "nlb")
    t3 = {"resource_type": "ec2", "resource_id": "i-1", "tags": '{"_instance_type": "t3.micro"}'}
    m5 = {"resource_type": "ec2", "resource_id": "i-2", "tags": {"_instance_type": "m5.large"}}
    assert mod._passes_gate(t3, "tclass") and not mod._passes_gate(m5, "tclass")
    assert mod._passes_gate({"resource_type": "rds", "resource_id": "r", "tags": {"_replica_source": "db1"}}, "replica")
    assert not mod._passes_gate({"resource_type": "rds", "resource_id": "r", "tags": {}}, "replica")


# ── extended ──────────────────────────────────────────────────────────

def _load_extended(captured):
    _real_catalogs()
    install_polling_modules()
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.aws.boto_config", STANDARD_RETRY=None)

    def fake_exec(cw, queries, id_map, minutes=5):
        captured.append((queries, minutes))
        return 0
    install_stub("app.collector.metrics.runner", _execute_gmd=fake_exec, STALE_RESOURCE_HOURS=48)
    return load_module("app/collector/metrics/extended.py")


def test_extended_per_metric_tiers_and_fixes():
    captured = []
    ext = _load_extended(captured)
    sqs = {"id": 1, "resource_id": "q1", "resource_type": "sqs", "name": "q1", "tags": {}}
    enabled = {("sqs", "ApproximateAgeOfOldestMessage"), ("sqs", "NumberOfMessagesSent")}
    ext._collect_extended_service(MagicMock(), [sqs], "sqs", enabled, minutes=15, tier="standard")
    names = [q["MetricStat"]["Metric"]["MetricName"] for q in captured[-1][0]]
    assert names == ["ApproximateAgeOfOldestMessage"]

    ecs_defs = dict((d[0], d[3]) for d in ext.EXTENDED_METRICS["ecs"])
    assert ecs_defs["RunningTaskCount"] == "ECS/ContainerInsights"
    assert ecs_defs["CPUUtilization"] == "AWS/ECS"
    assert "UserErrors" not in [d[0] for d in ext.EXTENDED_METRICS["dynamodb"]]
    assert ext._region_for_service("route53", "ap-south-1") == "us-east-1"
    ga = {"resource_type": "globalaccelerator", "resource_id": "arn:aws:globalaccelerator::1:accelerator/abc-123",
          "name": "ga", "tags": {}}
    assert ext._build_dimensions(ga) == [{"Name": "Accelerator", "Value": "abc-123"}]


def test_search_query_for_dynamodb_and_msk():
    ext = _load_extended([])
    table = {"id": 1, "resource_id": "orders", "resource_type": "dynamodb", "name": "orders", "tags": {}}
    q = ext._search_query("e0", "dynamodb", "ThrottledRequests", table, "AWS/DynamoDB")
    assert q["Expression"] == ("SUM(SEARCH('{AWS/DynamoDB,Operation,TableName} "
                               "MetricName=\"ThrottledRequests\" TableName=\"orders\"', 'Sum', 300))")
    msk = {"id": 2, "resource_id": "kafka-1", "resource_type": "msk", "name": "kafka-1", "tags": {}}
    q2 = ext._search_query("e1", "msk", "CpuUser", msk, "AWS/Kafka")
    assert '"Broker ID"' in q2["Expression"] and '"Cluster Name"="kafka-1"' in q2["Expression"]
    bad = dict(table, resource_id="x' OR 1")
    assert ext._search_query("e2", "dynamodb", "ThrottledRequests", bad, "AWS/DynamoDB") is None


# ── Azure ─────────────────────────────────────────────────────────────

class _P:
    def __init__(self, ts, **vals):
        self.timestamp = ts
        for k in ("average", "total", "maximum", "minimum", "count"):
            setattr(self, k, vals.get(k))


def test_azure_aggregation_regions_and_null_latest():
    _real_catalogs()
    install_polling_modules()
    written = {"m": [], "h": []}
    install_stub("app.collector.metrics_writer",
                 write_metrics_batch=lambda r: written["m"].extend(r),
                 write_metric_history_batch=lambda r: written["h"].extend(r))
    install_stub("app.credentials", load_credential=lambda a: "s")
    install_stub("azure.identity", ClientSecretCredential=lambda **k: object())
    calls = []

    class Agg:
        AVERAGE, TOTAL = "Average", "Total"

    class Client:
        def __init__(self, endpoint):
            self.endpoint = endpoint

        def query_resources(self, **kw):
            calls.append((self.endpoint, kw["metric_namespace"], tuple(kw["metric_names"]),
                          kw["aggregations"], kw["granularity"].total_seconds()))
            out = []
            for _ in kw["resource_ids"]:
                metrics = []
                for name in kw["metric_names"]:
                    ts = type("TS", (), {"data": [_P(datetime(2026, 9, 23, 10, 0), total=3.0),
                                                   _P(datetime(2026, 9, 23, 10, 1))]})()   # newest null
                    metrics.append(type("M", (), {"name": name, "timeseries": [ts]})())
                out.append(type("R", (), {"metrics": metrics})())
            return out

    install_stub("azure.monitor.query", MetricsClient=lambda endpoint, cred: Client(endpoint),
                 MetricAggregationType=Agg)

    class Cur:
        def execute(self, sql, params=None):
            self.rows = ([{"id": 1, "resource_id": "/s/x/sites/a", "name": "a", "region": "centralindia"},
                          {"id": 2, "resource_id": "/s/x/sites/b", "name": "b", "region": "West Europe"}]
                         if "FROM resources" in sql else [])

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    install_stub("app.db", get_connection=lambda: type("C", (), {"cursor": lambda s, dictionary=True: Cur(),
                                                                  "close": lambda s: None})())
    mod = load_module("app/providers/azure/metrics_collector.py")
    import unittest.mock as um
    enabled = {("Microsoft.Web/sites/functions", "function_app"): {"Http5xx"},
               ("Microsoft.Storage/storageAccounts", "storage_account"): {"UsedCapacity"}}
    with um.patch.object(mod, "_enabled_azure_metrics", return_value=enabled):
        res = mod.collect_account_metrics({"id": 5, "tenant_id": "t", "client_id": "c",
                                           "subscription_id": "s", "default_region": "centralindia"},
                                          window_seconds=420)
    endpoints = {c[0] for c in calls}
    assert endpoints == {"https://centralindia.metrics.monitor.azure.com",
                         "https://westeurope.metrics.monitor.azure.com"}
    http = [c for c in calls if c[2] == ("Http5xx",)]
    assert all(c[1] == "Microsoft.Web/sites" and c[3] == ["Total"] for c in http)
    used = [c for c in calls if c[2] == ("UsedCapacity",)]
    assert used and all(c[4] == 3600 for c in used)
    assert (1, "Http5xx", 3.0) in written["m"]          # newest NON-null point
    assert res["errors"] == []


def test_azure_directory_metrics_pass_tier_filter():
    install_stub("app.credentials", load_credential=lambda a: "s")
    install_stub("app.collector.metrics_writer", write_metrics_batch=lambda r: None,
                 write_metric_history_batch=lambda r: None)
    install_stub("app.db", get_connection=lambda: None)
    mod = load_module("app/providers/azure/metrics_collector.py")

    class Cur:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return [{"namespace": "Microsoft.X/y", "service": "xsvc", "metric_name": "Foo", "category": "directory"},
                    {"namespace": "Microsoft.Web/sites", "service": "app_service", "metric_name": "Http4xx",
                     "category": "core"}]
    out = mod._enabled_azure_metrics(Cur(), 1, categories=("extended", "directory"),
                                     only_metric_names={"app_service": {"Http5xx"}})
    assert out == {("Microsoft.X/y", "xsvc"): {"Foo"}}


# ── GCP ───────────────────────────────────────────────────────────────

def test_gcp_aligner_choice():
    install_stub("app.credentials", load_credential=lambda a: "{}")
    install_stub("app.collector.metrics_writer", write_metrics_batch=lambda r: None,
                 write_metric_history_batch=lambda r: None)
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.providers.gcp.metrics_extended", EXTENDED_RESOLVERS={})
    mod = load_module("app/providers/gcp/metrics_collector.py")
    assert mod._choose_aligner("DELTA", "DISTRIBUTION", "Average") == ("ALIGN_MEAN", "REDUCE_MEAN")
    assert mod._choose_aligner("CUMULATIVE", "DISTRIBUTION", "Average") == ("ALIGN_DELTA", "REDUCE_MEAN")
    assert mod._choose_aligner("DELTA", "INT64", "Total") == ("ALIGN_SUM", "REDUCE_SUM")
    assert mod._choose_aligner("CUMULATIVE", "INT64", "Total") == ("ALIGN_DELTA", "REDUCE_SUM")
    assert mod._choose_aligner("GAUGE", "DOUBLE", "Average") == ("ALIGN_MEAN", "REDUCE_MEAN")
    assert mod._choose_aligner("GAUGE", "BOOL", "Average") == ("ALIGN_FRACTION_TRUE", "REDUCE_MEAN")
    assert mod._choose_aligner("GAUGE", "STRING", "Average") is None
    assert "cpu/usage_time" in mod._REMOVED_METRICS["compute_instance"]


# ── alert evaluator + api usage + describe ───────────────────────────

def test_evaluator_counters_are_time_gated():
    src = open("app/collector/alert_evaluator.py").read()
    assert "breach_cycles   = IF(last_seen_at <= DATE_SUB(UTC_TIMESTAMP(), INTERVAL {MIN_CYCLE_SECONDS} SECOND)" in src
    assert src.count("healthy_streak = {_GATED_STREAK_SQL}") == 2
    # counters must be assigned BEFORE last_seen_at (MySQL left-to-right SET)
    for chunk in src.split("healthy_streak = {_GATED_STREAK_SQL}")[1:]:
        assert "last_seen_at" in chunk[:120]
    assert "def evaluate_alerts(p1_only=False)" in src


def test_api_usage_flush_writes_one_row_per_provider_tier():
    rows = []
    install_stub("app.collector.op_log", log_event=lambda *a, **k: rows.append((a, k)))
    _, au = install_polling_modules()
    au.record("aws", "standard", calls=2, units=1000)
    au.record("aws", "standard", calls=1, units=500)
    au.record("gcp", "low", calls=1, units=2_000_000)
    assert au.flush() == 2
    detail = {k["detail"]["provider"]: k["detail"] for _, k in rows}
    assert detail["aws"]["units"] == 1500 and abs(detail["aws"]["list_price_usd"] - 0.015) < 1e-9
    assert abs(detail["gcp"]["list_price_usd"] - 1.0) < 1e-9
    assert au.flush() == 0


def test_attached_ebs_impaired_fails_status_check():
    src = open("app/aws/describe_polling.py").read()
    assert 'get("AttachedEbsStatus", {}).get("Status") == "impaired"' in src
    assert "sys_ok and inst_ok and not ebs_bad" in src
