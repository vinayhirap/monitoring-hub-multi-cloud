"""Standard (5-min) tier is phase-locked to CloudWatch's window boundary + settle grace.

Pure-function test: the helper is lifted out of scheduler.py's source, so importing the
scheduler (and with it the DB driver / AWS SDK) is not needed."""
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..", "app", "collector")
_SCHED = open(os.path.join(_ROOT, "scheduler.py"), encoding="utf-8").read()
_RUNNER = open(os.path.join(_ROOT, "metrics", "runner.py"), encoding="utf-8").read()

_ns = {"STANDARD_INTERVAL": int(re.search(r"^STANDARD_INTERVAL\s*=\s*(\d+)", _SCHED, re.M).group(1))}
exec(_SCHED[_SCHED.index("STANDARD_PHASE_SECONDS ="):_SCHED.index("class _LeadershipLost")], _ns)  # noqa: S102
_due = _ns["_standard_due_at"]
INTERVAL = _ns["STANDARD_INTERVAL"]
PHASE = _ns["STANDARD_PHASE_SECONDS"]
MIN_GAP = _ns["STANDARD_MIN_GAP_SECONDS"]
GRACE = int(re.search(r"^SETTLE_GRACE_SECONDS\s*=\s*(\d+)", _RUNNER, re.M).group(1))


def test_never_run_is_due_now():
    assert _due(0) == 0.0


def test_due_points_sit_on_the_phase_grid():
    for last in (1_000_000.0, 1_000_017.3, 1_000_123.0, 1_000_299.9):
        assert _due(last) % INTERVAL == PHASE


def test_phase_is_not_earlier_than_the_settle_grace():
    # running before boundary + grace would read the previous window
    assert PHASE >= GRACE


def test_one_run_per_slot_never_a_burst():
    last = t = 1_000_000.0
    end = t + 3600
    runs = 0
    while t < end:
        if t >= _due(last):
            last, runs = t, runs + 1
        t += 1
    assert 11 <= runs <= 13


def test_min_gap_after_a_late_run():
    last = 1_000_000.0 + PHASE + 120          # ran 2 min late
    assert _due(last) - last >= MIN_GAP
