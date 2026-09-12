#!/usr/bin/env python3
"""
apply_phase0_jwt_auth.py — Phase 0 of the RBAC project: real sessions.

BEFORE this patch, "authentication" in this app worked like this:
  - POST /api/auth/login checked the password, then returned
    {id, username, role} in the response body — no token, no signature.
  - The frontend saved that raw JSON in localStorage. Editing
    localStorage in devtools to {"role":"admin"} made the frontend
    treat you as admin — nothing server-side checked it.
  - The ONLY backend authorization check anywhere was in
    app/api/admin/users.py, and it trusted a client-supplied `actor_id`
    field in the POST body — exactly as spoofable, one layer deeper.
    create_user() didn't even do that: it had NO check at all.
  - Every other router (alerts, admin/accounts, live_data, audit_logs,
    settings, metric_catalog) had zero authentication of any kind.
  - app/auth/security.py and app/auth/deps.py already existed but were
    DEAD CODE: never imported anywhere, broken (shadowed `import jwt`
    with `from jose import jwt` while python-jose isn't even a
    dependency), and had a hardcoded fallback secret "dev-fallback-secret".

AFTER this patch:
  - /api/auth/login sets a real, signed JWT in an httpOnly, SameSite=Lax
    cookie (`mh_session`). httpOnly means client-side JS (and therefore
    any XSS bug) can't read or forge it, unlike localStorage.
  - Every request to a protected route is verified server-side via
    app.auth.deps.get_current_user — no client-supplied identity field
    is trusted anywhere anymore.
  - app/api/admin/users.py now requires an authenticated admin (via the
    verified session, not a body field) on every route, including
    create_user which previously had NO check.
  - CORS no longer uses allow_origins=["*"] (which browsers reject
    outright when combined with credentials — it would have silently
    broken cookie-based auth) — origins are now explicit and
    env-driven (CORS_ALLOWED_ORIGINS).
  - New endpoints: POST /api/auth/logout, GET /api/auth/me.
  - app/auth/security.py + app/auth/deps.py are rewritten to be real,
    working, actually-imported modules (PyJWT, no insecure fallback
    secret — JWT_SECRET is required or the app refuses to start).

WHAT THIS PATCH DOES NOT DO (future phases):
  - No cloud/account/region SCOPE enforcement yet — this is role-only
    (admin/editor/viewer). Scope-based RBAC is Phase 1+.
  - Frontend is NOT touched by this script (by your choice: backend
    fully done and tested first). Until the frontend is updated to (a)
    send `credentials: 'include'` on fetch calls and (b) stop relying
    on its own localStorage copy for authorization decisions, the UI
    WILL start getting 401s from every protected endpoint after this
    patch + restart. That's expected — it means the gate is working.
  - The /ws/{channel} WebSocket endpoint is still unauthenticated.
    Flagging as a known gap for a later pass, not silently ignoring it.

REQUIRED MANUAL STEP before restarting uvicorn — add to your local .env
(and to .env.production on the server later):

    JWT_SECRET=<output of: python -c "import secrets; print(secrets.token_hex(32))">

The app will raise a clear RuntimeError on first use if this is missing
— it will NOT silently fall back to an insecure default.

Usage:
    python apply_phase0_jwt_auth.py --dry-run
    python apply_phase0_jwt_auth.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-phase0-jwt-auth"


class PatchError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────
# Full-file rewrites (dead/broken files, or new files) — (path, expected
# anchor substring to confirm we're replacing the right thing / already
# applied, full new content)
# ─────────────────────────────────────────────────────────────────────────

SECURITY_PY_OLD_ANCHOR = "dev-fallback-secret"
SECURITY_PY_NEW = '''"""
app/auth/security.py

Password hashing + JWT session tokens.

JWT_SECRET must be set in the environment — there is deliberately NO
insecure fallback default. A shared/predictable default secret would
let anyone forge a valid admin session token, which defeats every
authorization check built on top of this module.
"""
import os
from datetime import datetime, timedelta

import bcrypt
import jwt

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 12 * 60  # 12 hours


def _get_secret() -> str:
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise RuntimeError(
            "JWT_SECRET is not set. Generate one and add it to your .env file:\\n"
            "  python -c \\"import secrets; print(secrets.token_hex(32))\\"\\n"
            "then add JWT_SECRET=<the printed value> to .env (and .env.production on the server)."
        )
    return secret


def hash_password(password: str) -> str:
    # Raw bcrypt, not passlib — passlib's bcrypt backend detection is
    # broken with modern bcrypt versions (the same class of issue the
    # deployment log already hit once; app/api/auth.py avoids passlib
    # for this exact reason).
    return bcrypt.hashpw(password[:72].encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password[:72].encode(), hashed_password.encode())
    except Exception:
        return False


def create_access_token(user_id: int, username: str, role: str,
                         expires_minutes: int = ACCESS_TOKEN_EXPIRE_MINUTES) -> str:
    now = datetime.utcnow()
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes),
    }
    return jwt.encode(payload, _get_secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Raises jwt.PyJWTError (ExpiredSignatureError / InvalidTokenError / etc.) on failure."""
    payload = jwt.decode(token, _get_secret(), algorithms=[ALGORITHM])
    return {
        "id": int(payload["sub"]),
        "username": payload["username"],
        "role": payload["role"],
    }
'''

DEPS_PY_OLD_ANCHOR = "require_account_access"
DEPS_PY_NEW = '''"""
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
'''

INIT_PY_NEW = "# app/auth package\n"

FULL_REWRITES = [
    ("app/auth/security.py", SECURITY_PY_OLD_ANCHOR, SECURITY_PY_NEW),
    ("app/auth/deps.py", DEPS_PY_OLD_ANCHOR, DEPS_PY_NEW),
]

# ─────────────────────────────────────────────────────────────────────────
# app/api/admin/users.py — full rewrite (many small changes throughout)
# ─────────────────────────────────────────────────────────────────────────
USERS_PY_OLD_ANCHOR = "def _require_admin(user_id: int, conn):"
USERS_PY_NEW = '''# app/api/admin/users.py
from fastapi import APIRouter, HTTPException, Body, Depends
from app.db import get_connection
from app.auth.deps import require_role
import datetime
import json
import hashlib

router = APIRouter(prefix="/api/users", tags=["Users"])


def _hash_password(password: str) -> str:
    try:
        from passlib.context import CryptContext
        ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
        return ctx.hash(password)
    except ImportError:
        return hashlib.sha256(password.encode()).hexdigest()


def _serialize(obj):
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    return obj


def _write_audit(actor: str, action: str, detail: str, role: str = "ADMIN"):
    try:
        conn   = get_connection()
        cursor = conn.cursor()
        payload = json.dumps({"detail": detail, "role": role})
        cursor.execute(
            "INSERT INTO audit_logs (actor, action, payload) VALUES (%s, %s, %s)",
            (actor, action, payload)
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Audit log write error: {e}")


# Every endpoint below requires an authenticated admin. Identity comes
# from the verified session (app.auth.deps), never from a
# client-supplied field — the previous design trusted a client-supplied
# actor_id in the request body, and create_user() had no check at all.
#
# NOTE: per the scope-based RBAC plan, Editors should eventually manage
# Viewers within their own assigned scope. That needs the access-scope
# model first (a later phase) — this router is admin-only for now,
# which is a strict tightening vs. before, not a regression.


@router.get("")
def list_users(current_user: dict = Depends(require_role("admin"))):
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, username, role, created_at FROM users ORDER BY created_at ASC")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [_serialize(r) for r in rows]


@router.post("")
def create_user(payload: dict = Body(...), current_user: dict = Depends(require_role("admin"))):
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()
    role     = (payload.get("role") or "viewer").strip().lower()

    if not username:
        raise HTTPException(status_code=400, detail="username required")
    if not password or len(password) < 6:
        raise HTTPException(status_code=400, detail="password min 6 characters")
    if role not in ["admin", "editor", "viewer"]:
        raise HTTPException(status_code=400, detail="role must be admin, editor, or viewer")

    pw_hash = _hash_password(password)
    conn    = get_connection()
    cursor  = conn.cursor()

    cursor.execute("SHOW COLUMNS FROM users LIKE 'password%'")
    col    = cursor.fetchone()
    pw_col = col[0] if col else "password_hash"

    try:
        cursor.execute(
            f"INSERT INTO users (username, {pw_col}, role) VALUES (%s, %s, %s)",
            (username, pw_hash, role)
        )
        conn.commit()
        new_id = cursor.lastrowid
    except Exception as e:
        if "Duplicate" in str(e) or "1062" in str(e):
            raise HTTPException(status_code=409, detail=f"User '{username}' already exists")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conn.close()

    _write_audit(actor=current_user["username"], action="User created", detail=f"{username} added as {role.upper()}")
    return {"status": "created", "id": new_id, "username": username, "role": role}


@router.patch("/{user_id}/role")
def update_role(user_id: int, payload: dict = Body(...), current_user: dict = Depends(require_role("admin"))):
    new_role = (payload.get("role") or "").strip().lower()

    if new_role not in ["admin", "editor", "viewer"]:
        raise HTTPException(status_code=400, detail="role must be admin, editor, or viewer")

    if current_user["id"] == user_id:
        raise HTTPException(status_code=403, detail="Cannot change your own role")

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT username FROM users WHERE id = %s", (user_id,))
    user = cursor.fetchone()
    if not user:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    cursor.execute("UPDATE users SET role = %s WHERE id = %s", (new_role, user_id))
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(actor=current_user["username"], action="Role changed", detail=f"{user['username']} \u2192 {new_role.upper()}")
    return {"status": "updated", "id": user_id, "role": new_role}


@router.patch("/{user_id}/accounts")
def update_account_access(user_id: int, payload: dict = Body(...), current_user: dict = Depends(require_role("admin"))):
    account_ids = payload.get("account_ids", [])

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT username FROM users WHERE id = %s", (user_id,))
    user = cursor.fetchone()
    if not user:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    try:
        cursor.execute("DELETE FROM user_account_access WHERE user_id = %s", (user_id,))
        for acc_id in account_ids:
            cursor.execute(
                "INSERT IGNORE INTO user_account_access (user_id, aws_account_id) VALUES (%s, %s)",
                (user_id, acc_id)
            )
        conn.commit()
    except Exception:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_account_access (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                aws_account_id BIGINT NOT NULL,
                UNIQUE KEY uniq_user_account (user_id, aws_account_id)
            )
        """)
        conn.commit()
        for acc_id in account_ids:
            cursor.execute(
                "INSERT IGNORE INTO user_account_access (user_id, aws_account_id) VALUES (%s, %s)",
                (user_id, acc_id)
            )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    _write_audit(actor=current_user["username"], action="Account access updated", detail=f"{user['username']} access: {account_ids}")
    return {"status": "updated", "user_id": user_id, "account_ids": account_ids}


@router.delete("/{user_id}")
def delete_user(user_id: int, current_user: dict = Depends(require_role("admin"))):
    if current_user["id"] == user_id:
        raise HTTPException(status_code=403, detail="Cannot delete your own account")

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT username FROM users WHERE id = %s", (user_id,))
    user = cursor.fetchone()
    if not user:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(actor=current_user["username"], action="User deleted", detail=f"{user['username']} removed")
    return {"status": "deleted", "id": user_id, "username": user["username"]}
'''

FULL_REWRITES.append(("app/api/admin/users.py", USERS_PY_OLD_ANCHOR, USERS_PY_NEW))


def _write_new_file_if_missing(rel_path: str, content: str, dry_run: bool, report: list):
    full_path = REPO_ROOT / rel_path
    if full_path.exists():
        return
    if dry_run:
        report.append(f"[DRY RUN] would create: {rel_path}")
    else:
        full_path.write_text(content, encoding="utf-8")
        report.append(f"CREATED: {rel_path}")


# ─────────────────────────────────────────────────────────────────────────
# Anchor-based partial patches
# ─────────────────────────────────────────────────────────────────────────
PATCHES = []

# ── app/api/auth.py ────────────────────────────────────────────────────
AUTH_IMPORTS_OLD = '''# app/api/auth.py
from fastapi import APIRouter, HTTPException, Body
from app.db import get_connection
import bcrypt
import logging
import secrets
import json
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["Auth"])

RESET_TOKEN_TTL_MINUTES = 30'''

AUTH_IMPORTS_NEW = '''# app/api/auth.py
import os

from fastapi import APIRouter, HTTPException, Body, Response, Depends
from app.db import get_connection
from app.auth.security import create_access_token
from app.auth.deps import get_current_user, COOKIE_NAME
import bcrypt
import logging
import secrets
import json
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["Auth"])

RESET_TOKEN_TTL_MINUTES = 30

# COOKIE_SECURE must be "true" once the app is served over HTTPS (see the
# Security Checklist in the deployment guide). Defaults to False because
# production currently serves plain HTTP on port 80 — a Secure cookie
# would silently never be sent by the browser over HTTP, breaking login.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").strip().lower() == "true"
COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60  # 12 hours, matches token expiry'''

PATCHES.append(("app/api/auth.py", [(AUTH_IMPORTS_OLD, AUTH_IMPORTS_NEW)]))

AUTH_LOGIN_OLD = '''@router.post("/login")
def login(payload: dict = Body(...)):
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, username, role, password AS pw FROM users WHERE username = %s AND active = 1",
        (username,)
    )
    user = cursor.fetchone()
    cursor.close()
    conn.close()

    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not _verify_password(password, user["pw"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return {
        "id":       user["id"],
        "username": user["username"],
        "role":     user["role"],
    }'''

AUTH_LOGIN_NEW = '''@router.post("/login")
def login(response: Response, payload: dict = Body(...)):
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, username, role, password AS pw FROM users WHERE username = %s AND active = 1",
        (username,)
    )
    user = cursor.fetchone()
    cursor.close()
    conn.close()

    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not _verify_password(password, user["pw"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token(user["id"], user["username"], user["role"])
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=COOKIE_MAX_AGE_SECONDS,
        path="/",
    )

    return {
        "id":       user["id"],
        "username": user["username"],
        "role":     user["role"],
    }


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.get("/me")
def me(current_user: dict = Depends(get_current_user)):
    return current_user'''

PATCHES.append(("app/api/auth.py", [(AUTH_LOGIN_OLD, AUTH_LOGIN_NEW)]))

AUTH_CHANGEPW_OLD = '''@router.post("/change-password")
def change_password(payload: dict = Body(...)):
    """Self-service change password — requires the current password."""
    username     = (payload.get("username") or "").strip()
    current_pw   = (payload.get("current_password") or "").strip()
    new_pw       = (payload.get("new_password") or "").strip()

    if not username or not current_pw or not new_pw:
        raise HTTPException(status_code=400, detail="username, current_password and new_password are required")
    if len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")'''

AUTH_CHANGEPW_NEW = '''@router.post("/change-password")
def change_password(payload: dict = Body(...), current_user: dict = Depends(get_current_user)):
    """
    Self-service change password — requires the current password AND a
    valid session. Always acts on the SESSION's identity, never a
    client-supplied username, so a logged-in user can never target
    another account's password by passing a different username field.
    """
    username     = current_user["username"]
    current_pw   = (payload.get("current_password") or "").strip()
    new_pw       = (payload.get("new_password") or "").strip()

    if not current_pw or not new_pw:
        raise HTTPException(status_code=400, detail="current_password and new_password are required")
    if len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")'''

PATCHES.append(("app/api/auth.py", [(AUTH_CHANGEPW_OLD, AUTH_CHANGEPW_NEW)]))

# ── app/main.py ─────────────────────────────────────────────────────────
MAIN_IMPORTS_OLD = '''from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import logging
import threading

from app.api.alerts         import router as alerts_router
from app.api.admin.accounts import router as admin_accounts_router
from app.api.auth           import router as auth_router
from app.api.admin.users    import router as admin_users_router
from app.api.settings       import router as settings_router
from app.api.live_data      import router as live_data_router
from app.api.audit_logs     import router as audit_logs_router
from app.api.metric_catalog import router as metric_catalog_router'''

MAIN_IMPORTS_NEW = '''from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import logging
import os
import threading

from app.api.alerts         import router as alerts_router
from app.api.admin.accounts import router as admin_accounts_router
from app.api.auth           import router as auth_router
from app.api.admin.users    import router as admin_users_router
from app.api.settings       import router as settings_router
from app.api.live_data      import router as live_data_router
from app.api.audit_logs     import router as audit_logs_router
from app.api.metric_catalog import router as metric_catalog_router
from app.auth.deps          import get_current_user'''

PATCHES.append(("app/main.py", [(MAIN_IMPORTS_OLD, MAIN_IMPORTS_NEW)]))

MAIN_CORS_OLD = '''app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)'''

MAIN_CORS_NEW = '''_cors_origins_env = os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
CORS_ALLOWED_ORIGINS = [o.strip() for o in _cors_origins_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    # Explicit origins are required here (not "*") because credentialed
    # (cookie-based) requests need the browser to see its own exact
    # origin echoed back in the response — wildcard + credentials is
    # rejected by browsers outright and would silently break session
    # cookies. Configure via CORS_ALLOWED_ORIGINS in .env.
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)'''

PATCHES.append(("app/main.py", [(MAIN_CORS_OLD, MAIN_CORS_NEW)]))

MAIN_ROUTERS_OLD = '''app.include_router(alerts_router,        prefix="/api")
app.include_router(admin_accounts_router)
app.include_router(auth_router)
app.include_router(admin_users_router)
app.include_router(live_data_router)
app.include_router(audit_logs_router)
app.include_router(settings_router)
app.include_router(metric_catalog_router)'''

MAIN_ROUTERS_NEW = '''# auth_router stays public (it contains /login itself; /me and
# /change-password enforce auth per-route internally). admin_users_router
# enforces admin-only per-route internally (app/api/admin/users.py).
# Every other router below requires a valid session at minimum — more
# specific role/scope checks are a later authorization phase.
_auth_dep = [Depends(get_current_user)]

app.include_router(alerts_router,         prefix="/api", dependencies=_auth_dep)
app.include_router(admin_accounts_router, dependencies=_auth_dep)
app.include_router(auth_router)
app.include_router(admin_users_router)
app.include_router(live_data_router,      dependencies=_auth_dep)
app.include_router(audit_logs_router,     dependencies=_auth_dep)
app.include_router(settings_router,       dependencies=_auth_dep)
app.include_router(metric_catalog_router, dependencies=_auth_dep)'''

PATCHES.append(("app/main.py", [(MAIN_ROUTERS_OLD, MAIN_ROUTERS_NEW)]))

# ── requirements.txt ───────────────────────────────────────────────────
REQ_OLD = '''bcrypt
passlib[bcrypt]
requests==2.32.3'''

REQ_NEW = '''bcrypt
passlib[bcrypt]
PyJWT==2.10.1
requests==2.32.3'''

PATCHES.append(("requirements.txt", [(REQ_OLD, REQ_NEW)]))

# ── .env.production.example ────────────────────────────────────────────
ENV_EXAMPLE_OLD = '''VM_URL=http://3.109.181.40'''

ENV_EXAMPLE_NEW = '''VM_URL=http://3.109.181.40

# Session auth (Phase 0 RBAC prerequisite)
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
JWT_SECRET=CHANGE_ME_GENERATE_A_RANDOM_SECRET
# Set to true once the app is served over HTTPS (see Security Checklist)
COOKIE_SECURE=false
# Comma-separated origins allowed to send credentialed (cookie) requests.
# In production behind Nginx (frontend+API same origin) this can just be
# the app's own URL; the defaults here cover local dev.
CORS_ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173'''

PATCHES.append((".env.production.example", [(ENV_EXAMPLE_OLD, ENV_EXAMPLE_NEW)]))


def preflight():
    print("=== Pre-flight: verifying anchors match exactly ===")
    problems = []

    for rel_path, old_anchor, _new in FULL_REWRITES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        if old_anchor not in text:
            problems.append(f"{rel_path}: expected anchor '{old_anchor}' not found")
        else:
            print(f"  OK  {rel_path}: ready for full rewrite")

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        for old, _new in replacements:
            count = text.count(old)
            if count == 0:
                problems.append(f"{rel_path}: anchor not found (0 matches)")
            elif count > 1:
                problems.append(f"{rel_path}: anchor matched {count} times, expected exactly 1")
            else:
                print(f"  OK  {rel_path}: anchor matched exactly once")

    if problems:
        print("\n".join(problems))
        # Idempotency check: is the new text already present everywhere?
        def _already(rel, new_text):
            p = REPO_ROOT / rel
            return p.exists() and new_text in p.read_text(encoding="utf-8")

        already_applied = all(_already(rel, new) for rel, _anchor, new in FULL_REWRITES) and all(
            _already(rel, new) for rel, repls in PATCHES for _old, new in repls
        )
        if already_applied:
            print("\nAll target text already present — patch appears to be already applied. Nothing to do.")
            sys.exit(0)
        raise PatchError("Pre-flight failed — aborting before touching any file. See problems above.")
    print("Pre-flight OK.\n")


def apply_all(dry_run: bool):
    changed_files = []
    report = []

    for rel_path, _old_anchor, new_content in FULL_REWRITES:
        full_path = REPO_ROOT / rel_path
        if dry_run:
            report.append(f"[DRY RUN] would fully rewrite: {rel_path}")
        else:
            backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
            if not backup_path.exists():
                shutil.copy2(full_path, backup_path)
            full_path.write_text(new_content, encoding="utf-8")
            report.append(f"REWROTE: {rel_path}  (backup: {backup_path.name})")
            changed_files.append(full_path)

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        text = full_path.read_text(encoding="utf-8")
        original_text = text
        for old, new in replacements:
            if old not in text:
                raise PatchError(f"{rel_path}: expected anchor vanished mid-patch — aborting")
            text = text.replace(old, new, 1)

        if text == original_text:
            continue

        if dry_run:
            report.append(f"[DRY RUN] would patch: {rel_path}")
        else:
            backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
            if not backup_path.exists():
                shutil.copy2(full_path, backup_path)
            full_path.write_text(text, encoding="utf-8")
            report.append(f"PATCHED: {rel_path}  (backup: {backup_path.name})")
            changed_files.append(full_path)

    _write_new_file_if_missing("app/auth/__init__.py", INIT_PY_NEW, dry_run, report)

    for line in report:
        print(line)

    return changed_files


def validate_python_syntax(changed_files):
    print("\n=== Validating Python syntax (py_compile) ===")
    for f in changed_files:
        if f.suffix != ".py":
            continue
        try:
            py_compile.compile(str(f), doraise=True)
            print(f"  OK  {f.relative_to(REPO_ROOT)}")
        except py_compile.PyCompileError as e:
            raise PatchError(f"SYNTAX ERROR after patching {f}: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        preflight()
        changed = apply_all(args.dry_run)
        if not args.dry_run:
            validate_python_syntax(changed)
            print(f"\n=== Done. {len(changed)} file(s) touched. ===")
            print("\nREQUIRED before restarting:")
            print('  1. Add to your local .env: JWT_SECRET=<run: python -c "import secrets; print(secrets.token_hex(32))">')
            print("  2. pip install -r requirements.txt   (installs PyJWT)")
            print("  3. Full uvicorn restart (not --reload)")
            print("\nExpected: the frontend WILL start getting 401s on protected endpoints")
            print("until the frontend patch (next stage) sends credentials with requests.")
            print("That's the gate working correctly, not a bug.")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
