# tests/test_environment_sync_write_guard.py
"""env_sync writes: ascending id (one lock order for every bulk writer of resources rows) and never over a manual
choice saved since the rows were read (UPDATE ... AND env_override <=> %s)."""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app  # noqa: F401

from tests.conftest import load_module, install_stub


def _rows():
    base = {"aws_account_id": 1, "resource_type": "ec2", "tags": None, "env_resolved": None, "env_source": None,
            "env_reason": None, "tag_state": None, "default_env": None}
    return [dict(base, id=30, resource_id="i-30", name="web-prod-30", env_override=None),
            dict(base, id=10, resource_id="i-10", name="web-dev-10", env_override="uat"),
            dict(base, id=20, resource_id="i-20", name="web-uat-20", env_override=None)]


def _run(monkeypatch):
    written = []

    class RCur:
        def __init__(self):
            self._last = ""

        def execute(self, sql, params=None):
            self._last = " ".join(sql.split())

        def fetchall(self):
            return _rows() if "FROM resources r" in self._last else []

        def close(self):
            pass

    class WCur:
        rowcount = 0

        def executemany(self, sql, chunk):
            written.append((" ".join(sql.split()), list(chunk)))
            self.rowcount = len(chunk)

        def close(self):
            pass

    class Conn:
        def cursor(self, dictionary=False):
            return RCur() if dictionary else WCur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    install_stub("app.db", get_connection=lambda: Conn())
    mod = load_module("app/collector/env_sync.py")
    return mod.sync_resource_environments(), written


def test_updates_are_written_in_ascending_id_order(monkeypatch):
    rep, written = _run(monkeypatch)
    ids = [u[4] for u in written[0][1]]
    assert ids == sorted(ids) == [10, 20, 30] and rep["applied"] == 3


def test_update_is_guarded_by_the_override_that_was_read(monkeypatch):
    _, written = _run(monkeypatch)
    sql, chunk = written[0]
    assert sql.endswith("WHERE id = %s AND env_override <=> %s")
    assert {u[4]: u[5] for u in chunk} == {10: "uat", 20: None, 30: None}
