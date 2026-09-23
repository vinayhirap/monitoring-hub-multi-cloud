# tests/test_collector_direct_b11.py
"""
Regression tests for audit b11 (app/aws/collector_direct.py L1-1290):
account fail-closed helpers, _cached single-flight, _smart_period
retention rounding, GetMetricData NextToken paging, RDS/ECS pagination,
ECS query-id uniqueness, S3 bucket-region metrics and public-access
detection. No network: boto3 sessions/clients are faked.
"""
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub


class _NoDbConn:
    def cursor(self, dictionary=True):
        raise AssertionError("DB must not be touched when account_id is missing")

    def close(self):
        pass


def _load(conn=None):
    install_stub("app.db", get_connection=lambda: conn or _NoDbConn())
    install_stub("app.clients.vm_client", vm_query=lambda p: None, vm_query_all=lambda p, d: {})
    install_stub("app.collector.disk_mounts", all_cwagent_disk_dims=lambda cw, instance_id: [])
    install_stub("app.aws.boto_config", STANDARD_RETRY=None, CONCURRENT_CLIENT_RETRY=None)
    return load_module("app/aws/collector_direct.py")


# ── fail-closed account scoping ─────────────────────────────────────────

def test_history_range_without_account_fails_closed():
    mod = _load()
    assert mod._metric_history_query_range("logs", "System", "incomingbytes",
                                           datetime(2026, 9, 8), datetime(2026, 9, 9)) == []


def test_snapshot_without_account_fails_closed():
    mod = _load()
    assert mod._metric_snapshot_query_all("ec2", "cpuutilization") == {}


def test_history_range_rejects_unknown_match_field():
    mod = _load()
    assert mod._metric_history_query_range("ec2", "i-1", "cpu", datetime(2026, 9, 8),
                                           datetime(2026, 9, 9), match_field="1=1 OR name",
                                           account_id=7) == []


# ── _cached ─────────────────────────────────────────────────────────────

def test_cached_single_flight_under_concurrency():
    mod = _load()
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.2)
        return "v"

    results = []
    threads = [threading.Thread(target=lambda: results.append(mod._cached("k", slow)))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["v"] * 8
    assert len(calls) == 1


def test_cached_does_not_store_exceptions_and_prunes_expired():
    mod = _load()

    def boom():
        raise RuntimeError("x")

    try:
        mod._cached("err", boom)
    except RuntimeError:
        pass
    assert "err" not in mod._cache
    mod._cache["old"] = {"data": 1, "ts": time.time() - 10_000, "ttl": 60}
    mod._cache_prune_locked(time.time())
    assert "old" not in mod._cache


# ── _smart_period ───────────────────────────────────────────────────────

def test_smart_period_respects_cloudwatch_retention_steps():
    mod = _load()
    assert mod._smart_period(6) == 60
    assert mod._smart_period(168) % 60 == 0
    for h in (361, 400, 720, 1000, 1512):
        assert mod._smart_period(h) % 300 == 0, h
    for h in (1513, 2160, 8760):
        assert mod._smart_period(h) % 3600 == 0, h
    for h in (6, 24, 168, 400, 2160):
        assert h * 3600 / mod._smart_period(h) <= 1440


# ── GetMetricData paging ────────────────────────────────────────────────

class _PagedCW:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_metric_data(self, **kw):
        self.calls.append(kw)
        idx = 0 if "NextToken" not in kw else int(kw["NextToken"])
        return self.pages[idx]


def test_gmd_series_follows_next_token_and_appends():
    mod = _load()
    t1 = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 8, 11, tzinfo=timezone.utc)
    cw = _PagedCW([
        {"MetricDataResults": [{"Id": "q0", "Timestamps": [t1], "Values": [1.0]}], "NextToken": "1"},
        {"MetricDataResults": [{"Id": "q0", "Timestamps": [t2], "Values": [2.0]}]},
    ])
    q = mod._make_query("q0", "AWS/EC2", "CPUUtilization", [], "Average")
    out = mod._gmd_series(cw, [q], hours=6)
    assert [p["v"] for p in out["q0"]] == [1.0, 2.0]
    assert len(cw.calls) == 2


def test_gmd_snapshot_keeps_newest_across_pages():
    mod = _load()
    cw = _PagedCW([
        {"MetricDataResults": [{"Id": "q0", "Values": [9.0]}], "NextToken": "1"},
        {"MetricDataResults": [{"Id": "q0", "Values": [1.0]}]},
    ])
    q = mod._make_query("q0", "AWS/EC2", "CPUUtilization", [], "Average")
    assert mod._gmd_snapshot(cw, [q]) == {"q0": 9.0}


# ── fake boto3 plumbing for collectors ──────────────────────────────────

class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kw):
        pages = self._pages(**kw) if callable(self._pages) else self._pages
        return iter(pages)


class _Client:
    def __init__(self, paginators=None, **methods):
        self._paginators = paginators or {}
        for k, v in methods.items():
            setattr(self, k, v)

    def get_paginator(self, name):
        return _Paginator(self._paginators[name])


class _Session:
    def __init__(self, clients):
        self.clients = clients
        self.client_kwargs = {}

    def client(self, name, **kw):
        self.client_kwargs[name] = kw
        return self.clients[name]


def test_rds_reads_every_page():
    mod = _load()
    rds = _Client(paginators={"describe_db_instances": [
        {"DBInstances": [{"DBInstanceIdentifier": f"db{i}"} for i in range(100)]},
        {"DBInstances": [{"DBInstanceIdentifier": "db100"}]},
    ]})
    mod.get_session = lambda *a, **k: _Session({"rds": rds})
    assert len(mod._rds_raw("ap-south-1", account={"id": 7})) == 101


def test_ecs_paginates_chunks_and_uses_unique_qids():
    mod = _load()
    svc_arns = [f"arn:svc/{i}" for i in range(12)]
    described = []

    def describe_services(cluster, services):
        assert len(services) <= 10
        described.extend(services)
        return {"services": [{"serviceName": s.split("/")[-1].replace("svc", "a-b") if i % 2 else "a_b",
                              "serviceArn": s} for i, s in enumerate(services)]}

    def describe_clusters(clusters, include):
        assert len(clusters) <= 100
        return {"clusters": [{"clusterName": c.split("/")[-1], "clusterArn": c} for c in clusters]}

    ecs = _Client(
        paginators={
            "list_clusters": [{"clusterArns": ["arn:cluster/c-1"]}],
            "list_services": lambda cluster: [{"serviceArns": svc_arns[:10]},
                                              {"serviceArns": svc_arns[10:]}],
        },
        describe_clusters=describe_clusters,
        describe_services=describe_services,
    )
    seen = {}

    def gmd(**kw):
        ids = [q["Id"] for q in kw["MetricDataQueries"]]
        seen["ids"] = ids
        return {"MetricDataResults": []}

    cw = _Client(get_metric_data=gmd)
    mod.get_session = lambda *a, **k: _Session({"ecs": ecs, "cloudwatch": cw})
    out = mod._ecs_raw("ap-south-1", account={"id": 7})
    assert len(described) == 12
    assert len(out[0]["services"]) == 12
    assert len(seen["ids"]) == len(set(seen["ids"])) == 24


def test_s3_metric_series_queries_bucket_region_and_hides_raw_errors():
    mod = _load()
    regions = []

    class _CW:
        def get_metric_statistics(self, **kw):
            return {"Datapoints": []}

    class _S3:
        def get_bucket_location(self, Bucket):
            return {"LocationConstraint": "ap-south-1"}

    class _Sess:
        def client(self, name, region_name=None, config=None):
            if name == "cloudwatch":
                regions.append(region_name)
                return _CW()
            return _S3()

    mod.get_session = lambda *a, **k: _Sess()
    out = mod.get_s3_metric_series("b1", 24, account={"id": 7})
    assert regions == ["ap-south-1"]
    assert out["bucket_size"] == []

    def broken(*a, **k):
        raise RuntimeError("secret-ish internal detail")
    mod.get_session = broken
    out = mod.get_s3_metric_series("b2", 24, account={"id": 7})
    assert "secret-ish" not in out["note"]


def test_s3_bucket_detail_public_access_states():
    mod = _load()

    class _Err(Exception):
        def __init__(self, code):
            self.response = {"Error": {"Code": code}}

    def make(pab):
        class _S3:
            def get_bucket_location(self, Bucket):
                return {"LocationConstraint": "EU"}

            def get_bucket_versioning(self, Bucket):
                return {"Status": "Enabled"}

            def get_public_access_block(self, Bucket):
                if isinstance(pab, Exception):
                    raise pab
                return {"PublicAccessBlockConfiguration": pab}
        return _S3()

    b = {"Name": "b", "CreationDate": datetime(2026, 1, 1)}
    full = dict(BlockPublicAcls=True, IgnorePublicAcls=True, BlockPublicPolicy=True, RestrictPublicBuckets=True)
    r = mod._s3_bucket_detail(make(full), b)
    assert r["public_access"] is False and r["region"] == "eu-west-1"
    assert mod._s3_bucket_detail(make(dict(full, IgnorePublicAcls=False)), b)["public_access"] is True
    assert mod._s3_bucket_detail(make(_Err("NoSuchPublicAccessBlockConfiguration")), b)["public_access"] is True
    assert mod._s3_bucket_detail(make(_Err("AccessDenied")), b)["public_access"] is None
