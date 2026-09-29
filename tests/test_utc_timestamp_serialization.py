# tests/test_utc_timestamp_serialization.py
"""
2026-09-29: MySQL DATETIME columns carry no timezone info, so every
datetime this app reads back from the DB is naive -- even though every
value it ever WRITES there is a genuine UTC wall-clock reading. A bare
.isoformat() on a naive datetime produces a string with no trailing 'Z'
or offset, and per the ECMAScript Date-parsing spec, an ISO date-TIME
string with no timezone designator is parsed as the browser's LOCAL
time, not UTC (only a bare DATE string defaults to UTC -- one of the
best-known Date-parsing footguns).

Found live on prod 2026-09-29: CloudOps's own instance-detail chart
showed its newest CPUUtilization point as "11:02" while the topbar
correctly said IST and the real local time was 16:45 -- the value shown
was the metric's raw UTC clock reading (11:02 UTC), silently misread by
the browser as if it were already 11:02 IST. The frontend's timezone
code (AccountDetail.jsx's toLocaleTimeString(...,{timeZone: ianaName}))
was working correctly throughout; the bug was entirely upstream, in the
backend sending an unmarked string in the first place.

app/utils/time_json.py centralizes the fix app/api/status_page.py had
already hand-written correctly for its own fields (str(dt) + "Z") --
these tests both pin the helper's own behavior and prove it's actually
wired into the one function that produced the exact bug seen live
(collector_direct.py's chart-series endpoint; see the corrected
assertions in test_collector_direct.py's
test_chart_range_resolves_ec2_by_resource_id for the same proof from
the other direction).
"""
import datetime
import sys

from tests.conftest import load_module


def _load_time_json():
    return load_module("app/utils/time_json.py")


def test_naive_datetime_gets_an_explicit_z():
    """The exact case that broke live: a MySQL-sourced, UTC-intended,
    naive datetime must never be serialized without a timezone marker."""
    mod = _load_time_json()
    naive = datetime.datetime(2026, 9, 29, 11, 2, 0)
    assert mod.to_utc_iso(naive) == "2026-09-29T11:02:00Z"


def test_aware_datetime_is_converted_to_utc_not_reinterpreted_as_naive():
    """A genuinely tz-aware datetime (e.g. anything boto3 returns for a
    CloudWatch timestamp) must be correctly converted, not blindly
    stamped with 'Z' as if it were naive-UTC already."""
    mod = _load_time_json()
    ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    aware_ist = datetime.datetime(2026, 9, 29, 16, 32, 0, tzinfo=ist)
    assert mod.to_utc_iso(aware_ist) == "2026-09-29T11:02:00Z"


def test_none_passes_through_as_none():
    """Callers (e.g. resolved_at on an still-open alert) rely on None
    staying None, not becoming the string 'None' or raising."""
    mod = _load_time_json()
    assert mod.to_utc_iso(None) is None


def test_bare_date_has_no_timezone_ambiguity():
    mod = _load_time_json()
    d = datetime.date(2026, 9, 29)
    assert mod.to_utc_iso(d) == "2026-09-29"


def test_metric_history_chart_series_actually_uses_the_shared_helper():
    """Proves the wiring, not just the helper: loads the REAL
    collector_direct.py (the file that produced the live bug) with the
    REAL time_json module registered under its actual import path, and
    confirms a naive metric_timestamp comes out Z-suffixed end to end --
    the same shape test_collector_direct.py's own tests now pin."""
    sys.modules["app.utils.time_json"] = load_module("app/utils/time_json.py")
    from tests.conftest import install_stub

    class _Cursor:
        def __init__(self, rows):
            self._rows = rows
        def execute(self, sql, params=None):
            pass
        def fetchone(self):
            return {"id": 501}
        def fetchall(self):
            return self._rows
        def close(self):
            pass

    class _Conn:
        def __init__(self, rows):
            self._cursor = _Cursor(rows)
        def cursor(self, dictionary=True):
            return self._cursor
        def close(self):
            pass

    rows = [{"metric_value": 6.5, "metric_timestamp": datetime.datetime(2026, 9, 29, 11, 2, 0)}]
    install_stub("app.db", get_connection=lambda: _Conn(rows))
    install_stub("app.clients.vm_client", vm_query=lambda p: None, vm_query_all=lambda p, d: {})
    install_stub("app.collector.disk_mounts", all_cwagent_disk_dims=lambda cw, iid: [])
    install_stub("app.aws.boto_config", STANDARD_RETRY=None, CONCURRENT_CLIENT_RETRY=None)
    mod = load_module("app/aws/collector_direct.py")

    result = mod._metric_history_query_range(
        "ec2", "i-0424cb66e22e05a21", "cpuutilization",
        datetime.datetime(2026, 9, 29, 10), datetime.datetime(2026, 9, 29, 12),
        account_id=1,
    )
    assert result == [{"t": "2026-09-29T11:02:00Z", "v": 6.5}]
