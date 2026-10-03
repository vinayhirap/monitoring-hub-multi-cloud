# tests/test_anomaly_floor_and_idle_resolve.py
"""2026-09-30, two alert-noise fixes agreed with the operator.

1. ANOMALY-ONLY SIZE FLOOR. An idle EBS volume has a near-zero baseline, so the anomaly line (mean + 3 sigma)
   was ~26 operations and 57 operations in 5 minutes (0.2/s) raised a WARNING. The line is now never lower than
   threshold_defaults.ALERT_MIN_ABSOLUTE (1,000 operations / 50 MB per 5-minute period).
2. IDLE LOAD BALANCERS. ELB TargetResponseTime is only published for periods with requests, so an idle load
   balancer's alert sat in `stale` (NO DATA) for the full 72 h. If RequestCount was OBSERVED at zero for the
   whole window, the alert now closes as `no_traffic`.

The idle rule is a SQL predicate, so it is tested by running the evaluator's ACTUAL query in SQLite."""
import re
import sqlite3
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app.alert_rules  # noqa: F401,E402
import app.aws.metric_catalog_data  # noqa: F401,E402
import app.collector.polling_model  # noqa: F401,E402
from app.threshold_defaults import alert_floor, ALERT_MIN_ABSOLUTE  # noqa: E402
from tests.conftest import FakeCursor  # noqa: E402
from tests.test_baseline_and_dynamic_bounds import _load_alert_evaluator, _baseline_cursor  # noqa: E402


# ── 1. size floor ────────────────────────────────────────────────────

def test_floor_table_holds_exactly_the_agreed_metrics_and_units():
    assert ALERT_MIN_ABSOLUTE == {
        "volumereadops": 1000.0, "volumewriteops": 1000.0,
        "volumereadbytes": 50_000_000.0, "volumewritebytes": 50_000_000.0,
        "networkin": 1_000_000.0, "networkout": 1_000_000.0, "blockedrequests": 10.0,
        "httpcode_target_4xx_count": 10.0}
    assert alert_floor("VolumeReadOps") == 1000.0                 # PascalCase family (extended tier) too
    assert alert_floor("NetworkOut") == 1_000_000.0 and alert_floor("BlockedRequests") == 10.0
    assert alert_floor("requestcount") == 0.0 and alert_floor("allowedrequests") == 0.0 and alert_floor(None) == 0.0


def test_4xx_count_has_a_floor_but_server_errors_never_do():
    """2026-10-03: client-error noise is floored (10 per 5 min); 5xx must keep alerting at any count."""
    assert alert_floor("httpcode_target_4xx_count") == 10.0
    assert alert_floor("HTTPCode_Target_4XX_Count") == 10.0          # PascalCase family too
    for server_error in ("httpcode_target_5xx_count", "httpcode_elb_5xx_count", "errors5xx"):
        assert alert_floor(server_error) == 0.0


def test_4xx_count_line_is_lifted_to_the_floor_for_a_quiet_alb_and_untouched_for_a_busy_one():
    """Through the real evaluator path: a quiet ALB's baseline (mean 0.5, sigma 1.2 -> line ~4) must not
    alert on a handful of 4xx; a busy one's own line (mean 500, sigma 100) is far above the floor."""
    ev = _load_alert_evaluator()
    quiet = ev._anomaly_only_bound(_baseline_cursor(0.5, 1.2, 100), 7, "alb-quiet", "httpcode_target_4xx_count", 3.0)
    busy = ev._anomaly_only_bound(_baseline_cursor(500.0, 100.0, 100), 7, "alb-busy", "httpcode_target_4xx_count", 3.0)
    assert quiet == 10.0
    assert busy > 100.0 and busy != 10.0
    # server errors keep their small baseline-derived line -- no floor
    assert ev._anomaly_only_bound(_baseline_cursor(0.5, 1.2, 100), 7, "alb-quiet", "httpcode_target_5xx_count", 3.0) < 10.0


def test_idle_volume_line_is_the_floor_not_a_near_zero_number():
    ev = _load_alert_evaluator()
    # the real case: baseline mean ~2 operations, sigma ~7 -> old line 26 -> a reading of 57 fired
    line = ev._anomaly_only_bound(_baseline_cursor(2.0, 7.7, 100), 7, "vol-idle", "volumereadops", 3.0)
    assert line == 1000.0
    assert 57 <= line and 65 <= line                                  # both screenshot readings are now under it


def test_busy_volume_line_is_unchanged_by_the_floor():
    ev = _load_alert_evaluator()
    # mean 13,700, sigma 900: mean + 3 sigma = 16,400 ; 1.5 x mean = 20,550 -> the ratio rule wins, floor irrelevant
    assert ev._anomaly_only_bound(_baseline_cursor(13700.0, 900.0, 100), 7, "vol-busy", "volumewriteops", 3.0) == 20550.0


def test_bytes_floor_and_mixed_case_metric_names():
    ev = _load_alert_evaluator()
    assert ev._anomaly_only_bound(_baseline_cursor(1e6, 2e6, 100), 7, "v", "VolumeWriteBytes", 3.0) == 50_000_000.0
    assert ev._anomaly_only_bound(_baseline_cursor(4e7, 1e7, 100), 7, "v", "volumewritebytes", 3.0) == 7e7   # above floor


def test_metrics_without_a_floor_keep_the_old_line_and_cold_start_still_means_no_alert():
    ev = _load_alert_evaluator()
    assert ev._anomaly_only_bound(_baseline_cursor(2.0, 7.7, 100), 7, "lb", "requestcount", 3.0) == 25.1     # mean+3 sigma
    assert ev._anomaly_only_bound(_baseline_cursor(2.0, 7.7, 5), 7, "vol", "volumereadops", 3.0) is None      # < CONFIDENT_SAMPLES
    assert ev._anomaly_only_bound(_baseline_cursor(2.0, 0, 100), 7, "vol", "volumereadops", 3.0) is None      # flat line


# ── 2. idle load balancers: the real SQL, in SQLite ──────────────────

NOW = 1_000_000


def _tr(sql):
    sql = re.sub(r"DATE_SUB\(UTC_TIMESTAMP\(\), INTERVAL (\d+) MINUTE\)", lambda m: f"(UTC_TIMESTAMP() - {int(m.group(1)) * 60})", sql)
    return sql.replace("%s", "?")


def _capture_no_traffic_query():
    ev = _load_alert_evaluator()
    seen = []

    class _Cur(FakeCursor):
        def __init__(self):
            super().__init__([])

        def execute(self, sql, params=None):
            seen.append((" ".join(sql.split()), tuple(params or ())))
            self._pending = []

        def fetchall(self):
            return []

        def close(self):
            pass

    ev._auto_resolve_stale_alerts(_Cur())
    hits = [(s, p) for s, p in seen if p and p[0] == "elb" and p[1] == "responselatency"]
    assert len(hits) == 1, f"expected exactly one no_traffic query, got {len(hits)}"
    return hits[0], ev


def _world(alert, history, rtype="elb", metric="responselatency", status="active"):
    """One ELB (resources.id=1) with one alert and some requestcount history rows [(minutes_ago, value)]."""
    conn = sqlite3.connect(":memory:")
    conn.create_function("UTC_TIMESTAMP", 0, lambda: NOW)
    conn.executescript("""
        CREATE TABLE alerts (id INTEGER PRIMARY KEY, resource_id TEXT, aws_account_id INT, metric_name TEXT,
                             status TEXT, last_seen_at INT, triggered_at INT);
        CREATE TABLE resources (id INTEGER PRIMARY KEY, resource_id TEXT, aws_account_id INT, resource_type TEXT);
        CREATE TABLE metric_history (resource_id INT, metric_name TEXT, metric_value REAL, metric_timestamp INT);
    """)
    conn.execute("INSERT INTO resources VALUES (1, 'arn:lb', 10, ?)", (rtype,))
    conn.execute("INSERT INTO alerts VALUES (1, 'arn:lb', 10, ?, ?, ?, ?)",
                 (metric, status, NOW - alert["last_seen_min"] * 60, NOW - alert["last_seen_min"] * 60 - 300))
    for minutes_ago, value in history:
        conn.execute("INSERT INTO metric_history VALUES (1, 'requestcount', ?, ?)", (value, NOW - minutes_ago * 60))
    return conn


def _selected(conn):
    (sql, params), ev = _capture_no_traffic_query()
    open_in = "('active','acknowledged')"
    return [r[0] for r in conn.execute(_tr(sql.replace("{open_in}", open_in)), params)]


ZERO_HOUR = [(m, 0.0) for m in range(2, 59, 3)]          # RequestCount observed at 0 for the last hour


def test_idle_load_balancer_alert_closes_as_no_traffic():
    assert _selected(_world({"last_seen_min": 90}, ZERO_HOUR)) == [1]


def test_acknowledged_alert_is_closed_too():
    assert _selected(_world({"last_seen_min": 90}, ZERO_HOUR, status="acknowledged")) == [1]


def test_any_request_in_the_window_keeps_the_alert():
    assert _selected(_world({"last_seen_min": 90}, ZERO_HOUR + [(30, 4.0)])) == []


def test_no_observation_of_the_traffic_metric_means_not_idle():
    # nothing in requestcount history for the window: we simply cannot see this load balancer
    assert _selected(_world({"last_seen_min": 90}, [])) == []
    assert _selected(_world({"last_seen_min": 90}, [(120, 0.0)])) == []          # zeros exist, but too old


def test_recently_seen_alert_is_left_alone():
    assert _selected(_world({"last_seen_min": 30}, ZERO_HOUR)) == []


def test_only_the_configured_metric_and_resource_type_are_touched():
    assert _selected(_world({"last_seen_min": 90}, ZERO_HOUR, metric="errors5xx")) == []
    assert _selected(_world({"last_seen_min": 90}, ZERO_HOUR, rtype="rds")) == []
    assert _selected(_world({"last_seen_min": 90}, ZERO_HOUR, status="resolved")) == []


def test_reason_is_recorded_and_config_is_what_we_agreed():
    ev = _load_alert_evaluator()
    assert ev.NO_DATA_MEANS_IDLE == {("elb", "responselatency"): "requestcount"}
    assert ev.IDLE_RESOLVE_MINUTES == 60
    src = open("app/collector/alert_evaluator.py", encoding="utf-8").read()
    assert 'run("no_traffic"' in src and "no_traffic          a request-driven metric" in src


def test_a_failure_in_the_idle_rule_cannot_break_the_stale_sweep():
    """The idle rule's SQL is new; if it ever errors (schema drift, deadlock...) the rest of the sweep --
    including the pending-candidate cleanup after it -- must still run and the sweep must return normally."""
    ev = _load_alert_evaluator()
    log = []

    class _Cur(FakeCursor):
        def __init__(self):
            super().__init__([])

        def execute(self, sql, params=None):
            norm = " ".join(sql.split())
            log.append(norm)
            if params and len(params) >= 2 and params[0] == "elb" and params[1] == "responselatency":
                raise RuntimeError("1054 Unknown column (simulated)")
            self._pending = []

        def fetchall(self):
            return []

    total, by_reason = ev._auto_resolve_stale_alerts(_Cur())
    assert (total, by_reason) == (0, {})
    assert any(q.startswith("DELETE FROM alert_pending") for q in log), "cleanup after the idle rule must still run"
    # and the two rules on either side of it did run
    assert any("no_data_expired" in q or "72 HOUR" in q for q in log)
