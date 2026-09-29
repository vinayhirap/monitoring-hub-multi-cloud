# tests/test_verify_cloudwatch_parity.py -- pure comparison logic of the
# read-only CloudWatch/DB/API parity tool (no AWS or DB access).
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tools.verify_cloudwatch_parity import classify, _last_point  # noqa: E402

T = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)


def test_same_window_within_tolerance_is_ok():
    assert classify((65.2, T), (65.4, T + timedelta(minutes=1)), "percent") == "OK"


def test_same_window_beyond_tolerance_is_diff():
    assert classify((83.3, T), (65.2, T), "percent") == "DIFF"


def test_different_windows_are_stale_not_diff():
    assert classify((10.0, T), (90.0, T + timedelta(minutes=30)), "percent") == "STALE"


def test_missing_side_is_missing():
    assert classify((1.0, T), None, "percent") == "MISSING"
    assert classify(None, None, "bytes") == "MISSING"


def test_bytes_use_relative_tolerance():
    assert classify((27000.0, T), (27500.0, T), "bytes") == "OK"
    assert classify((27000.0, T), (40000.0, T), "bytes") == "DIFF"


def test_last_point_picks_newest_valid():
    s = [{"t": "2026-09-29T13:55:00+00:00", "v": 1.0}, {"t": "2026-09-29T14:00:00Z", "v": 2.5}]
    assert _last_point(s)[0] == 2.5 and _last_point([]) is None


def test_bucket_window_is_exactly_one_period_from_ts():
    from tools.verify_cloudwatch_parity import bucket_window, _queries, _spec
    s, e = bucket_window(T, 300)
    assert s == T and e - s == timedelta(seconds=300)
    q = _queries(_spec("CWAgent", "LogicalDisk % Free Space", [], 60, invert=True), "q")
    assert [x["Id"] for x in q] == ["qraw", "q"] and q[1]["Expression"] == "100 - qraw"
    assert q[0]["ReturnData"] is False
    assert len(_queries(_spec("AWS/EC2", "CPUUtilization", [], 300), "q")) == 1


def test_db_last_offset_is_parameterised():
    from tools.verify_cloudwatch_parity import _db_last
    seen = []

    class C:
        def execute(self, sql, params):
            seen.append((" ".join(sql.split()), params))
        def fetchone(self):
            return {"metric_value": 1.0, "metric_timestamp": T}

    assert _db_last(C(), "metric_history", 5, "cpuutilization", back=2)[0] == 1.0
    sql, params = seen[0]
    assert "OFFSET %s" in sql and params == (5, "cpuutilization", 2)
