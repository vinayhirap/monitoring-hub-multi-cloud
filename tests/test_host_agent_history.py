# tests/test_host_agent_history.py
"""
Host agent patch 0011: GET /api/host-agents/{id}/history (bucketed history + chart metadata for the same chart card the EC2
page uses) and the pure helpers in app/hostagents/history.py. The route is exercised against a small purpose-built fake
connection that answers exactly the statements the route issues and fails loudly on anything else (so "read only" is
enforced), and the aggregation SQL itself was run against a real MariaDB (see the patch notes).
"""
from datetime import timedelta
from pathlib import Path

import pytest

from tests.conftest import install_stub, load_module
from tests.hostagent_support import Env, utcnow

ROOT = Path(__file__).resolve().parent.parent
BASE = "/api/host-agents"


# ── pure helpers ─────────────────────────────────────────────────────────────

def _history():
    return load_module("app/hostagents/history.py")


def _info(contract="cpu_percent", label_key=None, label=None, unit="%", title="CPU utilization (guest OS)"):
    return {"contract": contract, "label_key": label_key, "label": label, "unit": unit, "title": title, "order": 0}


def test_units_are_the_strings_the_frontend_formatter_knows():
    h = _history()
    assert [h.chart_unit(u) for u in ("%", "bytes", "bytes/s", "seconds", "")] == [
        "Percent", "Bytes", "Bytes/Second", "Seconds", "Count"]
    assert h.chart_unit("furlongs") == "None"                      # unknown: plain number, never a crash


def test_title_carries_the_mount_or_interface():
    h = _history()
    assert h.chart_title(_info("disk_used_percent", "mount", "/", title="Disk used")) == "Disk used · /"
    assert h.chart_title(_info("net_rx_bytes_per_s", "iface", "ens5", title="Network receive")) == "Network receive · ens5"
    assert h.chart_title(_info()) == "CPU utilization (guest OS)"


def test_rows_to_points_buckets_are_utc_epoch_multiples_and_sorted():
    h = _history()
    rows = [
        {"metric_name": "m", "bucket": 5976120, "a": 40.0, "mn": 40, "mx": 40, "n": 1},
        {"metric_name": "m", "bucket": 5976119, "a": 15.0, "mn": 10, "mx": 20, "n": 2},
        {"metric_name": "m", "bucket": 5976121, "a": None, "mn": None, "mx": None, "n": 0},   # skipped, not zero
    ]
    pts = h.rows_to_points(rows, 300)["m"]
    assert [p["t"] for p in pts] == ["2026-10-24T09:55:00Z", "2026-10-24T10:00:00Z"]
    assert pts[0] == {"t": "2026-10-24T09:55:00Z", "v": 15.0, "a": 15.0, "mn": 10.0, "mx": 20.0, "n": 2}


def test_meta_has_every_key_the_chart_card_reads():
    h = _history()
    m = h.chart_meta(_info(), "host_cpu_percent", interval_seconds=60, stale_after=300, last_seen_epoch=1790000000,
                     stats=["Average", "Minimum", "Maximum", "SampleCount"])
    for key in ("title", "metric_name", "unit", "native_stat", "stats", "poll_seconds", "poll_label", "stale_after_seconds",
                "sparse", "period_seconds", "last_collected_ts", "threshold", "alert"):
        assert key in m, key
    assert m["poll_label"] == "1 min" and m["period_seconds"] == 60 and m["unit"] == "Percent"
    assert m["native_stat"] == "Average" and m["threshold"] is None and m["alert"] is None


def test_interval_label_for_non_minute_intervals():
    h = _history()
    assert h.chart_meta(_info(), "x", interval_seconds=45, stale_after=None, last_seen_epoch=None, stats=[])["poll_label"] == "45 s"
    assert h.chart_meta(_info(), "x", interval_seconds=None, stale_after=None, last_seen_epoch=None, stats=[])["poll_label"] is None


def test_sql_is_timezone_independent_and_parameterised():
    h = _history()
    sql = h.history_sql(3)
    assert "TIMESTAMPDIFF(SECOND, '1970-01-01 00:00:00', metric_timestamp)" in sql
    assert "UNIX_TIMESTAMP" not in sql                              # would depend on the session time zone
    assert "SUM(" not in sql                                        # Sum is never offered for a gauge, so it is not shipped
    assert sql.count("%s") == 3 + 3 and "IN (%s, %s, %s)" in sql
    assert "GROUP BY metric_name, bucket" in sql and "metric_value IS NOT NULL" in sql
    assert not any(w in sql.upper() for w in ("INSERT", "UPDATE", "DELETE"))


# ── the route ────────────────────────────────────────────────────────────────

class FakeDb:
    """Answers exactly the statements GET /history issues. Anything else raises."""

    def __init__(self, mod, host, resource, last_rows, bucket_rows, threshold_rows=(), alerts=(), fallback_rows=()):
        self.mod, self.host, self.resource = mod, host, resource
        self.fallback_rows = list(fallback_rows)
        self.last_rows, self.bucket_rows = last_rows, bucket_rows
        self.threshold_rows, self.alerts = list(threshold_rows), list(alerts)
        self.log = []

    def connect(self):
        return self

    def cursor(self, dictionary=True):
        return FakeCursor(self)

    def close(self):
        pass


class FakeCursor:
    def __init__(self, db):
        self.db, self._rows = db, []

    def execute(self, sql, params=()):
        m = self.db.mod
        self.db.log.append((sql, params))
        if sql == m.SQL_HOST_FOR_HISTORY:
            self._rows = [self.db.host] if self.db.host else []
        elif sql == m.SQL_METRICS_RESOURCE:
            self._rows = [self.db.resource] if self.db.resource else []
        elif sql == m.SQL_METRICS_LAST:
            self._rows = list(self.db.last_rows)
        elif sql == m.SQL_METRICS_HISTORY_LATEST:
            self._rows = list(self.db.fallback_rows)
        elif sql.startswith("SELECT metric_name, FLOOR(TIMESTAMPDIFF"):
            self._rows = list(self.db.bucket_rows)
        elif sql.startswith("SELECT LOWER(mc.metric_name) AS db_name"):
            self._rows = list(self.db.threshold_rows)
        else:
            raise AssertionError(f"unexpected SQL in GET /history: {sql[:90]}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


def _host(**kw):
    base = dict(id=1, aws_account_id=1, hostname="web-1", status="active", resource_id="host:web-1",
                last_seen_at=utcnow() - timedelta(seconds=20), config_json='{"interval_seconds": 60}')
    base.update(kw)
    return base


def _last(name, value=1.0, age=30):
    return {"metric_name": name, "metric_value": value, "metric_timestamp": utcnow() - timedelta(seconds=age)}


def _bucket(name, bucket, a):
    return {"metric_name": name, "bucket": bucket, "a": a, "mn": a, "mx": a, "n": 1}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("HOST_AGENT_ENABLED", "true")
    e = Env()
    disp = load_module("app/metric_display.py")
    install_stub("app.metric_display", METRIC_HISTORY_RETENTION_DAYS=disp.METRIC_HISTORY_RETENTION_DAYS,
                 bucket_seconds=disp.bucket_seconds, effective_hours=disp.effective_hours,
                 stats_available=disp.stats_available)
    e.eff_calls = []

    def effective_lines(cur, account_id, resource_id, row, db_name):
        e.eff_calls.append((account_id, resource_id, db_name))
        return float(row["warning_value"]), float(row["critical_value"]), "static"

    install_stub("app.metric_meta", stale_after_seconds=lambda i: 300, _effective_lines=effective_lines)
    e.alert_rows = []
    install_stub("app.alert_rules", fetch_open_alert_rows=lambda cur, accounts: list(e.alert_rows))
    return e


def _serve(env, monkeypatch, host=None, resource=None, last_rows=(), bucket_rows=(), threshold_rows=(), fallback_rows=()):
    db = FakeDb(env.mod, host, resource, last_rows, bucket_rows, threshold_rows, fallback_rows=fallback_rows)
    monkeypatch.setattr(env.mod, "get_connection", db.connect)
    return db


def _get(env, hours=None, perms=("hostagents.view",), role="viewer", accessible=None, hid=1):
    env.as_user(role, set(perms), accessible=accessible)
    return env.client.get(f"{BASE}/{hid}/history" + (f"?hours={hours}" if hours is not None else ""))


def test_returns_series_and_meta_in_chart_card_shape(env, monkeypatch):
    db = _serve(env, monkeypatch, host=_host(), resource={"id": 7, "resource_type": "host"},
                last_rows=[_last("host_load1"), _last("host_cpu_percent"), _last("disk_used_percent__boot"), _last("disk_used_percent")],
                bucket_rows=[_bucket("host_cpu_percent", 5976120, 21.5), _bucket("host_cpu_percent", 5976121, 23.0),
                             _bucket("disk_used_percent", 5976120, 24.9)])
    r = _get(env, hours=6)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["hostname"] == "web-1" and b["resource_type"] == "host" and b["shared_series"] is False
    assert b["requested_hours"] == 6 and b["effective_hours"] == 6 and b["bucket_secs"] == 60
    assert b["retention_days"] == 30 and b["interval_seconds"] == 60
    # contract order (cpu, load, ..., disk), then label order; titles carry the mount
    assert [s["name"] for s in b["series"]] == ["host_cpu_percent", "host_load1", "disk_used_percent", "disk_used_percent__boot"]
    cpu = b["series"][0]
    assert cpu["unit"] == "Percent" and [p["v"] for p in cpu["data"]] == [21.5, 23.0]
    assert b["series"][1]["unit"] == "Count" and b["series"][1]["data"] == []                  # known series, no point in the window
    assert b["series"][3]["title"] == "Disk used · boot"
    meta = b["meta"]["host_cpu_percent"]
    assert meta["poll_label"] == "1 min" and meta["stale_after_seconds"] == 300 and meta["stats"][0] == "Average"
    assert meta["threshold"] is None and meta["alert"] is None
    assert 0 <= meta["last_collected_ts"] and abs(meta["last_collected_ts"] - int(utcnow().timestamp())) < 120
    # the aggregation ran once, with (bucket, resource pk, cutoff, *names)
    agg = [p for s, p in db.log if s.startswith("SELECT metric_name, FLOOR")]
    assert len(agg) == 1 and agg[0][0] == 60 and agg[0][1] == 7 and agg[0][3:] == (
        "host_cpu_percent", "host_load1", "disk_used_percent", "disk_used_percent__boot")
    assert abs((utcnow() - agg[0][2]).total_seconds() - 6 * 3600) < 120
    assert not any(w in s.upper() for s, _ in db.log for w in ("INSERT", "UPDATE", "DELETE"))   # read only


def test_range_is_capped_at_retention_like_every_other_chart(env, monkeypatch):
    _serve(env, monkeypatch, host=_host(), resource={"id": 7, "resource_type": "host"}, last_rows=[_last("host_cpu_percent")])
    b = _get(env, hours=720).json()
    assert (b["requested_hours"], b["effective_hours"], b["bucket_secs"]) == (720, 720, 10800)
    b = _get(env, hours=5000).json()
    assert (b["requested_hours"], b["effective_hours"]) == (5000, 720)                       # the toolbar shows the 30-day note


@pytest.mark.parametrize("bad", ["0", "-3", "abc", "9000"])
def test_hours_is_validated(env, monkeypatch, bad):
    _serve(env, monkeypatch, host=_host(), resource={"id": 7, "resource_type": "host"})
    env.as_user("viewer", {"hostagents.view"})
    assert env.client.get(f"{BASE}/1/history?hours={bad}").status_code == 422


def test_threshold_line_and_open_alert_only_on_the_matching_series(env, monkeypatch):
    _serve(env, monkeypatch, host=_host(), resource={"id": 7, "resource_type": "host"},
           last_rows=[_last("host_cpu_percent"), _last("host_swap_used_percent")],
           threshold_rows=[{"db_name": "host_cpu_percent", "unit": "Percent", "warning_value": 85, "critical_value": 95,
                            "comparison": ">", "use_dynamic": 0, "dynamic_k": None}])
    env.alert_rows = [
        {"resource_id": "host:web-1", "metric_name": "host_cpu_percent", "severity": "warning", "state": "acknowledged"},
        {"resource_id": "host:web-1", "metric_name": "host_cpu_percent", "severity": "critical", "state": "firing"},
        {"resource_id": "i-someone-else", "metric_name": "host_swap_used_percent", "severity": "critical", "state": "firing"}]
    meta = _get(env).json()["meta"]
    assert meta["host_cpu_percent"]["threshold"] == {"warning": 85.0, "critical": 95.0, "comparison": ">", "mode": "static",
                                                     "static_warning": 85.0, "static_critical": 95.0}
    assert meta["host_cpu_percent"]["alert"] == {"severity": "CRITICAL", "state": "firing"}   # firing outranks acknowledged
    assert meta["host_swap_used_percent"]["threshold"] is None
    assert meta["host_swap_used_percent"]["alert"] is None                                    # another resource's alert is not shown
    assert env.eff_calls == [(1, "host:web-1", "host_cpu_percent")]                           # same resolver the EC2 charts use


def test_threshold_failure_never_fails_the_page(env, monkeypatch):
    _serve(env, monkeypatch, host=_host(), resource={"id": 7, "resource_type": "host"}, last_rows=[_last("host_cpu_percent")],
           threshold_rows=[{"db_name": "host_cpu_percent", "unit": "Percent", "warning_value": 85, "critical_value": 95,
                            "comparison": ">", "use_dynamic": 0, "dynamic_k": None}])

    def boom(*a, **k):
        raise RuntimeError("baseline table missing")

    install_stub("app.metric_meta", stale_after_seconds=lambda i: 300, _effective_lines=boom)
    r = _get(env)
    assert r.status_code == 200 and r.json()["meta"]["host_cpu_percent"]["threshold"] is None


def test_ec2_attached_host_is_flagged_and_only_agent_series_are_charted(env, monkeypatch):
    db = _serve(env, monkeypatch, host=_host(resource_id="i-0abc"), resource={"id": 9, "resource_type": "ec2"},
                last_rows=[_last("cpuutilization"), _last("networkin"), _last("mem_used_percent"), _last("host_load1")])
    b = _get(env).json()
    assert b["resource_type"] == "ec2" and b["shared_series"] is True
    assert [s["name"] for s in b["series"]] == ["host_load1", "mem_used_percent"]            # CloudWatch-only names never charted
    agg = [p for s, p in db.log if s.startswith("SELECT metric_name, FLOOR")][0]
    assert "cpuutilization" not in agg and "networkin" not in agg


def test_series_discovery_falls_back_to_recent_history_like_the_metrics_table(env, monkeypatch):
    db = _serve(env, monkeypatch, host=_host(), resource={"id": 7, "resource_type": "host"}, last_rows=[],
                fallback_rows=[_last("host_cpu_percent")], bucket_rows=[_bucket("host_cpu_percent", 5976120, 9.0)])
    b = _get(env).json()
    assert [s["name"] for s in b["series"]] == ["host_cpu_percent"] and b["series"][0]["data"][0]["v"] == 9.0
    assert any(sql == env.mod.SQL_METRICS_HISTORY_LATEST for sql, _ in db.log)


def test_uptime_is_stored_but_not_charted(env, monkeypatch):
    db = _serve(env, monkeypatch, host=_host(), resource={"id": 7, "resource_type": "host"},
                last_rows=[_last("host_uptime_seconds", 56000), _last("host_cpu_percent")])
    b = _get(env).json()
    assert [s["name"] for s in b["series"]] == ["host_cpu_percent"] and "host_uptime_seconds" not in b["meta"]
    agg = [p for s, p in db.log if s.startswith("SELECT metric_name, FLOOR")][0]
    assert "host_uptime_seconds" not in agg                                                  # and is not even queried


def test_host_with_no_resource_yet_returns_empty_not_an_error(env, monkeypatch):
    _serve(env, monkeypatch, host=_host(resource_id=None))
    b = _get(env).json()
    assert b["series"] == [] and b["meta"] == {} and b["resource_type"] is None


def test_offline_host_shows_its_last_data_and_old_report_time(env, monkeypatch):
    _serve(env, monkeypatch, host=_host(status="active", last_seen_at=utcnow() - timedelta(hours=5)),
           resource={"id": 7, "resource_type": "host"}, last_rows=[_last("host_cpu_percent", age=18000)],
           bucket_rows=[_bucket("host_cpu_percent", 5970000, 12.0)])
    b = _get(env, hours=24).json()
    assert b["series"][0]["data"][0]["v"] == 12.0                                             # nothing is invented or extended
    assert int(utcnow().timestamp()) - b["meta"]["host_cpu_percent"]["last_collected_ts"] > 4 * 3600


def test_not_found_out_of_scope_and_permissions(env, monkeypatch):
    _serve(env, monkeypatch, host=None)
    assert _get(env).status_code == 404
    _serve(env, monkeypatch, host=_host(aws_account_id=2), resource={"id": 7, "resource_type": "host"})
    assert _get(env, accessible={1}).status_code == 404                                       # exists, but not in this user's scope
    assert _get(env, accessible={1, 2}).status_code == 200
    assert _get(env, perms=()).status_code == 403
    env.user = None
    assert env.client.get(f"{BASE}/1/history").status_code == 401


def test_route_is_declared_with_the_view_permission_only():
    src = (ROOT / "app/api/host_agents.py").read_text()
    i = src.index('@router.get("/{host_id}/history")')
    assert 'require_permission("hostagents.view")' in src[i:i + 400] and "hostagents.manage" not in src[i:i + 400]
