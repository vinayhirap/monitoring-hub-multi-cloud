"""
tests/test_users_admin_rbac.py

Regression tests for audit chat 3 (RBAC administration APIs, files
app/api/admin/users.py + groups.py), covering the fixes in this patch:

  1. create_user() must INSERT into users.password -- CONFIRMED against
     the live dev database via `SHOW COLUMNS FROM users` on 2026-09-22
     (column is `password varchar(255)`, not `password_hash`). The
     checked-in db/schema.sql in this repo is stale/drifted from the
     real schema (the exact failure mode migration 011's own comment
     warns about) and must not be trusted as the source of truth for a
     column name -- an earlier version of this patch got this backwards
     by trusting db/schema.sql instead of the live DB, which broke a
     previously-working create_user on dev. This test now asserts
     against the column confirmed live, and its name/docstring say so
     explicitly, specifically so nobody "fixes" it back the wrong way
     from schema.sql without re-checking the real database first.
  2. update_role() / delete_user() must refuse to demote or delete the
     last remaining admin.

These call the router functions directly (bypassing FastAPI's DI, since
Depends(...) is only resolved when invoked through the ASGI app) with
app.db, app.auth.authorization, app.email.mailer and app.audit stubbed
out via tests/conftest.py's install_stub/load_module, the same pattern
tests/test_rbac_v2.py and friends already use in this suite.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from conftest import load_module, install_stub, FakeConn, FakeCursor, contains  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _FakeCursorWithLastrowid(FakeCursor):
    """The shared FakeCursor has no notion of AUTO_INCREMENT lastrowid
    (nothing in the existing suite needed one). create_user() reads
    cursor.lastrowid right after its INSERT, so this local variant adds
    a fixed stand-in value -- its exact number is never asserted on."""
    lastrowid = 42


class _FakeConnWithLastrowid(FakeConn):
    def cursor(self, dictionary=True):
        return _FakeCursorWithLastrowid(self.script)


def _load_users_module(script, conn_factory=FakeConn):
    """Fresh, isolated import of app/api/admin/users.py with its
    dependencies stubbed and the given FakeConn query script wired up
    as get_connection()'s return value."""
    install_stub("app.db", get_connection=lambda: conn_factory(script))
    install_stub(
        "app.auth.permissions",
        require_permission=lambda code: (lambda: None),
    )
    install_stub(
        "app.auth.authorization",
        get_effective_scope=lambda user: [],
        scope_within=lambda scopes, actor_scope: True,
        validate_scope_shape=lambda scope, valid_accounts: None,
        serialize_scope=lambda user: [],
        can_manage_role=lambda actor, target_role: True,
        FULL_ACCESS="FULL_ACCESS",
    )
    install_stub(
        "app.auth.security",
        hash_password=lambda pw: f"hashed:{pw}",
    )
    install_stub(
        "app.email.mailer",
        is_configured=lambda: False,
        get_public_app_url=lambda: "https://example.test",
        send_email=lambda **kw: True,
    )
    install_stub("app.audit", write_audit=lambda **kw: None)
    return load_module("app/api/admin/users.py")


ADMIN = {"id": 1, "username": "root-admin", "role": "admin"}


# ── create_user: password column (confirmed live, see module docstring) ──

def test_create_user_inserts_into_confirmed_live_password_column():
    """
    SHOW COLUMNS FROM users on the real dev database (2026-09-22)
    confirmed the column is `password`, not `password_hash` --
    db/schema.sql as checked into this repo is stale. This asserts the
    INSERT targets the real column and that the script only answers to
    that -- a regression to `password_hash` makes FakeCursor raise
    AssertionError("no script entry matched"), failing the test.
    """
    script = [
        (contains("INSERT INTO users (username, password, role, email)"),
         None),
    ]
    users_mod = _load_users_module(script, conn_factory=_FakeConnWithLastrowid)

    result = users_mod.create_user(
        payload={"username": "newbie", "password": "correcthorse", "role": "viewer", "scopes": []},
        current_user=ADMIN,
    )
    assert result["status"] == "created"
    assert result["username"] == "newbie"


# ── update_role: last-admin protection ──────────────────────────────

def test_update_role_blocks_demoting_the_last_admin():
    script = [
        (contains("SELECT username, role FROM users WHERE id"),
         [{"username": "solo-admin", "role": "admin"}]),
        (contains("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'"),
         [{"n": 1}]),
    ]
    users_mod = _load_users_module(script)

    with pytest.raises(HTTPException) as exc:
        users_mod.update_role(
            user_id=2, payload={"role": "viewer"}, current_user=ADMIN,
        )
    assert exc.value.status_code == 409
    assert "last remaining admin" in exc.value.detail


def test_update_role_allows_demotion_when_another_admin_remains():
    script = [
        (contains("SELECT username, role FROM users WHERE id"),
         [{"username": "second-admin", "role": "admin"}]),
        (contains("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'"),
         [{"n": 2}]),
        (contains("UPDATE users SET role"), None),
    ]
    users_mod = _load_users_module(script)

    result = users_mod.update_role(
        user_id=2, payload={"role": "viewer"}, current_user=ADMIN,
    )
    assert result == {"status": "updated", "id": 2, "role": "viewer"}


def test_update_role_does_not_count_admins_for_non_admin_target():
    """A demotion/promotion among non-admin roles should never even run
    the admin-count query -- FakeCursor raises if it's asked for a
    query not in the script, which proves it wasn't issued."""
    script = [
        (contains("SELECT username, role FROM users WHERE id"),
         [{"username": "some-editor", "role": "editor"}]),
        (contains("UPDATE users SET role"), None),
    ]
    users_mod = _load_users_module(script)

    result = users_mod.update_role(
        user_id=3, payload={"role": "viewer"}, current_user=ADMIN,
    )
    assert result["role"] == "viewer"


# ── delete_user: last-admin protection ──────────────────────────────

def test_delete_user_blocks_deleting_the_last_admin():
    script = [
        (contains("SELECT id, username, role FROM users WHERE id"),
         [{"id": 2, "username": "solo-admin", "role": "admin"}]),
        (contains("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'"),
         [{"n": 1}]),
    ]
    users_mod = _load_users_module(script)

    with pytest.raises(HTTPException) as exc:
        users_mod.delete_user(user_id=2, current_user=ADMIN)
    assert exc.value.status_code == 409
    assert "last remaining admin" in exc.value.detail


def test_delete_user_allows_deleting_admin_when_another_remains():
    script = [
        (contains("SELECT id, username, role FROM users WHERE id"),
         [{"id": 2, "username": "second-admin", "role": "admin"}]),
        (contains("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'"),
         [{"n": 2}]),
        (contains("DELETE FROM users WHERE id"), None),
    ]
    users_mod = _load_users_module(script)

    result = users_mod.delete_user(user_id=2, current_user=ADMIN)
    assert result == {"status": "deleted", "id": 2, "username": "second-admin"}


def test_delete_user_does_not_count_admins_for_non_admin_target():
    script = [
        (contains("SELECT id, username, role FROM users WHERE id"),
         [{"id": 4, "username": "some-viewer", "role": "viewer"}]),
        (contains("DELETE FROM users WHERE id"), None),
    ]
    users_mod = _load_users_module(script)

    result = users_mod.delete_user(user_id=4, current_user=ADMIN)
    assert result["username"] == "some-viewer"
