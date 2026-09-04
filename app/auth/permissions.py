# app/auth/permissions.py
"""
app/auth/permissions.py

Granular permission-identifier RBAC, layered ON TOP OF the existing
role system (admin/editor/viewer) rather than replacing it --
role_permissions (db/migrations/015_permissions_rbac.sql) maps each of
the 3 existing roles to a set of permission codes. Nothing about how a
user GETS a role changes here -- direct assignment, or via L1/L2/L3
group membership through app.auth.authorization.GROUP_LEVEL_ROLE --
this only makes what that role can DO expressible as named permissions
(users.create, groups.manage, ...) instead of role checks scattered
through every endpoint.

admin implicitly has every permission (bypasses the table lookup
entirely) -- deliberate: a gap in role_permissions seed data can never
lock an admin out of their own system, which would be a far worse
failure mode than the table needing to explicitly list every admin
permission. Anything genuinely admin-only should still be gated with
require_permission(...) as normal; admin always passes.
"""
import logging

from fastapi import Depends, HTTPException
from app.auth.deps import get_current_user
from app.db import get_connection

logger = logging.getLogger(__name__)


def get_role_permissions(role: str) -> set:
    """All permission codes granted to a role. admin -> every code
    that exists (see module docstring for why)."""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        if role == "admin":
            cursor.execute("SELECT code FROM permissions")
        else:
            cursor.execute(
                "SELECT p.code FROM role_permissions rp "
                "JOIN permissions p ON p.id = rp.permission_id "
                "WHERE rp.role = %s",
                (role,),
            )
        return {r["code"] for r in cursor.fetchall()}
    finally:
        cursor.close()
        conn.close()


def has_permission(user: dict, code: str) -> bool:
    if user.get("role") == "admin":
        return True
    return code in get_role_permissions(user.get("role"))


def require_permission(code: str):
    """
    Depends(require_permission("users.create")) -- 403s if the
    authenticated user's role doesn't grant this permission.
    get_current_user (run first, as this function's own dependency)
    already 401s for a missing/invalid session, so the ordering here
    is exactly: 401 (not authenticated) -> 403 (authenticated, but
    lacking this permission) -> the endpoint itself, matching the
    chain called for in the RBAC spec.
    """
    def _check(user: dict = Depends(get_current_user)) -> dict:
        if not has_permission(user, code):
            raise HTTPException(status_code=403, detail=f"Missing permission: {code}")
        return user
    return _check
