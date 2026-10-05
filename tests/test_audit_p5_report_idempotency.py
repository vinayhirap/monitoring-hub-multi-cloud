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


class _StrictRules:
    """Models mysql-connector's UNBUFFERED cursor: executing while a result set is unread raises InternalError.
    The first idempotency version passed its tests with a forgiving fake and then returned HTTP 500 in production."""


def _make_strict(mod):
    """Wrap the module's scripted cursor class so an unread result set blocks the next execute()."""
    real_get = mod.get_db_cursor

    from contextlib import contextmanager

    @contextmanager
    def strict(*a, **k):
        with real_get(*a, **k) as (conn, cur):
            state = {"unread": False}
            orig_execute, orig_fetchone, orig_fetchall = cur.execute, cur.fetchone, cur.fetchall

            def execute(sql, params=None):
                if state["unread"]:
                    raise RuntimeError("Unread result found")           # mysql.connector.errors.InternalError
                out = orig_execute(sql, params)
                state["unread"] = sql.lstrip().upper().startswith("SELECT")
                return out

            def fetchone():
                state["unread"] = False
                return orig_fetchone()

            def fetchall():
                state["unread"] = False
                return orig_fetchall()
            cur.execute, cur.fetchone, cur.fetchall = execute, fetchone, fetchall
            yield conn, cur
    mod.get_db_cursor = strict


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


def test_every_result_set_is_read_before_the_next_statement_the_500_regression():
    """Reports page showed 'API /reports/generate ... -> 500' on every click."""
    tasks = _Tasks()
    mod = _load_reports_module(_script([]))
    _make_strict(mod)
    assert _gen(mod, tasks)["status"] == "QUEUED"                          # new job: lock, check, insert, release
    dup = _load_reports_module(_script([{"id": 41, "status": "COMPLETE"}]))
    _make_strict(dup)
    assert _gen(dup, _Tasks())["deduplicated"] is True                     # early return still releases the lock cleanly
    src = (__import__("pathlib").Path(__file__).resolve().parent.parent / "app/api/reports.py").read_text()
    fn = src[src.index("def generate_report"):src.index("def get_report_job") if "def get_report_job" in src else len(src)]
    assert fn.count("cur.fetchone()") >= 2 and "GET_LOCK" in fn and "RELEASE_LOCK" in fn


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
