"""
tests/test_rbac_review_2026_10.py

Regression tests for the October 2026 RBAC review:
  * update_role: a delegated roles.manage holder cannot assign or change a role above their own
  * bindings PATCH / DELETE: same escalation gate as POST
  * overrides DELETE: a delegated holder cannot lift a deny someone else created
  * authorization.require_org_wide: unscoped billing / org-wide settings need org-wide access
Harness: same stubbing pattern as tests/test_access_control_audit.py.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from conftest import load_module, install_stub, contains  # noqa: E402
from test_access_control_audit import RecordingConn, _install_common, ADMIN, EDITOR  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _users(script):
    conn = RecordingConn(script)
    _install_common(conn)
    return load_module("app/api/admin/users.py"), conn


def _bindings(script, can_grant=True):
    conn = RecordingConn(script)
    _install_common(conn)
    install_stub("app.auth.rbac", invalidate_principal=lambda uid=None: None,
                 can_grant=lambda actor, role_key, scope: can_grant, _scope_from_row=lambda row: row)
    return load_module("app/api/admin/bindings.py"), conn


# ── update_role ──
@pytest.mark.parametrize("target_role,new_role", [("viewer", "admin"), ("admin", "viewer"), ("editor", "admin")])
def test_non_admin_cannot_cross_above_own_role(target_role, new_role):
    users, conn = _users([(contains("SELECT username, role FROM users WHERE id"), [{"username": "x", "role": target_role}])])
    with pytest.raises(HTTPException) as e:
        users.update_role(user_id=9, payload={"role": new_role}, current_user=EDITOR)
    assert e.value.status_code == 403
    assert not any("UPDATE users SET role" in s for s, _ in conn.log)


def test_non_admin_may_still_set_roles_at_or_below_own():
    users, _ = _users([(contains("SELECT username, role FROM users WHERE id"), [{"username": "x", "role": "viewer"}]),
                       (contains("UPDATE users SET role"), None)])
    assert users.update_role(user_id=9, payload={"role": "editor"}, current_user=EDITOR)["role"] == "editor"


# ── bindings ──
_BROW = {"id": 5, "principal_type": "user", "principal_id": 4, "role_key": "admin", "role_rank": 30, "scope_id": 3}


def test_patch_binding_blocked_without_grant_rights():
    b, conn = _bindings([(contains("FROM role_bindings b JOIN roles r"), [_BROW]), (contains("FROM rbac_scopes"), [{"id": 3}])], can_grant=False)
    with pytest.raises(HTTPException) as e:
        b.update_binding(5, payload={"expires_at": None}, current_user=EDITOR)
    assert e.value.status_code == 403
    assert not any(s.startswith("UPDATE role_bindings") for s, _ in conn.log)


def test_revoke_binding_blocked_without_grant_rights():
    b, conn = _bindings([(contains("FROM role_bindings b JOIN roles r"), [_BROW]), (contains("FROM rbac_scopes"), [{"id": 3}])], can_grant=False)
    with pytest.raises(HTTPException) as e:
        b.revoke_binding(5, current_user=EDITOR)
    assert e.value.status_code == 403
    assert not any(s.startswith("DELETE FROM role_bindings") for s, _ in conn.log)


def test_revoke_binding_allowed_for_admin_without_scope_lookup():
    b, conn = _bindings([(contains("FROM role_bindings b JOIN roles r"), [_BROW]),
                         (contains("DELETE FROM role_bindings"), None)], can_grant=False)
    assert b.revoke_binding(5, current_user=ADMIN)["status"] == "revoked"
    assert not any("rbac_scopes" in s for s, _ in conn.log)


# ── overrides ──
def test_delegated_user_cannot_delete_override_created_by_someone_else():
    b, conn = _bindings([(contains("SELECT * FROM permission_overrides"),
                          [{"id": 8, "principal_type": "user", "principal_id": 4, "granted_by": 1}])])
    with pytest.raises(HTTPException) as e:
        b.delete_override(8, current_user=EDITOR)
    assert e.value.status_code == 403
    assert not any(s.startswith("DELETE FROM permission_overrides") for s, _ in conn.log)


def test_delegated_user_can_delete_their_own_override():
    b, _ = _bindings([(contains("SELECT * FROM permission_overrides"),
                       [{"id": 8, "principal_type": "user", "principal_id": 4, "granted_by": EDITOR["id"]}]),
                      (contains("DELETE FROM permission_overrides"), None)])
    assert b.delete_override(8, current_user=EDITOR)["status"] == "deleted"


# ── require_org_wide ──
def _authz():
    install_stub("app.db", get_connection=lambda: None)
    return load_module("app/auth/authorization.py")


def test_require_org_wide_allows_full_access_and_refuses_scoped_users():
    a = _authz()
    a.get_accessible_account_ids = lambda user: None
    a.require_org_wide(ADMIN, "Billed cost")
    for scoped in (set(), {1, 2}):
        a.get_accessible_account_ids = lambda user, s=scoped: s
        with pytest.raises(HTTPException) as e:
            a.require_org_wide(EDITOR, "Billed cost")
        assert e.value.status_code == 403
