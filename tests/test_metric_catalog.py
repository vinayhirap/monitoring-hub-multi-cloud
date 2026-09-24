# tests/test_metric_catalog.py
"""
Covers app/api/metric_catalog.py's audit-b17 fixes:

  1. The 10 pre-scan-flagged functions that opened a DB connection with
     no try/finally now go through app.db.get_db_cursor() instead of a
     raw get_connection()/cur.close()/conn.close() sequence -- verified
     both by source inspection (no function in this file calls
     get_connection() directly any more) and functionally (an
     exception raised partway through a request still closes the
     connection, via a get_db_cursor() stand-in that mirrors the real
     one's try/finally semantics).
  2. generate_yace_config()'s new _yaml_comment_safe() helper strips
     newlines out of admin-editable account_name/account_id/
     default_region fields before they're interpolated into the
     config's leading `#` comment lines, closing off a same-privilege
     comment-injection point that yaml.dump() itself was never exposed
     to (role_arn/external_id go through yaml.dump(), not an f-string).
"""
import inspect
import sys
from contextlib import contextmanager

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub


# ── stand-ins ──────────────────────────────────────────────────────────

class _FakeCursor:
    """Records every execute() call; answers from a simple script list
    of (predicate, rows) pairs, first match wins, like conftest's
    FakeCursor but also tracking rowcount for the enable/insert paths."""

    def __init__(self, script=None):
        self.script = script or []
        self.calls = []
        self._pending = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        for predicate, rows in self.script:
            if predicate(normalized, params or ()):
                self._pending = rows
                return
        self._pending = []

    def fetchone(self):
        return self._pending[0] if self._pending else None

    def fetchall(self):
        return self._pending or []

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self, dictionary=False):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _real_get_db_cursor_stub(conn):
    """
    A stand-in for app.db.get_db_cursor that copies the REAL function's
    try/finally/rollback semantics exactly (see app/db.py) but hands
    back our _FakeConn/_FakeCursor instead of a pooled connection --
    so a test exercising an exception path is exercising the actual
    cleanup logic, not a no-op fake.
    """
    @contextmanager
    def get_db_cursor(dictionary: bool = False, commit: bool = True):
        cur = conn.cursor(dictionary=dictionary)
        try:
            yield conn, cur
            if commit:
                conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            cur.close()
            conn.close()
    return get_db_cursor


def _load(conn, accessible_account_ids=None):
    install_stub(
        "app.db",
        get_connection=lambda: conn,
        get_db_cursor=_real_get_db_cursor_stub(conn),
    )
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub(
        "app.auth.authorization",
        get_accessible_account_ids=lambda user: accessible_account_ids,
    )
    install_stub(
        "app.threshold_defaults",
        DEFAULT_THRESHOLDS={},
        FALLBACK_THRESHOLD=(70, 90, ">"),
        normalize_threshold_resource_type=lambda service: service,
        normalize_service_key=lambda resource_type, resource_id: resource_type,
    )
    install_stub("app.aws.boto_config", STANDARD_RETRY=None)
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    return load_module("app/api/metric_catalog.py")


def contains(*substrings):
    def predicate(sql, params):
        return all(s in sql for s in substrings)
    return predicate


# ── source-inspection: refactor completeness ────────────────────────────

_PREVIOUSLY_FLAGGED = [
    "get_catalog", "get_services", "get_default_template", "seed_account_defaults",
    "enable_metrics_for_services", "get_account_metrics", "_set_account_metrics_internal",
    "apply_default_template", "discover_namespace_metrics", "generate_yace_config",
]


def test_no_flagged_function_opens_a_raw_connection_anymore():
    """
    Regression test for audit b17: all 10 pre-scan-flagged functions
    (opened a connection with no try/finally) must now go through
    get_db_cursor() instead of calling get_connection() directly.
    """
    conn = _FakeConn(_FakeCursor())
    mod = _load(conn)
    for name in _PREVIOUSLY_FLAGGED:
        src = inspect.getsource(getattr(mod, name))
        assert "get_connection(" not in src, f"{name} still calls get_connection() directly"
        assert "get_db_cursor(" in src, f"{name} doesn't use get_db_cursor()"


# ── functional: connection is closed even when a handler raises ────────

def test_get_account_metrics_closes_connection_on_404():
    """
    Regression test for audit b17 (connection-leak fix): account-not-
    found used to close manually right before raising; now the whole
    block is inside get_db_cursor(), whose finally always closes
    (and whose except rolls back) regardless of how the block exits.
    Confirms the connection is still closed when the 404 path fires.
    """
    cur = _FakeCursor(script=[(contains("FROM aws_accounts"), [])])  # account not found
    conn = _FakeConn(cur)
    mod = _load(conn, accessible_account_ids=None)

    try:
        mod.get_account_metrics(999, current_user={"username": "alice", "role": "viewer"})
        assert False, "expected HTTPException"
    except Exception as e:
        assert getattr(e, "status_code", None) == 404

    assert conn.closed is True
    assert conn.rollbacks == 1  # get_db_cursor rolls back on any exception, incl. HTTPException
    assert conn.commits == 0


def test_set_account_metrics_internal_commits_and_closes_on_success():
    """
    Regression test for audit b17: _set_account_metrics_internal used
    two separate raw cursors on one connection with no try/finally.
    Confirms the refactored single get_db_cursor() block still adds/
    enables/disables correctly and commits+closes exactly once.
    """
    cur = _FakeCursor(script=[
        (contains("SELECT id FROM aws_accounts"), [{"id": 1}]),
        (contains("SELECT metric_id FROM account_metric_selections"), [{"metric_id": 5}]),
    ])
    conn = _FakeConn(cur)
    mod = _load(conn)

    result = mod._set_account_metrics_internal(1, {"enabled_metric_ids": [5, 6]})

    assert result == {"status": "saved", "enabled_count": 2}
    assert conn.commits == 1
    assert conn.closed is True
    inserted = [sql for sql, p in cur.calls if sql.startswith("INSERT INTO account_metric_selections")]
    assert len(inserted) == 1  # metric 6 (new) inserted; metric 5 (existing) just re-enabled


# ── functional: yace-config header injection fix ────────────────────────

def test_yaml_comment_safe_strips_newlines():
    """
    Regression test for audit b17: a newline in account_name/
    account_id/default_region must not survive into the YACE config's
    comment header, where it could start injecting real YAML content
    ahead of the legitimate yaml.dump() output.
    """
    conn = _FakeConn(_FakeCursor())
    mod = _load(conn)
    assert mod._yaml_comment_safe("prod\napiVersion: v2") == "prod apiVersion: v2"
    assert mod._yaml_comment_safe("normal-name") == "normal-name"
    assert mod._yaml_comment_safe("crlf\r\ninjected") == "crlf  injected"


def test_generate_yace_config_header_has_no_injected_newline():
    """
    End-to-end check: an account_name containing a newline still
    produces a config whose header block is exactly one comment line
    for that field -- no extra line sneaks in before the real
    yaml.dump() output.
    """
    cur = _FakeCursor(script=[
        (contains("FROM aws_accounts"), [{
            "account_name": "prod\napiVersion: v2",
            "account_id": "111122223333",
            "role_arn": None,
            "external_id": None,
            "default_region": "us-east-1",
        }]),
        (contains("FROM metric_catalog mc"), [{
            "namespace": "AWS/EC2", "service": "ec2", "metric_name": "CPUUtilization",
            "statistic": "Average", "default_interval": 60,
        }]),
    ])
    conn = _FakeConn(cur)
    mod = _load(conn, accessible_account_ids=None)

    resp = mod.generate_yace_config(1, download=False, tier=None,
                                     current_user={"username": "alice", "role": "admin"})
    body = resp.body.decode() if isinstance(resp.body, bytes) else resp.body
    header_lines = [l for l in body.split("\n") if l.startswith("# YACE discovery config")]
    assert len(header_lines) == 1
    assert "apiVersion: v2" not in body.split("\n")[1]  # didn't leak onto its own line
