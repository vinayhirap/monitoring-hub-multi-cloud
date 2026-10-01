# app/auth/permissions.py
"""
app/auth/permissions.py

Granular permission-identifier RBAC, layered ON TOP OF the existing
role system (admin/editor/viewer) rather than replacing it --
role_permissions (db/migrations/015_permissions_rbac.sql) maps each of
the 3 existing roles to a set of permission codes. A user's role is
always assigned deliberately (app/api/admin/users.py) and is never
derived from L1/L2/L3 group membership -- groups only ever grant
account/region SCOPE, never role. This only makes what a role can DO
expressible as named permissions (users.create, groups.manage, ...)
instead of role checks scattered through every endpoint.

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


def _globally_denied(user: dict, code: str) -> bool:
    """
    True if an UNSCOPED deny override (scope = "everywhere") targets this
    user (directly, or via a group they are in) for this permission.

    Until this existed, permission_overrides rows were stored and listed in
    the RBAC admin UI but nothing on the request path ever read them -- a
    "deny" that denied nothing. An unscoped deny is the one form that is
    safe to enforce at the route gate: it can only REMOVE access, so it
    cannot over-grant, and it needs no per-row scope logic. (Scoped denies
    still need row-level enforcement in each data endpoint.)

    Fails open ONLY when the v2 tables are missing (a DB that hasn't run
    migration 040 yet); any other error propagates so authorization never
    silently degrades into "allowed".
    """
    try:
        from app.auth import rbac
    except Exception:  # pragma: no cover - module absent in some unit-test stubs
        return False
    try:
        access = rbac.resolve(user)
    except Exception as exc:
        errno = getattr(exc, "errno", None)
        if errno in (1146, 1054):  # table / column doesn't exist yet
            return False
        raise
    return any(d.permission_code == code and d.scope is None for d in access.denials)


def denied_permission_codes(user: dict) -> set:
    """Every permission code an unscoped deny override removes from this user
    (used by GET /api/permissions/me so the UI hides what the API refuses)."""
    if user.get("role") == "admin":
        return set()
    try:
        from app.auth import rbac
        access = rbac.resolve(user)
    except Exception:
        return set()
    return {d.permission_code for d in access.denials if d.scope is None}


def has_permission(user: dict, code: str) -> bool:
    if user.get("role") == "admin":
        return True
    if code not in get_role_permissions(user.get("role")):
        return False
    return not _globally_denied(user, code)


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
