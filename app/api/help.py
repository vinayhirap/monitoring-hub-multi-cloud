# app/api/help.py
"""
In-app Help Center API, plus the permission wiring for the Cloud API cost panel (Phase E).

  GET /api/help/sections   require_permission("help.view")
      -> {"sections": [...]}, built by app.help_content.get_sections(user_permissions, role) (Phase D).
      Filtering is SERVER-SIDE and happens twice: get_sections() decides what the user may see, and this module then
      drops any section whose `requires` code is not in the user's effective permissions, so a bug or a new section
      in the content package can never leak admin-only text to a viewer. Effective permissions are the role's codes
      minus any UNSCOPED deny override (the same rule the route gate and GET /api/permissions/me use).

  GET /api/usage/cloud-cost   require_permission("usage.view")
      The endpoint itself belongs to Phase F (app/api/usage.py, `router`). This module only decides how it is
      mounted: usage_router() returns Phase F's router when that module exists, otherwise a 501 placeholder on the
      same path with the same permission, so this patch deploys on its own and the RBAC matrix is identical before
      and after Phase F merges.

Until Phase D is merged, /api/help/sections answers 501 with a plain message instead of a 500 or a missing route.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException

from app.auth.permissions import (
    denied_permission_codes,
    get_role_permissions,
    require_permission,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/help", tags=["Help"])


def _missing(exc: ModuleNotFoundError, module: str) -> bool:
    """True only when `module` itself (or its parent package) is absent. A ModuleNotFoundError raised from INSIDE the
    module for some other dependency is a real bug and must not be reported as "not installed yet"."""
    name = getattr(exc, "name", None) or ""
    return name == module or module.startswith(name + ".")


def _effective_permissions(user: dict) -> set:
    return set(get_role_permissions(user["role"])) - set(denied_permission_codes(user))


def filter_sections(sections, permissions: set, is_admin: bool = False) -> list:
    """Drop every section the user lacks the permission for. `requires` of None/"" means everyone.
    Admin passes every permission check implicitly (app/auth/permissions.py), so admin keeps all."""
    out = []
    for s in sections or []:
        req = s.get("requires") if isinstance(s, dict) else None
        if is_admin or not req or req in permissions:
            out.append(s)
    return out


@router.get("/sections")
def help_sections(current_user: dict = Depends(require_permission("help.view"))):
    try:
        from app.help_content import get_sections
    except ModuleNotFoundError as exc:
        if not _missing(exc, "app.help_content"):
            raise
        raise HTTPException(status_code=501, detail="Help Center content is not installed on this server yet")

    perms = _effective_permissions(current_user)
    role = current_user["role"]
    sections = get_sections(perms, role)
    return {"sections": filter_sections(sections, perms, is_admin=(role == "admin"))}


# ── Cloud API cost panel mounting ────────────────────────────────────────────

_placeholder = APIRouter(prefix="/api/usage", tags=["Usage"])


@_placeholder.get("/cloud-cost")
def cloud_cost_not_installed(current_user: dict = Depends(require_permission("usage.view"))):
    raise HTTPException(status_code=501, detail="Cloud API cost summary is not installed on this server yet")


def usage_router() -> APIRouter:
    """Phase F's router if app/api/usage.py exists, else the 501 placeholder."""
    try:
        from app.api.usage import router as real
        return real
    except ModuleNotFoundError as exc:
        if not _missing(exc, "app.api.usage"):
            raise
        return _placeholder
