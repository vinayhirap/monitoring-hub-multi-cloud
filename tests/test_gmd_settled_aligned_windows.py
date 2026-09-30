# tests/test_gmd_settled_aligned_windows.py
"""Prod 2026-09-29 (i-06e979f9d79014edf networkout): with end=now the same
5-minute CloudWatch datapoint was stored under a different timestamp on every
run (16:24 and 16:25 both held the 16:25 value), and the still-filling newest
window (SampleCount 1) was stored as final and frozen by INSERT IGNORE. For
Period-300 (EC2/EBS basic monitoring) metrics the window end must be floored
to a 300 s boundary after a settle grace; Period-60 metrics keep end=now."""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.test_gmd_period_matches_native_resolution import _load_runner  # noqa: E402
from tests.conftest import load_module  # noqa: E402

UTC = timezone.utc


def _mod():
    return _load_runner()


def test_end_is_floored_to_a_300s_boundary_after_the_grace():
    mod = _mod()
    g = mod.SETTLE_GRACE_SECONDS
    assert g >= 120
    # 16:52:30 - grace -> latest boundary that is >= grace old
    now = datetime(2026, 9, 29, 16, 52, 30, tzinfo=UTC)
    end = mod._align_window_end(now, 300)
    assert end.minute % 5 == 0 and end.second == 0
    assert end <= now - timedelta(seconds=g)
    assert now - timedelta(seconds=g) - end < timedelta(seconds=300)


def test_every_run_within_a_5_min_span_lands_on_canonical_boundaries():
    mod = _mod()
    base = datetime(2026, 9, 29, 16, 0, 0, tzinfo=UTC)
    for s in range(0, 3600, 7):
        end = mod._align_window_end(base + timedelta(seconds=s), 300)
        assert end.minute % 5 == 0 and end.second == 0 and end.microsecond == 0


def test_newest_window_has_closed_and_settled_before_it_is_requested():
    mod = _mod()
    for s in range(0, 900, 13):
        now = datetime(2026, 9, 29, 16, 0, 0, tzinfo=UTC) + timedelta(seconds=s)
        end = mod._align_window_end(now, 300)
        # newest window in the response is [end-300, end): closed >= grace ago
        assert now - end >= timedelta(seconds=mod.SETTLE_GRACE_SECONDS)


class _CW:
    def __init__(self):
        self.calls = []

    def get_metric_data(self, MetricDataQueries, StartTime, EndTime, **kw):
        self.calls.append((StartTime, EndTime))
        return {"MetricDataResults": [{"Id": q["Id"], "Values": [], "Timestamps": []}
                                      for q in MetricDataQueries]}


def _runner_with_writes(history):
    import sys as _s
    mod = _mod()
    mod.write_metrics_batch = lambda rows: None
    mod.write_metric_history_batch = lambda rows, **kw: history.extend(rows)
    return mod


def test_aligned_call_sends_aligned_start_and_end():
    hist = []
    mod = _runner_with_writes(hist)
    cw = _CW()
    mod._execute_gmd(cw, [{"Id": "q0", "MetricStat": {}}], {"q0": (1, "cpuutilization", "Average")},
                     minutes=15, align_period=300)
    start, end = cw.calls[0]
    assert end.minute % 5 == 0 and end.second == 0
    assert end - start == timedelta(minutes=15)


def test_unaligned_default_is_unchanged_end_is_now():
    hist = []
    mod = _runner_with_writes(hist)
    cw = _CW()
    before = datetime.now(UTC)
    mod._execute_gmd(cw, [{"Id": "q0", "MetricStat": {}}], {"q0": (1, "m", "Average")}, minutes=6)
    _s, end = cw.calls[0]
    assert before <= end <= datetime.now(UTC)


def test_sum_zero_fill_row_uses_the_last_bucket_start_not_the_next_bucket():
    # A zero row stamped at `end` would occupy the (resource, metric, ts) key
    # of the NEXT real bucket and INSERT IGNORE would then discard its value.
    hist = []
    mod = _runner_with_writes(hist)
    cw = _CW()
    mod._execute_gmd(cw, [{"Id": "q0", "MetricStat": {}}], {"q0": (9, "volumereadops", "Sum")},
                     minutes=25, align_period=300)
    _start, end = cw.calls[0]
    assert hist and hist[0][3] == end - timedelta(seconds=300)
    assert hist[0][3].minute % 5 == 0


def test_run_gmd_aligns_only_single_period_batches():
    seen = []
    mod = _mod()
    mod._execute_gmd = lambda cw, q, m, minutes=5, align_period=None: seen.append(align_period) or 0
    ec2 = [("CPUUtilization", "cpuutilization", "Average", "AWS/EC2", 300)]
    rds = [("CPUUtilization", "cpuutilization", "Average", "AWS/RDS", 60)]
    mixed = ec2 + rds
    res_ec2 = [{"id": 1, "resource_type": "ec2", "resource_id": "i-1", "name": "x", "tags": "{}"}]
    res_rds = [{"id": 2, "resource_type": "rds", "resource_id": "db-1", "name": "x", "tags": "{}"}]
    mod._run_gmd(None, res_ec2, ec2, minutes=15)
    mod._run_gmd(None, res_rds, rds, minutes=6)
    mod._run_gmd(None, res_ec2, mixed, minutes=15)
    # 1-min batches (RDS/ELB/Lambda) now align to 60 too; only MIXED batches keep end=now
    assert seen == [300, 60, None]


def test_one_minute_windows_end_on_settled_minute_boundary():
    """ALB/RDS/Lambda (Period 60): the still-filling newest minute must not be requested."""
    mod = _mod()
    now = datetime(2026, 9, 30, 10, 42, 47, tzinfo=timezone.utc)
    end = mod._align_window_end(now, 60, mod._grace_for(60))
    assert end == datetime(2026, 9, 30, 10, 40, 0, tzinfo=timezone.utc)
    assert (now - end).total_seconds() >= mod.SETTLE_GRACE_1MIN_SECONDS


def test_history_rows_are_written_with_overwrite():
    """A bucket first stored while filling must be corrected by a later poll."""
    calls = []
    mod = _mod()
    mod.write_metrics_batch = lambda rows: None
    mod.write_metric_history_batch = lambda rows, **kw: calls.append(kw)
    cw = _CW()
    mod._execute_gmd(cw, [{"Id": "q0", "MetricStat": {}}], {"q0": (1, "requestcount", "Sum")},
                     minutes=6, align_period=60)
    assert calls == [{"overwrite": True}]
