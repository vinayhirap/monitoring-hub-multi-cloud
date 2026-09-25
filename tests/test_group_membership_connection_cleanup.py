# tests/test_group_membership_connection_cleanup.py
"""
Regression coverage for app/api/admin/groups.py's add_group_members().

An earlier version of this function only called conn.close() on the
specific error branches it anticipated (group not found, missing user
id, a non-duplicate INSERT error) -- any OTHER exception (e.g. the
users-lookup SELECT itself hitting a transient DB error) skipped every
close() and leaked a connection out of the pool. That was fixed
upstream (the whole function body is wrapped in try/finally so
conn.close() is unconditional); this test locks that behavior in so a
future edit to this function can't silently reintroduce the leak.

Uses this repo's load_module()/install_stub() convention (see
tests/conftest.py) rather than a real database.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0] if "/tests/" in __file__ else ".")
from tests.conftest import load_module, install_stub, FakeConn


def _install_common_stubs(get_conn):
    install_stub("app.db", get_connection=get_conn)
    install_stub("app.email.mailer", send=lambda *a, **k: None)
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.api.admin.users", _user_manageable_by=lambda actor, target: True)


def _fake_get_group(conn, group_id):
    return {"id": group_id, "name": "Test Group", "level": "L2", "parent_group_id": 1}


def test_add_group_members_closes_connection_on_unexpected_db_error():
    """THE REGRESSION CASE: an exception raised before any of the old
    explicit conn.close() call sites (here, cursor() itself blowing up,
    standing in for any transient DB error during the users-lookup
    SELECT) must still result in the connection being closed exactly
    once, and must still surface as an error to the caller."""
    audit_calls = []

    class _Conn(FakeConn):
        def __init__(self):
            self.closed = 0

        def cursor(self, dictionary=True):
            raise RuntimeError("simulated transient DB error")

        def close(self):
            self.closed += 1

    conn = _Conn()
    _install_common_stubs(lambda: conn)
    install_stub("app.audit", write_audit=lambda *a, **k: audit_calls.append((a, k)))
    install_stub("app.auth.authorization", get_group=_fake_get_group)

    mod = load_module("app/api/admin/groups.py")

    raised = False
    try:
        mod.add_group_members(
            1, {"user_ids": [5]},
            current_user={"id": 1, "username": "admin", "role": "admin"},
        )
    except RuntimeError:
        raised = True

    assert raised, "the underlying error must still propagate, not be swallowed"
    assert conn.closed == 1, (
        f"connection must be closed exactly once even on an unanticipated "
        f"exception (was leaked before this fix); got {conn.closed}"
    )
    assert not audit_calls, "no audit row should be written when the operation failed"


def test_add_group_members_happy_path_unchanged():
    """Confirms the try/finally refactor didn't change behaviour on the
    normal success path: same return shape, one commit, one close, one
    audit call, in the same order as before."""
    audit_calls = []
    events = []

    class _Cursor:
        def __init__(self):
            self._pending = None

        def execute(self, sql, params=None):
            norm = " ".join(sql.split())
            if norm.startswith("SELECT id FROM users"):
                self._pending = [{"id": 5}]
            elif norm.startswith("INSERT INTO user_group_memberships"):
                events.append("insert")
                self._pending = None
            else:
                raise AssertionError(f"unexpected query: {norm!r}")

        def fetchall(self):
            return self._pending or []

        def close(self):
            pass

    class _Conn(FakeConn):
        def __init__(self):
            self.closed = 0
            self.committed = 0

        def cursor(self, dictionary=False):
            return _Cursor()

        def close(self):
            self.closed += 1
            events.append("close")

        def commit(self):
            self.committed += 1
            events.append("commit")

    conn = _Conn()
    _install_common_stubs(lambda: conn)
    install_stub("app.audit", write_audit=lambda *a, **k: audit_calls.append((a, k)))
    install_stub("app.auth.authorization", get_group=_fake_get_group)

    mod = load_module("app/api/admin/groups.py")

    result = mod.add_group_members(
        1, {"user_ids": [5]},
        current_user={"id": 1, "username": "admin", "role": "admin"},
    )

    assert result == {
        "status": "updated", "group_id": 1, "added": [5], "already_member": [],
    }
    assert conn.committed == 1
    assert conn.closed == 1
    assert len(audit_calls) == 1
    # commit happens before close, which happens before the audit write
    # (audit runs after conn is released, same as before this fix).
    assert events == ["insert", "commit", "close"]
