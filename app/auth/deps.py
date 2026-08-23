"""
app/auth/deps.py

FastAPI dependencies for authenticated routes. Reads the session from
the httpOnly `mh_session` cookie set by POST /api/auth/login — never
trusts any client-supplied identity field (body params, headers, etc.).

require_account_access() from the previous (dead, unused) version of
this file is intentionally NOT carried forward here — cloud/account/
region SCOPE enforcement is a separate, later authorization layer
(Phase 1+ of the RBAC plan), not something to half-implement now.
"""
from fastapi import Depends, HTTPException, Request
import jwt

from app.auth.security import decode_token

COOKIE_NAME = "mh_session"


def get_current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired, please log in again")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid session")


def require_role(*roles: str):
    """
    Depends(require_role('admin')) — 403s if the authenticated user's
    role isn't in `roles`. Role-only for now; scope checks (which
    cloud/account/region a user may act on) are a later authorization
    layer, not implemented in this phase.
    """
    def _check(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return _check
