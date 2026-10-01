# app/api/admin/users.py
from fastapi import APIRouter, HTTPException, Body, Depends
from app.db import get_connection
from app.auth.permissions import require_permission
from app.auth import authorization as authz
from app.auth.security import hash_password
from app.email import mailer
import datetime
from app.utils.time_json import to_utc_iso
import hashlib
import json
import re
import secrets

router = APIRouter(prefix="/api/users", tags=["Users"])


# Audit B01 follow-up: this used to call bcrypt directly with its own
# gensalt() and password[:72].encode() truncation -- a second, independent
# implementation of exactly what app/auth/security.py already does (and
# tests), so it silently diverged: it truncated by slicing the STRING to 72
# rather than encoding then truncating BYTES (wrong on any password with a
# multi-byte character near that boundary), and its cost was hardcoded
# instead of following BCRYPT_ROUNDS. Reusing security.hash_password() here
# means every password in this codebase is hashed exactly one way.
def _hash_password(password: str) -> str:
    return hash_password(password)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _serialize(obj):
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return to_utc_iso(obj)
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    return obj


from app.audit import write_audit as _write_audit
# NOTE: previously a local copy defaulting role="ADMIN" whenever a
# caller didn't pass one explicitly. Three call sites below ("Role
# changed", "Access revoked", "User deleted") relied on that default
# silently -- despite all three endpoints being reachable by
# require_role("admin", "editor"), so an editor performing any of them
# had the action misattributed to "ADMIN" in the compliance audit
# trail. Fixed by passing role=current_user["role"].upper() explicitly
# at every call site below.


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
def list_users(current_user: dict = Depends(require_permission("users.view"))):
    """
    Enriched user list: everything the Users table needs in ONE round trip
    (status, last sign-in, group names, direct scope grants and v2 role
    bindings) instead of the UI issuing N follow-up calls per row.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, username, role, email, active, created_at, last_login_at, deactivated_at "
            "FROM users ORDER BY created_at ASC"
        )
        rows = cursor.fetchall()

        if current_user["role"] != "admin":
            # Editor: only viewers they can actually manage.
            rows = [r for r in rows if _user_manageable_by(current_user, r)]

        ids = [r["id"] for r in rows]
        groups_by_user, scopes_by_user, bindings_by_user = {}, {}, {}
        if ids:
            ph = ",".join(["%s"] * len(ids))
            cursor.execute(
                "SELECT ugm.user_id, g.name FROM user_group_memberships ugm "
                "JOIN org_groups g ON g.id = ugm.group_id "
                f"WHERE ugm.user_id IN ({ph}) ORDER BY g.name",
                tuple(ids),
            )
            for r in cursor.fetchall():
                groups_by_user.setdefault(r["user_id"], []).append(r["name"])

            cursor.execute(
                f"SELECT user_id, COUNT(*) AS n FROM access_scopes WHERE user_id IN ({ph}) GROUP BY user_id",
                tuple(ids),
            )
            scopes_by_user = {r["user_id"]: r["n"] for r in cursor.fetchall()}

            cursor.execute(
                "SELECT principal_id, COUNT(*) AS n FROM role_bindings "
                f"WHERE principal_type = 'user' AND principal_id IN ({ph}) "
                "AND (expires_at IS NULL OR expires_at > NOW()) GROUP BY principal_id",
                tuple(ids),
            )
            bindings_by_user = {r["principal_id"]: r["n"] for r in cursor.fetchall()}
        cursor.close()

        out = []
        for r in rows:
            r["active"] = bool(r["active"]) if r["active"] is not None else True
            r["groups"] = groups_by_user.get(r["id"], [])
            r["scope_grants"] = scopes_by_user.get(r["id"], 0)
            r["role_bindings"] = bindings_by_user.get(r["id"], 0)
            r["is_self"] = r["id"] == current_user["id"]
            out.append(_serialize(r))
        return out
    finally:
        conn.close()


@router.post("")
def create_user(payload: dict = Body(...), current_user: dict = Depends(require_permission("users.create"))):
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()
    role     = (payload.get("role") or "viewer").strip().lower()
    scopes   = payload.get("scopes") or []
    email    = (payload.get("email") or "").strip() or None

    if not username:
        raise HTTPException(status_code=400, detail="username required")
    if len(username) > 100 or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._@+\-]*$", username):
        raise HTTPException(
            status_code=400,
            detail="username may contain letters, digits and . _ @ + - only (max 100, must start with a letter or digit)",
        )
    if not password or len(password) < 8:
        raise HTTPException(status_code=400, detail="password min 8 characters")
    if role not in ["admin", "editor", "viewer"]:
        raise HTTPException(status_code=400, detail="role must be admin, editor, or viewer")
    # SECURITY: only .strip()'d before this fix -- no format check, no
    # rejection of embedded control characters. This value later flows
    # straight into mailer.send_email(to_addr=email, ...) as both the
    # MIME "To" header and the raw SMTP envelope recipient, so an
    # unvalidated value here was a CRLF/header-injection vector (an
    # editor could plant a fake "email" containing a newline to smuggle
    # in extra headers or attempt SMTP command injection). mailer.py's
    # send_email() also independently refuses CR/LF as defense-in-depth,
    # but rejecting here means bad data never even reaches the users
    # table in the first place.
    if email and not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        raise HTTPException(status_code=400, detail="email is not a valid address")

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
        # Ported from a local hotfix found already running on prod
        # (35.154.149.94), never committed to git -- without this,
        # every failed create_user (e.g. a duplicate username, the most
        # common real-world case) leaked a pooled DB connection
        # permanently, the same class of bug that exhausted the pool
        # and took the dashboard offline for hours on Sep 5 2026 (see
        # deploy/update.sh's own verification gate for that incident).
        #
        # rollback() before close() so an aborted INSERT never leaves a
        # dangling transaction on a connection about to go back to
        # (or out of) the pool. The `finally: cursor.close()` below
        # still runs after this -- wrapped in its own try/except there
        # specifically so that IF closing an already-closed connection's
        # cursor ever raises anything connector-version-specific, it
        # can never replace/mask the real HTTPException being raised
        # here with an unrelated one.
        conn.rollback()
        conn.close()
        if "Duplicate" in str(e) or "1062" in str(e):
            raise HTTPException(status_code=409, detail=f"User '{username}' already exists")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cursor.close()
        except Exception:
            pass

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
        role=current_user["role"].upper(),
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
        # Audit B01 follow-up: this token used to be INSERTed raw, unlike
        # every other reset token in the system (app/api/auth.py's
        # /forgot-password hashes with SHA-256 before storing -- see that
        # module's docstring). Anyone with DB/backup read access could use
        # a raw row here to log in as the new user without ever seeing the
        # email. Stored hashed now; app/api/auth.py's /reset-password
        # already accepts either form (it matches on SHA-256(token) OR, for
        # anything that doesn't look like a 64-hex-char hash, the raw
        # value), so the emailed link and the reset flow are unaffected.
        token      = secrets.token_urlsafe(32)
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=60 * 24)
        mail_conn  = get_connection()
        mail_cur   = mail_conn.cursor()
        try:
            mail_cur.execute(
                "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (%s, %s, %s)",
                (new_id, _token_hash(token), expires_at),
            )
            mail_conn.commit()
        except Exception:
            mail_conn.rollback()
            raise
        finally:
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

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT username, role FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        if not user:
            cursor.close()
            raise HTTPException(status_code=404, detail="User not found")

        # SECURITY: last-admin protection. Nothing previously stopped an
        # admin from demoting the only other admin (or themselves being
        # the only one left, if role were ever self-editable), leaving
        # zero accounts able to reach any admin-only route -- including
        # the very users/roles endpoints needed to fix it, with no
        # recovery path short of a direct DB write.
        if user["role"] == "admin" and new_role != "admin":
            from app.auth import principals as _principals
            if _principals.active_admin_count(conn, exclude_user_id=user_id) < 1:
                cursor.close()
                raise HTTPException(
                    status_code=409,
                    detail="Cannot change the role of the last remaining admin",
                )

        cursor.execute("UPDATE users SET role = %s WHERE id = %s", (new_role, user_id))
        conn.commit()
        cursor.close()
    finally:
        conn.close()

    _write_audit(actor=current_user["username"], action="Role changed",
                 detail=f"{user['username']} \u2192 {new_role.upper()}",
                 role=current_user["role"].upper())
    return {"status": "updated", "id": user_id, "role": new_role}


@router.get("/{user_id}/access")
def get_user_access(user_id: int, current_user: dict = Depends(require_permission("users.view"))):
    conn = get_connection()
    try:
        target = _fetch_user(conn, user_id)
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if not _user_manageable_by(current_user, target):
            raise HTTPException(status_code=403, detail="You do not have access to this user's scope")
    finally:
        conn.close()
    return authz.serialize_scope(target)


@router.post("/{user_id}/access")
def add_user_access(user_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("users.update"))):
    scopes = payload.get("scopes") or []
    if not scopes:
        raise HTTPException(status_code=400, detail="scopes required")

    conn = get_connection()
    try:
        target = _fetch_user(conn, user_id)
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if not _user_manageable_by(current_user, target):
            raise HTTPException(status_code=403, detail="You do not have access to manage this user's scope")

        actor_scope = authz.get_effective_scope(current_user)
        inserted_ids = _validate_and_insert_scopes(conn, user_id, scopes, current_user, actor_scope)
    finally:
        conn.close()

    _write_audit(
        actor=current_user["username"], action="Access granted",
        detail=f"{target['username']}: +{len(inserted_ids)} scope grant(s)",
        role=current_user["role"].upper(),
    )
    return {"status": "updated", "user_id": user_id, "scope_ids": inserted_ids}


@router.delete("/access/{scope_id}")
def revoke_access_scope(scope_id: int, current_user: dict = Depends(require_permission("users.update"))):
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT s.id, s.user_id, u.username, u.role FROM access_scopes s "
            "JOIN users u ON u.id = s.user_id WHERE s.id = %s",
            (scope_id,),
        )
        row = cursor.fetchone()
        cursor.close()
        if not row:
            raise HTTPException(status_code=404, detail="Scope grant not found")

        target = {"id": row["user_id"], "username": row["username"], "role": row["role"]}
        if not _user_manageable_by(current_user, target):
            raise HTTPException(status_code=403, detail="You do not have access to manage this user's scope")

        cursor = conn.cursor()
        cursor.execute("DELETE FROM access_scopes WHERE id = %s", (scope_id,))
        conn.commit()
        cursor.close()
    finally:
        conn.close()

    _write_audit(actor=current_user["username"], action="Access revoked",
                 detail=f"{target['username']}: scope #{scope_id} removed",
                 role=current_user["role"].upper())
    return {"status": "revoked", "scope_id": scope_id}


@router.delete("/{user_id}")
def delete_user(user_id: int, current_user: dict = Depends(require_permission("users.delete"))):
    from app.auth import principals as _principals

    if current_user["id"] == user_id:
        raise HTTPException(status_code=403, detail="Cannot delete your own account")

    conn = get_connection()
    try:
        target = _fetch_user(conn, user_id)
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if not _user_manageable_by(current_user, target):
            raise HTTPException(status_code=403, detail="You do not have access to delete this user")

        # SECURITY: last-admin protection -- see update_role for why.
        if target["role"] == "admin" and _principals.active_admin_count(conn, exclude_user_id=user_id) < 1:
            raise HTTPException(status_code=409, detail="Cannot delete the last remaining admin")

        # Atomic: (1) hand everything this user authored to the acting
        # administrator so the eight ON DELETE RESTRICT foreign keys can't
        # block the delete (this used to 500 for any admin who had ever
        # created a group or granted access), (2) drop bindings /
        # overrides / reviews that target this user (polymorphic principal,
        # no FK to cascade), (3) delete the user.
        try:
            _principals.reassign_authorship(conn, user_id, current_user["id"])
            purged = _principals.purge_principal_grants(conn, "user", user_id)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))  # access_scopes, memberships cascade via FK
            conn.commit()
            cursor.close()
        except HTTPException:
            raise
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=409, detail=f"User could not be deleted: {e}")
    finally:
        conn.close()

    try:
        from app.auth import rbac as _rbac
        _rbac.invalidate_principal(None)
    except Exception:
        pass
    _write_audit(actor=current_user["username"], action="User deleted",
                 detail=f"{target['username']} removed"
                        + (f" ({purged['bindings']} binding(s), {purged['overrides']} override(s) removed)"
                           if purged and (purged['bindings'] or purged['overrides']) else ""),
                 role=current_user["role"].upper())
    return {"status": "deleted", "id": user_id, "username": target["username"]}


# ─────────────────────────────────────────────────────────────────────
# Lifecycle: profile edit, deactivate / activate, admin password reset,
# and a single-call detail view for the user drawer.
# ─────────────────────────────────────────────────────────────────────

def _issue_reset_link(user_id: int, hours: int = 24) -> str:
    """Create a hashed one-time password-set token (same table and format
    as /forgot-password and the welcome email) and return the full link."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(hours=hours)
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (%s, %s, %s)",
            (user_id, _token_hash(token), expires_at),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    return f"{mailer.get_public_app_url()}/reset-password?token={token}"


def _load_manageable_target(conn, user_id: int, current_user: dict) -> dict:
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, username, role, email, active FROM users WHERE id = %s", (user_id,)
    )
    target = cursor.fetchone()
    cursor.close()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if not _user_manageable_by(current_user, target):
        raise HTTPException(status_code=403, detail="You do not have access to manage this user")
    return target


@router.patch("/{user_id}")
def update_user(user_id: int, payload: dict = Body(...),
                current_user: dict = Depends(require_permission("users.update"))):
    """Edit profile fields. Only `email` today; role has its own endpoint
    because it is a privilege change with its own permission and guards."""
    if "email" not in payload:
        raise HTTPException(status_code=400, detail="nothing to update")
    email = (payload.get("email") or "").strip() or None
    if email and not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        raise HTTPException(status_code=400, detail="email is not a valid address")

    conn = get_connection()
    try:
        target = _load_manageable_target(conn, user_id, current_user)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET email = %s WHERE id = %s", (email, user_id))
        conn.commit()
        cursor.close()
    finally:
        conn.close()

    _write_audit(actor=current_user["username"], action="User updated",
                 detail=f"{target['username']}: email {'set' if email else 'cleared'}",
                 role=current_user["role"].upper())
    return {"status": "updated", "id": user_id, "email": email}


@router.post("/{user_id}/deactivate")
def deactivate_user(user_id: int, current_user: dict = Depends(require_permission("users.update"))):
    """
    Reversible lockout. Until now the only way to cut an account off was
    DELETE (irreversible, loses history). deps.get_current_user() and the
    login query already honour users.active, so this takes effect on every
    worker within the session-state cache window; token_version is bumped
    and this worker's cache dropped so it is immediate here.
    """
    from app.auth import principals as _principals

    if current_user["id"] == user_id:
        raise HTTPException(status_code=403, detail="Cannot deactivate your own account")

    conn = get_connection()
    try:
        target = _load_manageable_target(conn, user_id, current_user)
        if target["active"] is not None and not target["active"]:
            raise HTTPException(status_code=409, detail="User is already deactivated")
        if target["role"] == "admin" and _principals.active_admin_count(conn, exclude_user_id=user_id) < 1:
            raise HTTPException(status_code=409, detail="Cannot deactivate the last active admin")
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET active = 0, deactivated_at = UTC_TIMESTAMP(), deactivated_by = %s, "
            "token_version = token_version + 1 WHERE id = %s",
            (current_user["id"], user_id),
        )
        conn.commit()
        cursor.close()
    finally:
        conn.close()

    try:
        from app.auth.deps import forget_user_sessions
        forget_user_sessions(user_id)
    except Exception:
        pass
    _write_audit(actor=current_user["username"], action="User deactivated",
                 detail=f"{target['username']} can no longer sign in", role=current_user["role"].upper())
    return {"status": "deactivated", "id": user_id}


@router.post("/{user_id}/activate")
def activate_user(user_id: int, current_user: dict = Depends(require_permission("users.update"))):
    conn = get_connection()
    try:
        target = _load_manageable_target(conn, user_id, current_user)
        if target["active"] is None or target["active"]:
            raise HTTPException(status_code=409, detail="User is already active")
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET active = 1, deactivated_at = NULL, deactivated_by = NULL WHERE id = %s",
            (user_id,),
        )
        conn.commit()
        cursor.close()
    finally:
        conn.close()

    try:
        from app.auth.deps import forget_user_sessions
        forget_user_sessions(user_id)
    except Exception:
        pass
    _write_audit(actor=current_user["username"], action="User activated",
                 detail=f"{target['username']} can sign in again", role=current_user["role"].upper())
    return {"status": "activated", "id": user_id}


@router.post("/{user_id}/reset-password")
def admin_reset_password(user_id: int, current_user: dict = Depends(require_permission("users.password.reset"))):
    """
    Admin-initiated password reset. Never sets or reveals a password: it
    issues the same hashed, single-use, 24 h set-your-password link the
    welcome email uses. If the user has an email and SMTP is configured
    the link is emailed; otherwise it is returned ONCE in this response so
    the administrator can hand it over out-of-band (and the audit trail
    records that a link was issued either way).
    """
    if current_user["id"] == user_id:
        raise HTTPException(status_code=403, detail="Use Change Password for your own account")

    conn = get_connection()
    try:
        target = _load_manageable_target(conn, user_id, current_user)
    finally:
        conn.close()

    link = _issue_reset_link(user_id)
    emailed = False
    if target.get("email") and mailer.is_configured():
        emailed = bool(mailer.send_email(
            to_addr=target["email"],
            subject="Reset your CloudOps password",
            body_text=(
                f"Hi {target['username']},\n\n"
                f"An administrator has requested a password reset for your CloudOps account.\n\n"
                f"Set a new password (link valid 24 hours, single use):\n{link}\n\n"
                f"If you weren't expecting this, contact your CloudOps administrator.\n"
            ),
        ))

    _write_audit(actor=current_user["username"], action="Password reset issued",
                 detail=f"{target['username']}: link {'emailed' if emailed else 'issued to administrator'}",
                 role=current_user["role"].upper())
    out = {"status": "issued", "id": user_id, "email_sent": emailed, "expires_in_hours": 24}
    if not emailed:
        out["reset_link"] = link
    return out


@router.get("/{user_id}/detail")
def get_user_detail(user_id: int, current_user: dict = Depends(require_permission("users.view"))):
    """Everything the user drawer shows, in one call: profile, groups,
    direct scope grants, v2 role bindings, deny overrides, last review."""
    conn = get_connection()
    try:
        target = _load_manageable_target(conn, user_id, current_user)
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, username, role, email, active, created_at, last_login_at, deactivated_at "
            "FROM users WHERE id = %s", (user_id,))
        profile = cursor.fetchone()

        cursor.execute(
            "SELECT g.id, g.name, g.level, ugm.assigned_at FROM user_group_memberships ugm "
            "JOIN org_groups g ON g.id = ugm.group_id WHERE ugm.user_id = %s ORDER BY g.level, g.name",
            (user_id,))
        groups = cursor.fetchall()

        cursor.execute(
            "SELECT s.id, s.cloud, s.account_ref_id, a.account_name, s.regions, s.resource_types, s.created_at "
            "FROM access_scopes s LEFT JOIN aws_accounts a ON a.id = s.account_ref_id "
            "WHERE s.user_id = %s ORDER BY s.created_at", (user_id,))
        scopes = cursor.fetchall()
        for sc in scopes:
            for f in ("regions", "resource_types"):
                sc[f] = authz._parse_json_list(sc[f])

        cursor.execute(
            "SELECT b.id, r.role_key, r.name AS role_name, sc.label AS scope_label, b.reason, b.expires_at, "
            "b.created_at, gu.username AS granted_by_username "
            "FROM role_bindings b JOIN roles r ON r.id = b.role_id "
            "JOIN rbac_scopes sc ON sc.id = b.scope_id LEFT JOIN users gu ON gu.id = b.granted_by "
            "WHERE b.principal_type = 'user' AND b.principal_id = %s ORDER BY b.created_at DESC", (user_id,))
        bindings = cursor.fetchall()

        cursor.execute(
            "SELECT o.id, p.code AS permission_code, o.effect, sc.label AS scope_label, o.reason, o.expires_at "
            "FROM permission_overrides o JOIN permissions p ON p.id = o.permission_id "
            "LEFT JOIN rbac_scopes sc ON sc.id = o.scope_id "
            "WHERE o.principal_type = 'user' AND o.principal_id = %s ORDER BY o.created_at DESC", (user_id,))
        overrides = cursor.fetchall()

        cursor.execute(
            "SELECT ar.decision, ar.notes, ar.reviewed_at, ru.username AS reviewed_by_username "
            "FROM access_reviews ar LEFT JOIN users ru ON ru.id = ar.reviewed_by "
            "WHERE ar.principal_type = 'user' AND ar.principal_id = %s ORDER BY ar.reviewed_at DESC LIMIT 1",
            (user_id,))
        last_review = cursor.fetchone()
        cursor.close()
    finally:
        conn.close()

    profile["active"] = bool(profile["active"]) if profile["active"] is not None else True
    return _serialize({
        "profile": profile,
        "groups": groups,
        "scope_grants": scopes,
        "role_bindings": bindings,
        "overrides": overrides,
        "last_review": last_review,
        "effective_scope": authz.serialize_scope(target),
    })
