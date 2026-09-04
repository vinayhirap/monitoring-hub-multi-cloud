# app/api/permissions.py
"""
app/api/permissions.py

Read-only endpoints backing:
  - GET /api/permissions/me  -- every permission code the CURRENT
    user's role grants. Fetched once at login and cached in
    AuthContext; frontend hasPermission()/hasAnyPermission()/
    hasAllPermissions() are built from this, not from re-deriving
    role logic client-side.
  - GET /api/permissions     -- the full catalog, grouped by category,
    with a per-role granted/not-granted matrix -- what the redesigned
    Roles & Permissions admin screen renders. is_internal permissions
    (SMTP, system config) are deliberately excluded from this response
    -- they're still enforced on their actual endpoints via
    require_permission(...), just never surfaced in the normal RBAC
    permission-management UI.
"""
from fastapi import APIRouter, Depends
from app.db import get_connection
from app.auth.deps import get_current_user, require_role
from app.auth.permissions import get_role_permissions

router = APIRouter(prefix="/api/permissions", tags=["Permissions"])


@router.get("/me")
def my_permissions(current_user: dict = Depends(get_current_user)):
    codes = get_role_permissions(current_user["role"])
    return {"role": current_user["role"], "permissions": sorted(codes)}


@router.get("")
def list_permissions(current_user: dict = Depends(require_role("admin"))):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, code, category, label, description FROM permissions "
        "WHERE is_internal = 0 ORDER BY category, code"
    )
    perms = cursor.fetchall()

    cursor.execute("SELECT role, permission_id FROM role_permissions")
    grants = cursor.fetchall()
    cursor.close()
    conn.close()

    granted_by_role = {"viewer": set(), "editor": set(), "admin": set()}
    for g in grants:
        granted_by_role.setdefault(g["role"], set()).add(g["permission_id"])

    categories = {}
    for p in perms:
        bucket = categories.setdefault(p["category"], [])
        bucket.append({
            "id": p["id"], "code": p["code"], "label": p["label"],
            "description": p["description"],
            "roles": {
                "viewer": p["id"] in granted_by_role["viewer"],
                "editor": p["id"] in granted_by_role["editor"],
                # admin implicitly has every permission (see
                # app.auth.permissions module docstring) -- always
                # rendered as granted regardless of the row's literal
                # presence in role_permissions.
                "admin":  True,
            },
        })
    return [{"category": cat, "permissions": items} for cat, items in categories.items()]
