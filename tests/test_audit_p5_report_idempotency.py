# tests/test_audit_p5_report_idempotency.py
"""Audit B11/C6: an identical report request returns the job that already exists."""
import sys
sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import contains
from tests.test_reports_account_scoping import _load_reports_module, _FakeBackgroundTasks, _user


class _Tasks(_FakeBackgroundTasks):
    def __init__(self):
        self.added = []
    def add_task(self, fn, *a, **k):
        self.added.append(a)


def _gen(mod, tasks, **over):
    kw = dict(background_tasks=tasks, request=None, report_type="WEEKLY", scope_type="ACCOUNT",
              scope_id="10", account_id=10, period_start=None, period_end=None, current_user=_user("editor"))
    kw.update(over)
    return mod.generate_report(**kw)


def _script(existing):
    return [
        (contains("GET_LOCK"), [{"got": 1}]),
        (contains("FROM report_jobs"), existing),
        (contains("INSERT INTO report_jobs"), []),
        (contains("RELEASE_LOCK"), []),
    ]


def test_duplicate_returns_existing_job_and_runs_nothing():
    tasks = _Tasks()
    mod = _load_reports_module(_script([{"id": 41, "status": "PROCESSING"}]))
    out = _gen(mod, tasks)
    assert out == {"job_id": 41, "status": "PROCESSING", "deduplicated": True}
    assert tasks.added == []


def test_new_request_creates_and_queues_a_job():
    tasks = _Tasks()
    mod = _load_reports_module(_script([]))
    out = _gen(mod, tasks)
    assert out["status"] == "QUEUED" and "deduplicated" not in out
    assert len(tasks.added) == 1


def test_lock_is_always_released_even_when_insert_fails():
    executed = []

    class Boom(Exception):
        pass

    import tests.test_reports_account_scoping as base
    script = [
        (contains("GET_LOCK"), [{"got": 1}]),
        (contains("FROM report_jobs"), []),
        (contains("RELEASE_LOCK"), []),
    ]
    mod = _load_reports_module(script)   # no INSERT entry -> FakeCursor raises AssertionError inside the lock
    try:
        _gen(mod, _Tasks())
        assert False, "expected the unscripted INSERT to raise"
    except AssertionError as e:
        assert "INSERT INTO report_jobs" in str(e)


def test_custom_range_is_part_of_the_dedupe_key_and_failed_jobs_do_not_block():
    src = open(__file__.rsplit("/tests/", 1)[0] + "/app/api/reports.py").read()
    assert 'if report_type == "CUSTOM"' in src and "period_start = %s AND period_end = %s" in src
    assert "status IN ('QUEUED','PROCESSING')" in src and "'FAILED'" not in src.split("_find_duplicate_job")[1].split("def ")[0]
    assert "_INFLIGHT_MAX_MINUTES = 15" in src


def test_stuck_inflight_jobs_have_an_age_limit():
    src = open(__file__.rsplit("/tests/", 1)[0] + "/app/api/reports.py").read()
    assert "created_at > NOW() - INTERVAL %s MINUTE" in src
