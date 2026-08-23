# app/api/admin/users.py
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

    _write_audit(actor=current_user["username"], action="Role changed", detail=f"{user['username']} → {new_role.upper()}")
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
