"""
tests/test_access_control_audit.py

Regression tests for the final RBAC / user-management audit
(db/migrations/075, app/auth/principals.py and the lifecycle endpoints in
app/api/admin/users.py, bindings.py, rbac_scopes.py, roles.py,
app/auth/permissions.py).

Same pattern as tests/test_users_admin_rbac.py: call the router functions
directly with app.db / authorization / mailer / audit stubbed through
conftest.install_stub, and a scripted FakeConn. A RecordingConn variant
also logs every statement so ordering/atomicity can be asserted.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from conftest import load_module, install_stub, FakeConn, FakeCursor, contains  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

ADMIN = {"id": 1, "username": "root-admin", "role": "admin"}
EDITOR = {"id": 2, "username": "ed", "role": "editor"}


# ───────────────────────── harness ─────────────────────────

class _Rec(FakeCursor):
    lastrowid = 7
    rowcount = 1

    def __init__(self, script, log):
        super().__init__(script)
        self._log = log

    def execute(self, sql, params=None):
        self._log.append((" ".join(sql.split()), params))
        super().execute(sql, params)


class RecordingConn(FakeConn):
    def __init__(self, script):
        super().__init__(script)
        self.log = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, dictionary=True):
        return _Rec(self.script, self.log)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _install_common(conn, mailer_configured=False, sent=None):
    install_stub("app.db", get_connection=lambda: conn)
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None),
                 has_permission=lambda user, code: True)
    install_stub(
        "app.auth.authorization",
        get_effective_scope=lambda user: [], scope_within=lambda s, a: True,
        validate_scope_shape=lambda s, v: None, serialize_scope=lambda u: [],
        can_manage_role=lambda a, r: True, FULL_ACCESS="FULL_ACCESS",
        _parse_json_list=lambda v: v or [],
    )
    install_stub("app.auth.security", hash_password=lambda pw: f"hashed:{pw}")

    def _send(**kw):
        if sent is not None:
            sent.append(kw)
        return True
    install_stub("app.email.mailer", is_configured=lambda: mailer_configured,
                 get_public_app_url=lambda: "https://example.test", send_email=_send)
    install_stub("app.audit", write_audit=lambda **kw: None)
    install_stub("app.utils.time_json", to_utc_iso=lambda v: v)
    install_stub("app.auth.deps", get_current_user=lambda: None, forget_user_sessions=lambda uid: None)
    install_stub("app.auth.rbac", invalidate_principal=lambda uid=None: None)
    principals = load_module("app/auth/principals.py")
    install_stub("app.auth.principals", **{k: getattr(principals, k) for k in dir(principals) if not k.startswith("__")})
    return principals


def _users(script, **kw):
    conn = RecordingConn(script)
    _install_common(conn, **kw)
    return load_module("app/api/admin/users.py"), conn


# ───────────────────────── principals ─────────────────────────

def test_reassign_authorship_covers_every_restrict_fk():
    conn = RecordingConn([(contains("UPDATE"), None)])
    principals = _install_common(conn)
    principals.reassign_authorship(conn, 9, 1)
    tables = {sql.split()[1] for sql, _ in conn.log}
    assert tables == {
        "access_scopes", "org_groups", "group_policies", "user_group_memberships",
        "rbac_scopes", "role_bindings", "permission_overrides", "access_reviews",
    }
    assert all(params == (1, 9) for _, params in conn.log)
    assert conn.commits == 0, "helper must leave the transaction to the caller"


def test_purge_principal_grants_targets_three_tables_and_validates_type():
    conn = RecordingConn([(contains("DELETE FROM"), None)])
    principals = _install_common(conn)
    out = principals.purge_principal_grants(conn, "group", 5)
    assert {sql.split()[2] for sql, _ in conn.log} == {"role_bindings", "permission_overrides", "access_reviews"}
    assert set(out) == {"bindings", "overrides", "reviews"}
    with pytest.raises(ValueError):
        principals.purge_principal_grants(conn, "team", 5)


def test_active_admin_count_ignores_deactivated_admins():
    conn = RecordingConn([(contains("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'", "active = 1"), [{"n": 2}])])
    principals = _install_common(conn)
    assert principals.active_admin_count(conn, exclude_user_id=4) == 2
    assert "active = 1" in conn.log[0][0]


# ───────────────────────── delete_user ─────────────────────────

def _delete_script(role="viewer", other_admins=1):
    return [
        (contains("SELECT id, username, role FROM users WHERE id"), [{"id": 4, "username": "victim", "role": role}]),
        (contains("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'"), [{"n": other_admins}]),
        (contains("UPDATE"), None),
        (contains("DELETE FROM"), None),
    ]


def test_delete_user_reassigns_authorship_purges_grants_then_deletes_atomically():
    users, conn = _users(_delete_script())
    users.delete_user(4, current_user=ADMIN)
    sql = [s for s, _ in conn.log]
    first_delete_user = next(i for i, s in enumerate(sql) if s.startswith("DELETE FROM users"))
    updates = [i for i, s in enumerate(sql) if s.startswith("UPDATE")]
    purges = [i for i, s in enumerate(sql) if s.startswith("DELETE FROM") and "users" not in s.split()[2]]
    assert len(updates) == 8 and len(purges) == 3
    assert max(updates) < first_delete_user and max(purges) < first_delete_user
    assert conn.commits == 1 and conn.rollbacks == 0


def test_delete_user_refuses_self():
    users, _ = _users(_delete_script())
    with pytest.raises(HTTPException) as e:
        users.delete_user(ADMIN["id"], current_user=ADMIN)
    assert e.value.status_code == 403


def test_delete_user_refuses_last_active_admin():
    users, conn = _users(_delete_script(role="admin", other_admins=0))
    with pytest.raises(HTTPException) as e:
        users.delete_user(4, current_user=ADMIN)
    assert e.value.status_code == 409
    assert not any(s.startswith("DELETE") for s, _ in conn.log)


def test_delete_user_rolls_back_and_returns_409_when_db_refuses():
    class Boom(RecordingConn):
        def cursor(self, dictionary=True):
            cur = super().cursor(dictionary)
            orig = cur.execute

            def ex(sql, params=None):
                if sql.strip().startswith("DELETE FROM users"):
                    raise RuntimeError("fk violation")
                return orig(sql, params)
            cur.execute = ex
            return cur
    conn = Boom(_delete_script())
    _install_common(conn)
    users = load_module("app/api/admin/users.py")
    with pytest.raises(HTTPException) as e:
        users.delete_user(4, current_user=ADMIN)
    assert e.value.status_code == 409 and conn.rollbacks == 1 and conn.commits == 0


# ───────────────────────── deactivate / activate ─────────────────────────

def _target(role="viewer", active=1):
    return [(contains("SELECT id, username, role, email, active FROM users WHERE id"),
             [{"id": 4, "username": "victim", "role": role, "email": None, "active": active}])]


def test_deactivate_sets_inactive_and_bumps_token_version():
    users, conn = _users(_target() + [(contains("UPDATE users SET active = 0"), None)])
    out = users.deactivate_user(4, current_user=ADMIN)
    assert out["status"] == "deactivated"
    upd = next(s for s, _ in conn.log if s.startswith("UPDATE users SET active = 0"))
    assert "token_version = token_version + 1" in upd


def test_deactivate_refuses_self_and_last_admin_and_already_inactive():
    users, _ = _users(_target())
    with pytest.raises(HTTPException) as e:
        users.deactivate_user(ADMIN["id"], current_user=ADMIN)
    assert e.value.status_code == 403

    users, _ = _users(_target(role="admin") + [(contains("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'"), [{"n": 0}])])
    with pytest.raises(HTTPException) as e:
        users.deactivate_user(4, current_user=ADMIN)
    assert e.value.status_code == 409

    users, _ = _users(_target(active=0))
    with pytest.raises(HTTPException) as e:
        users.deactivate_user(4, current_user=ADMIN)
    assert e.value.status_code == 409


def test_activate_requires_inactive_user():
    users, _ = _users(_target(active=1))
    with pytest.raises(HTTPException) as e:
        users.activate_user(4, current_user=ADMIN)
    assert e.value.status_code == 409

    users, conn = _users(_target(active=0) + [(contains("UPDATE users SET active = 1"), None)])
    assert users.activate_user(4, current_user=ADMIN)["status"] == "activated"


# ───────────────────────── admin password reset ─────────────────────────

def test_admin_reset_returns_link_once_when_email_not_sent():
    users, conn = _users(_target() + [(contains("INSERT INTO password_reset_tokens"), None)])
    out = users.admin_reset_password(4, current_user=ADMIN)
    assert out["email_sent"] is False and out["reset_link"].startswith("https://example.test/reset-password?token=")
    stored = next(p for s, p in conn.log if "INSERT INTO password_reset_tokens" in s)
    assert out["reset_link"].split("token=")[1] not in stored, "only the HASH may be stored"


def test_admin_reset_emails_and_does_not_return_link_when_configured():
    sent = []
    row = [{"id": 4, "username": "victim", "role": "viewer", "email": "v@x.io", "active": 1}]
    users, _ = _users([(contains("SELECT id, username, role, email, active FROM users WHERE id"), row),
                       (contains("INSERT INTO password_reset_tokens"), None)], mailer_configured=True, sent=sent)
    out = users.admin_reset_password(4, current_user=ADMIN)
    assert out["email_sent"] is True and "reset_link" not in out and len(sent) == 1


def test_admin_reset_refuses_self():
    users, _ = _users(_target())
    with pytest.raises(HTTPException) as e:
        users.admin_reset_password(ADMIN["id"], current_user=ADMIN)
    assert e.value.status_code == 403


# ───────────────────────── validation ─────────────────────────

@pytest.mark.parametrize("name", ["bad name", "-lead", "a" * 101, "x;drop", "t\nx"])
def test_create_user_rejects_bad_usernames(name):
    users, _ = _users([])
    with pytest.raises(HTTPException) as e:
        users.create_user(payload={"username": name, "password": "longenough", "role": "viewer"}, current_user=ADMIN)
    assert e.value.status_code == 400


def test_update_user_validates_email():
    users, _ = _users(_target())
    with pytest.raises(HTTPException) as e:
        users.update_user(4, payload={"email": "nope"}, current_user=ADMIN)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        users.update_user(4, payload={}, current_user=ADMIN)
    assert e.value.status_code == 400


# ───────────────────────── overrides ─────────────────────────

def _bindings(script):
    conn = RecordingConn(script)
    _install_common(conn)
    return load_module("app/api/admin/bindings.py"), conn


def _ov(**kw):
    base = {"principal_type": "user", "principal_id": 4, "permission_code": "alerts.view", "effect": "deny", "reason": "audit"}
    base.update(kw)
    return base


def test_allow_override_is_rejected_because_resolver_ignores_it():
    b, _ = _bindings([])
    with pytest.raises(HTTPException) as e:
        b.create_override(payload=_ov(effect="allow"), current_user=ADMIN)
    assert e.value.status_code == 400 and "deny" in e.value.detail.lower()


def test_deny_on_an_admin_is_rejected_because_admins_bypass_the_table():
    b, _ = _bindings([(contains("SELECT id FROM users WHERE id"), [{"id": 4}]),
                      (contains("SELECT role FROM users WHERE id"), [{"role": "admin"}])])
    with pytest.raises(HTTPException) as e:
        b.create_override(payload=_ov(), current_user=ADMIN)
    assert e.value.status_code == 400 and "Administrators" in e.value.detail


def test_expiry_in_the_past_is_rejected():
    b, _ = _bindings([])
    with pytest.raises(HTTPException) as e:
        b._parse_expiry("2001-01-01T00:00:00Z")
    assert e.value.status_code == 400
    assert b._parse_expiry(None) is None
    with pytest.raises(HTTPException):
        b._parse_expiry("not-a-date")


# ───────────────────────── global deny enforcement ─────────────────────────

def _perms(access_or_exc, role_codes=("alerts.view",)):
    install_stub("app.db", get_connection=lambda: RecordingConn(
        [(contains("SELECT p.code"), [{"code": c} for c in role_codes]), (contains("SELECT code"), [{"code": c} for c in role_codes])]))
    install_stub("app.auth.deps", get_current_user=lambda: None)

    def resolve(user):
        if isinstance(access_or_exc, Exception):
            raise access_or_exc
        return access_or_exc
    install_stub("app.auth.rbac", resolve=resolve)
    mod = load_module("app/auth/permissions.py")
    mod.get_role_permissions = lambda role: set(role_codes)  # isolate from the role_permissions query
    return mod


def _den(code, scope=None):
    return types.SimpleNamespace(permission_code=code, scope=scope)


VIEWER = {"id": 9, "username": "v", "role": "viewer"}


def test_unscoped_deny_blocks_a_permission_the_role_grants():
    perms = _perms(types.SimpleNamespace(denials=[_den("alerts.view")]))
    assert perms.has_permission(VIEWER, "alerts.view") is False
    assert perms.denied_permission_codes(VIEWER) == {"alerts.view"}


def test_scoped_deny_is_not_applied_at_the_route_gate():
    perms = _perms(types.SimpleNamespace(denials=[_den("alerts.view", scope=object())]))
    assert perms.has_permission(VIEWER, "alerts.view") is True


def test_admin_is_never_denied_even_with_a_stray_deny_row():
    perms = _perms(types.SimpleNamespace(denials=[_den("alerts.view")]))
    assert perms.has_permission({"id": 1, "username": "a", "role": "admin"}, "alerts.view") is True


def test_missing_v2_tables_fail_open_but_other_errors_propagate():
    class Err(Exception):
        errno = 1146
    assert _perms(Err("no table")).has_permission(VIEWER, "alerts.view") is True
    with pytest.raises(RuntimeError):
        _perms(RuntimeError("db down")).has_permission(VIEWER, "alerts.view")
