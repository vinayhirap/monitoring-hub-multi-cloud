# app/api/admin/users.py
from fastapi import APIRouter, HTTPException, Body, Depends
from app.db import get_connection
from app.auth.deps import require_role
from app.auth.permissions import require_permission
from app.auth import authorization as authz
from app.email import mailer
import bcrypt
import datetime
import json
import secrets

router = APIRouter(prefix="/api/users", tags=["Users"])


def _hash_password(password: str) -> str:
    # Raw bcrypt, not passlib \u2014 passlib's bcrypt backend detection is
    # broken with the installed bcrypt version here (confirmed: raises
    # ValueError, NOT the ImportError the old code only caught for \u2014
    # meaning create_user() was silently 500ing before this fix).
    return bcrypt.hashpw(password[:72].encode(), bcrypt.gensalt()).decode()


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
        conn = get_connection()
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


def _account_ids_by_cloud(conn) -> dict:
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, provider FROM aws_accounts")
    rows = cursor.fetchall()
    cursor.close()
    result = {"aws": set(), "azure": set(), "gcp": set()}
    for r in rows:
        result.setdefault(r["provider"], set()).add(r["id"])
    return result


def _fetch_user(conn, user_id: int):
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, username, role FROM users WHERE id = %s", (user_id,))
    row = cursor.fetchone()
    cursor.close()
    return row


def _user_manageable_by(actor: dict, target_user: dict) -> bool:
    """
    Can `actor` manage (view/modify access for, delete) `target_user`?
    Admin: yes, always (self-protection for delete/role-change is
    enforced separately at the route level).
    Editor: only if target is a viewer AND every one of the target's
    CURRENT scope grants is already within the editor's own effective
    scope. Re-checked live on every call \u2014 if an admin later grants
    that viewer something outside this editor's scope, the editor
    immediately loses the ability to manage them, rather than that
    being a one-time check that goes stale.
    """
    if actor["role"] == "admin":
        return True
    if actor["role"] != "editor" or target_user["role"] != "viewer":
        return False

    actor_scope = authz.get_effective_scope(actor)
    target_scope = authz.get_effective_scope(target_user)
    if target_scope == authz.FULL_ACCESS:
        return False  # shouldn't happen for a viewer, but never trust it
    if not target_scope:
        return True  # a viewer with zero scope is trivially "within" anything

    target_as_dicts = [
        {
            "cloud": g.cloud, "account_ref_id": g.account_ref_id,
            "regions": g.regions, "resource_groups": g.resource_groups,
            "resource_types": g.resource_types, "resource_ids": g.resource_ids,
        }
        for g in target_scope
    ]
    return authz.scope_within(target_as_dicts, actor_scope)


def _validate_and_insert_scopes(conn, user_id: int, scopes: list, actor: dict, actor_scope) -> list:
    """
    Validates each requested scope dict (structure, referential
    integrity against real accounts, and \u2014 for non-admin actors \u2014
    containment within the actor's own effective scope), then inserts
    them. Raises HTTPException on the first problem; nothing is
    inserted if any scope in the batch is invalid (all-or-nothing).
    """
    if not scopes:
        return []

    valid_accounts = _account_ids_by_cloud(conn)
    for s in scopes:
        err = authz.validate_scope_shape(s, valid_accounts)
        if err:
            raise HTTPException(status_code=400, detail=f"Invalid scope: {err}")

    if actor["role"] != "admin":
        if not authz.scope_within(scopes, actor_scope):
            raise HTTPException(
                status_code=403,
                detail="Cannot grant access outside your own assigned scope",
            )

    cursor = conn.cursor()
    inserted_ids = []
    for s in scopes:
        cursor.execute(
            "INSERT INTO access_scopes "
            "(user_id, cloud, account_ref_id, regions, resource_groups, resource_types, resource_ids, granted_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                user_id, s["cloud"], s.get("account_ref_id"),
                json.dumps(s["regions"]) if s.get("regions") else None,
                json.dumps(s["resource_groups"]) if s.get("resource_groups") else None,
                json.dumps(s["resource_types"]) if s.get("resource_types") else None,
                json.dumps(s["resource_ids"]) if s.get("resource_ids") else None,
                actor["id"],
            ),
        )
        inserted_ids.append(cursor.lastrowid)
    conn.commit()
    cursor.close()
    return inserted_ids


# Every endpoint below requires an authenticated admin OR editor.
# Fine-grained bounds (what an editor may see/create/delete) are
# enforced inside each function via authz.can_manage_role /
# _user_manageable_by / authz.scope_within \u2014 never by trusting
# anything the client sent about its own permissions.


@router.get("")
def list_users(current_user: dict = Depends(require_role("admin", "editor"))):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, username, role, created_at FROM users ORDER BY created_at ASC")
    rows = cursor.fetchall()

    if current_user["role"] == "admin":
        cursor.close()
        conn.close()
        return [_serialize(r) for r in rows]

    # Editor: only viewers they can actually manage.
    visible = [r for r in rows if _user_manageable_by(current_user, r)]
    cursor.close()
    conn.close()
    return [_serialize(r) for r in visible]


@router.post("")
def create_user(payload: dict = Body(...), current_user: dict = Depends(require_role("admin", "editor"))):
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()
    role     = (payload.get("role") or "viewer").strip().lower()
    scopes   = payload.get("scopes") or []
    email    = (payload.get("email") or "").strip() or None

    if not username:
        raise HTTPException(status_code=400, detail="username required")
    if not password or len(password) < 6:
        raise HTTPException(status_code=400, detail="password min 6 characters")
    if role not in ["admin", "editor", "viewer"]:
        raise HTTPException(status_code=400, detail="role must be admin, editor, or viewer")

    if not authz.can_manage_role(current_user, role):
        raise HTTPException(
            status_code=403,
            detail="Editors may only create viewer accounts" if current_user["role"] == "editor"
            else "Insufficient permissions to assign this role",
        )

    if current_user["role"] == "editor" and not scopes:
        raise HTTPException(
            status_code=400,
            detail="Editors must specify at least one scope when creating a viewer "
                   "(a viewer with no scope has no purpose and is refused rather than silently created)",
        )

    pw_hash = _hash_password(password)
    conn    = get_connection()
    cursor  = conn.cursor()

    try:
        cursor.execute(
            "INSERT INTO users (username, password, role, email) VALUES (%s, %s, %s, %s)",
            (username, pw_hash, role, email)
        )
        conn.commit()
        new_id = cursor.lastrowid
    except Exception as e:
        if "Duplicate" in str(e) or "1062" in str(e):
            raise HTTPException(status_code=409, detail=f"User '{username}' already exists")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()

    actor_scope = authz.get_effective_scope(current_user)
    try:
        _validate_and_insert_scopes(conn, new_id, scopes, current_user, actor_scope)
    except HTTPException:
        # Roll back the just-created user rather than leave an
        # orphaned account with no valid scope.
        cleanup = conn.cursor()
        cleanup.execute("DELETE FROM users WHERE id = %s", (new_id,))
        conn.commit()
        cleanup.close()
        conn.close()
        raise
    conn.close()

    _write_audit(
        actor=current_user["username"], action="User created",
        detail=f"{username} added as {role.upper()} with {len(scopes)} scope grant(s)",
    )

    # Welcome email with a set-your-password link, not the raw
    # password -- reuses the exact same password_reset_tokens flow as
    # /api/auth/forgot-password rather than a separate mechanism, and
    # never puts a plaintext credential in an email body/inbox. A
    # no-op (logged, not raised) if SMTP isn't configured or the user
    # has no email on file -- account creation itself already
    # succeeded above and must not be undone by a mail failure.
    email_sent = False
    if email and mailer.is_configured():
        token      = secrets.token_urlsafe(32)
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=60 * 24)
        mail_conn  = get_connection()
        mail_cur   = mail_conn.cursor()
        mail_cur.execute(
            "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (%s, %s, %s)",
            (new_id, token, expires_at),
        )
        mail_conn.commit()
        mail_cur.close()
        mail_conn.close()

        reset_link = f"{mailer.get_public_app_url()}/reset-password?token={token}"
        email_sent = mailer.send_email(
            to_addr=email,
            subject="Your CloudOps account has been created",
            body_text=(
                f"Hi {username},\n\n"
                f"An account has been created for you on CloudOps with the role: {role.upper()}.\n\n"
                f"Set your password (link valid 24 hours):\n{reset_link}\n\n"
                f"If you weren't expecting this, contact your CloudOps administrator.\n"
            ),
        )

    return {
        "status": "created", "id": new_id, "username": username, "role": role,
        "scopes_granted": len(scopes), "email_sent": email_sent,
    }


@router.patch("/{user_id}/role")
def update_role(user_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("roles.manage"))):
    # Role changes stay admin-only by design: an editor's authority is
    # to manage VIEWER accounts within their scope, not to change what
    # role anyone holds (including promoting a viewer they manage into
    # an editor, which would be a role-hierarchy change, not a scope
    # delegation).
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


@router.get("/{user_id}/access")
def get_user_access(user_id: int, current_user: dict = Depends(require_role("admin", "editor"))):
    conn = get_connection()
    target = _fetch_user(conn, user_id)
    if not target:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    if not _user_manageable_by(current_user, target):
        conn.close()
        raise HTTPException(status_code=403, detail="You do not have access to this user's scope")
    conn.close()
    return authz.serialize_scope(target)


@router.post("/{user_id}/access")
def add_user_access(user_id: int, payload: dict = Body(...), current_user: dict = Depends(require_role("admin", "editor"))):
    scopes = payload.get("scopes") or []
    if not scopes:
        raise HTTPException(status_code=400, detail="scopes required")

    conn = get_connection()
    target = _fetch_user(conn, user_id)
    if not target:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    if not _user_manageable_by(current_user, target):
        conn.close()
        raise HTTPException(status_code=403, detail="You do not have access to manage this user's scope")

    actor_scope = authz.get_effective_scope(current_user)
    inserted_ids = _validate_and_insert_scopes(conn, user_id, scopes, current_user, actor_scope)
    conn.close()

    _write_audit(
        actor=current_user["username"], action="Access granted",
        detail=f"{target['username']}: +{len(inserted_ids)} scope grant(s)",
    )
    return {"status": "updated", "user_id": user_id, "scope_ids": inserted_ids}


@router.delete("/access/{scope_id}")
def revoke_access_scope(scope_id: int, current_user: dict = Depends(require_role("admin", "editor"))):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT s.id, s.user_id, u.username, u.role FROM access_scopes s "
        "JOIN users u ON u.id = s.user_id WHERE s.id = %s",
        (scope_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Scope grant not found")

    target = {"id": row["user_id"], "username": row["username"], "role": row["role"]}
    if not _user_manageable_by(current_user, target):
        conn.close()
        raise HTTPException(status_code=403, detail="You do not have access to manage this user's scope")

    cursor = conn.cursor()
    cursor.execute("DELETE FROM access_scopes WHERE id = %s", (scope_id,))
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(actor=current_user["username"], action="Access revoked", detail=f"{target['username']}: scope #{scope_id} removed")
    return {"status": "revoked", "scope_id": scope_id}


@router.delete("/{user_id}")
def delete_user(user_id: int, current_user: dict = Depends(require_role("admin", "editor"))):
    if current_user["id"] == user_id:
        raise HTTPException(status_code=403, detail="Cannot delete your own account")

    conn = get_connection()
    target = _fetch_user(conn, user_id)
    if not target:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    if not _user_manageable_by(current_user, target):
        conn.close()
        raise HTTPException(status_code=403, detail="You do not have access to delete this user")

    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))  # access_scopes rows cascade via FK
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(actor=current_user["username"], action="User deleted", detail=f"{target['username']} removed")
    return {"status": "deleted", "id": user_id, "username": target["username"]}
