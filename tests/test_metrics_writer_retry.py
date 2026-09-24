"""
metrics_writer: batch writes retry on MySQL deadlock (1213) / lock wait
timeout (1205) instead of dropping the batch.
"""
import logging
import sys
from datetime import datetime

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app  # noqa: F401

from tests.conftest import load_module, install_stub


class _DBError(Exception):
    def __init__(self, errno, msg="db error"):
        super().__init__(f"{errno}: {msg}")
        self.errno = errno


def _setup(monkeypatch, fail_errnos):
    """fail_errnos: errno to raise on each successive attempt; once the list
    is exhausted, attempts succeed."""
    state = {"attempts": 0, "commits": [], "rollbacks": 0, "closed": 0}
    failures = list(fail_errnos)

    class _Cur:
        rowcount = 0

        def executemany(self, sql, rows):
            state["attempts"] += 1
            if failures:
                raise _DBError(failures.pop(0))
            self._rows = list(rows)
            self.rowcount = len(self._rows)

        def close(self):
            pass

    class _Conn:
        def __init__(self):
            self._cur = _Cur()

        def cursor(self):
            return self._cur

        def commit(self):
            state["commits"].append(getattr(self._cur, "_rows", []))

        def rollback(self):
            state["rollbacks"] += 1

        def close(self):
            state["closed"] += 1

    install_stub("app.db", get_connection=lambda: _Conn())
    mod = load_module("app/collector/metrics_writer.py")
    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: sleeps.append(s))
    return mod, state, sleeps


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING]


def test_latest_batch_retries_once_on_deadlock_then_succeeds(monkeypatch, caplog):
    mod, state, sleeps = _setup(monkeypatch, [1213])
    caplog.set_level(logging.DEBUG)

    mod.write_metrics_batch([(2, "b", 1.0), (1, "a", 2.0)])

    assert state["attempts"] == 2
    assert len(state["commits"]) == 1
    written = state["commits"][0]
    assert [(r[0], r[1]) for r in written] == [(1, "a"), (2, "b")]  # sorted by key
    assert sleeps == [0.2]
    assert state["closed"] == 2  # a fresh connection per attempt, all closed
    assert not _errors(caplog)
    assert len(_warnings(caplog)) == 1


def test_history_batch_retries_on_lock_wait_timeout(monkeypatch, caplog):
    mod, state, sleeps = _setup(monkeypatch, [1205])
    caplog.set_level(logging.DEBUG)
    t1, t2 = datetime(2026, 9, 24, 6, 0), datetime(2026, 9, 24, 6, 5)

    mod.write_metric_history_batch([(1, "a", 1.0, t2), (1, "a", 2.0, t1)])

    assert state["attempts"] == 2
    assert [r[3] for r in state["commits"][0]] == [t1, t2]
    assert sleeps == [0.2]
    assert not _errors(caplog)


def test_gives_up_after_three_attempts_and_logs_error(monkeypatch, caplog):
    mod, state, sleeps = _setup(monkeypatch, [1213, 1213, 1213, 1213])
    caplog.set_level(logging.DEBUG)

    mod.write_metrics_batch([(1, "a", 1.0)])

    assert state["attempts"] == 3  # bounded, no infinite loop
    assert state["commits"] == []
    assert sleeps == [0.2, 0.4]
    assert state["closed"] == 3
    assert len(_warnings(caplog)) == 2
    errs = _errors(caplog)
    assert len(errs) == 1
    assert "metrics_writer batch error" in errs[0].getMessage()


def test_non_retryable_error_is_not_retried(monkeypatch, caplog):
    mod, state, sleeps = _setup(monkeypatch, [1062])
    caplog.set_level(logging.DEBUG)

    mod.write_metric_history_batch([(1, "a", 1.0, datetime(2026, 9, 24))])

    assert state["attempts"] == 1
    assert sleeps == []
    assert len(_errors(caplog)) == 1
