# tests/test_audit_p11_setup_status.py
"""Audit B9: first-run checklist data."""
import sys
from pathlib import Path

import app          # noqa: F401
import app.auth     # noqa: F401

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class _Cur:
    def __init__(self, counts, broken=()):
        self.counts, self.broken, self._n = counts, set(broken), 0
    def execute(self, sql, params=None):
        table = sql.split("FROM ")[1].split(" ")[0]
        if table in self.broken:
            raise RuntimeError("no such table")
        self._n = self.counts.get(table, 0)
    def fetchone(self):
        return {"n": self._n}


class _Conn:
    def __init__(self, cur):
        self.cur, self.closed = cur, False
    def cursor(self, dictionary=False):
        return self.cur
    def close(self):
        self.closed = True


def _mod(cur):
    conn = _Conn(cur)
    install_stub("app.db", get_connection=lambda: conn)
    install_stub("app.auth.deps", get_current_user=lambda request=None: {"id": 1})
    return load_module("app/api/setup_status.py"), conn


def test_counts_become_done_flags():
    m, conn = _mod(_Cur({"aws_accounts": 2, "notification_channels": 0, "synthetic_checks": 1}))
    out = m.setup_status(current_user={"id": 1})
    flags = {s["key"]: s["done"] for s in out["steps"]}
    assert flags == {"accounts": True, "notifications": False, "synthetic": True, "slo": False,
                     "status_page": False, "escalation": False}
    assert out["done"] == 2 and out["total"] == 6 and conn.closed
    assert all(s["hint"] for s in out["steps"])


def test_a_missing_table_counts_as_not_set_up_instead_of_a_500():
    m, _ = _mod(_Cur({"aws_accounts": 1}, broken={"slo_definitions", "status_page_components"}))
    out = m.setup_status(current_user={"id": 1})
    assert {s["key"]: s["done"] for s in out["steps"]}["slo"] is False


def test_endpoint_is_authenticated_and_returns_counts_only():
    main = (ROOT / "app/main.py").read_text()
    assert "setup_router, " in main and "dependencies=_auth_dep" in main.split("setup_router,")[1].split("\n")[0]
    src = (ROOT / "app/api/setup_status.py").read_text()
    assert "SELECT COUNT(*)" in src and "SELECT *" not in src          # no row data leaves this endpoint
