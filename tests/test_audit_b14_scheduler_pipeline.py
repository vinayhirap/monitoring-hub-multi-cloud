# tests/test_audit_b14_scheduler_pipeline.py
"""
Regression coverage for audit B14 (scheduler, leader election, metrics
pipeline):
  - runner: GetMetricData chunking at 500 queries without splitting a
    metric-math pair; one batched last-value write per call; non-EC2
    resources (instance_state NULL) are collected; stale rows skipped;
    connections released on error; non-AWS accounts never reach boto3.
  - scheduler: leadership lost mid-iteration stops the remaining tiers;
    a failing tier is retried at its normal cadence, not every 2 minutes.
  - metrics_writer: bounded concurrent writers; batched prune.
"""
import sys
import threading
import time
from unittest.mock import MagicMock

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app  # noqa: F401

from tests.conftest import load_module, install_stub, install_polling_modules


# ── runner helpers ──────────────────────────────────────────────────

class _Cur:
    def __init__(self, log, rows=None, fail=False):
        self.log, self.rows, self.fail = log, rows or [], fail
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))
        if self.fail:
            raise RuntimeError("boom")

    def fetchall(self):
        return self.rows

    def close(self):
        self.log.append(("cursor.close", None))


class _Conn:
    def __init__(self, log, rows=None, fail=False):
        self.log, self.rows, self.fail = log, rows, fail

    def cursor(self, dictionary=True):
        return _Cur(self.log, self.rows, self.fail)

    def commit(self):
        pass

    def close(self):
        self.log.append(("conn.close", None))


def _load_runner(conn_factory=None, writes=None):
    writes = writes if writes is not None else {"latest": [], "history": []}
    install_stub("app.db", get_connection=conn_factory or (lambda: _Conn([])))
    install_stub("app.aws.sts", get_boto3_session=lambda account: MagicMock())
    install_stub("app.aws.boto_config", STANDARD_RETRY=None)
    install_stub("app.collector.metrics_writer",
                 write_metric=lambda *a, **k: None,
                 write_metrics_batch=lambda rows: writes["latest"].append(list(rows)),
                 write_metric_history_batch=lambda rows: writes["history"].append(list(rows)))
    install_stub("app.collector.disk_mounts",
                 all_cwagent_disk_dims=lambda cw, iid: [],
                 ensure_disk_mount_metric_registered=lambda *a, **k: None)
    install_polling_modules()
    return load_module("app/collector/metrics/runner.py"), writes


def _q(i, hidden=False):
    return {"Id": f"q{i}", "ReturnData": not hidden}


def test_chunking_caps_at_500():
    mod, _ = _load_runner()
    chunks = mod._chunk_gmd_queries([_q(i) for i in range(1001)])
    assert [len(c) for c in chunks] == [500, 500, 1]


def test_chunking_never_ends_on_hidden_input():
    mod, _ = _load_runner()
    queries = [_q(i) for i in range(499)] + [_q("raw", hidden=True),
                                              {"Id": "expr", "Expression": "100 - qraw", "ReturnData": True}]
    chunks = mod._chunk_gmd_queries(queries)
    assert all(len(c) <= 500 for c in chunks)
    assert chunks[0][-1].get("ReturnData", True) is True
    flat_ids = [q["Id"] for c in chunks for q in c]
    assert flat_ids.index("qraw") + 1 == flat_ids.index("expr")
    # raw and its expression are in the same chunk
    assert any({"qraw", "expr"} <= {q["Id"] for q in c} for c in chunks)


def test_execute_gmd_splits_calls_and_batches_last_value_writes():
    mod, writes = _load_runner()
    cw = MagicMock()
    calls = []

    def _gmd(MetricDataQueries, **kw):
        calls.append(len(MetricDataQueries))
        return {"MetricDataResults": [
            {"Id": q["Id"], "Values": [1.5], "Timestamps": ["t"]} for q in MetricDataQueries]}
    cw.get_metric_data.side_effect = _gmd
    queries = [{"Id": f"q{i}", "ReturnData": True} for i in range(750)]
    id_map = {f"q{i}": (i, "cpuutilization") for i in range(750)}

    n = mod._execute_gmd(cw, queries, id_map, minutes=6)

    assert calls == [500, 250]
    assert n == 750
    assert len(writes["latest"]) == 1 and len(writes["latest"][0]) == 750
    assert len(writes["history"]) == 1


def test_execute_gmd_one_failed_chunk_keeps_the_other():
    mod, writes = _load_runner()
    cw = MagicMock()
    state = {"n": 0}

    def _gmd(MetricDataQueries, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("Throttling")
        return {"MetricDataResults": [
            {"Id": q["Id"], "Values": [2.0], "Timestamps": ["t"]} for q in MetricDataQueries]}
    cw.get_metric_data.side_effect = _gmd
    queries = [{"Id": f"q{i}", "ReturnData": True} for i in range(600)]
    id_map = {f"q{i}": (i, "m") for i in range(600)}
    assert mod._execute_gmd(cw, queries, id_map) == 100


def test_resource_query_includes_non_ec2_and_skips_stale_rows():
    log = []
    mod, _ = _load_runner(lambda: _Conn(log, rows=[
        {"id": 1, "resource_id": "vol-1", "resource_type": "ebs", "name": None, "region": "ap-south-1", "tags": None}]))
    grouped = mod._get_resources_for_account(7, "standard")
    sql, params = [e for e in log if e[0].startswith("SELECT id, resource_id")][0]
    assert "instance_state IS NULL OR instance_state != 'terminated'" in sql
    assert "last_seen_at IS NULL OR last_seen_at >=" in sql
    assert params == (7, mod.STALE_RESOURCE_HOURS)
    assert ("ebs", "ap-south-1") in grouped
    assert ("conn.close", None) in log


def test_resource_query_releases_connection_on_error():
    log = []
    mod, _ = _load_runner(lambda: _Conn(log, fail=True))
    try:
        mod._get_resources_for_account(7, "low")
    except RuntimeError:
        pass
    assert ("conn.close", None) in log


def test_non_aws_accounts_never_collected():
    mod, _ = _load_runner()
    seen = []
    mod._collect_account = lambda acc, tier="standard": seen.append(acc["id"])
    mod.run_metrics_collection([
        {"id": 1, "account_name": "aws", "provider": "aws"},
        {"id": 2, "account_name": "az", "provider": "azure"},
        {"id": 3, "account_name": "gcp", "provider": "gcp"},
        {"id": 4, "account_name": "legacy"},                 # no provider key -> aws
    ], tier="critical")
    assert sorted(seen) == [1, 4]


# ── scheduler ───────────────────────────────────────────────────────

class _FakeStop:
    """Stops run_loop after `iterations` sleeps."""
    def __init__(self, iterations):
        self.left = iterations
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, timeout=None):
        self.left -= 1
        if self.left <= 0:
            self.stopped = True


def _load_scheduler():
    class _NoDb:
        def cursor(self, dictionary=True):
            raise RuntimeError("no db in tests")

        def close(self):
            pass
    install_stub("app.db", get_connection=lambda: _NoDb())
    install_stub("app.collector.op_log", log_event=lambda *a, **k: None)
    install_stub("app.collector.synthetic", run_due_checks=lambda: None)
    install_stub("app.collector.maintenance", sync_maintenance_silencing=lambda: None)
    install_stub("app.llm.summarizer", refresh_ollama_model=lambda: None)
    return load_module("app/collector/scheduler.py")


def test_leadership_lost_mid_iteration_stops_remaining_tiers():
    mod = _load_scheduler()
    ran = []
    event = threading.Event()
    event.set()

    def _run_once(tier):
        ran.append(tier)
        if tier == "critical":
            event.clear()          # lock lost while critical was running
    mod.run_once = _run_once
    mod.run_discovery_once = lambda: ran.append("discovery")
    mod._stop_event = _FakeStop(5)

    mod.run_loop(event)

    assert ran == ["critical"]


def test_failing_tier_retried_at_its_cadence_not_every_tick():
    mod = _load_scheduler()
    ran = []
    clock = {"t": 1_000_000.0}

    def _run_once(tier):
        ran.append(tier)
        if tier == "standard":
            raise RuntimeError("evaluate_alerts failed")
    mod.run_once = _run_once
    mod.run_discovery_once = lambda: None
    stop = _FakeStop(3)             # 3 iterations, 120s apart: t, t+120, t+240

    def _wait(timeout=None):
        clock["t"] += 120
        _FakeStop.wait(stop, timeout)
    stop.wait = _wait
    mod._stop_event = stop
    real_time = time.time
    mod.time.time = lambda: clock["t"]
    try:
        mod.run_loop(None)
    finally:
        mod.time.time = real_time

    assert ran.count("critical") == 3
    assert ran.count("standard") == 1   # was 3 (retried every 2-min tick)


# ── metrics_writer ──────────────────────────────────────────────────

def test_writer_concurrency_is_bounded():
    state = {"cur": 0, "max": 0}
    lock = threading.Lock()

    class _WCur:
        rowcount = 1

        def executemany(self, sql, rows):
            with lock:
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
            time.sleep(0.02)
            with lock:
                state["cur"] -= 1

        def close(self):
            pass

    class _WConn:
        def cursor(self):
            return _WCur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    install_stub("app.db", get_connection=lambda: _WConn())
    mod = load_module("app/collector/metrics_writer.py")
    threads = [threading.Thread(target=mod.write_metrics_batch, args=([(1, "m", 1.0)],))
               for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 1 <= state["max"] <= 4


def test_prune_deletes_in_batches():
    counts = iter([10000, 10000, 5])
    executed = []

    class _PCur:
        rowcount = 0

        def execute(self, sql, params=None):
            executed.append((sql, params))
            _PCur.rowcount = next(counts)

        def close(self):
            pass

    class _PConn:
        def cursor(self):
            return _PCur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    install_stub("app.db", get_connection=lambda: _PConn())
    mod = load_module("app/collector/metrics_writer.py")
    assert mod.prune_metric_history(retain_days=30) == 20005
    assert len(executed) == 3
    assert "LIMIT" in executed[0][0] and executed[0][1] == (30, 10000)
