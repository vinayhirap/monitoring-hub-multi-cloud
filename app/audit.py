# app/audit.py
"""
Single source of truth for writing to the compliance audit_logs table.

Until now this exact INSERT was copy-pasted independently into SIX
different modules (admin/accounts.py, admin/users.py, admin/groups.py,
auth.py, settings.py, metric_catalog.py), each with a slightly different
signature, a different default, and a different error-handling style
(some used logger.warning, two used a bare print() that never reaches
journalctl/log aggregation the same way). That drift produced real
compliance bugs, all fixed alongside this consolidation:

  - admin/groups.py's copy took no `role` parameter at all and hardcoded
    every entry's role to "ADMIN" in the payload, regardless of who
    actually called it. Every group endpoint is gated by a granular
    permission (groups.create/update/delete), not require_role("admin"),
    so an editor with that permission granted had their group-management
    actions permanently misattributed as admin actions.
  - admin/users.py had three call sites ("Role changed", "Access
    revoked", "User deleted") that never passed `role` either, silently
    falling back to the same hardcoded "ADMIN" default -- despite all
    three endpoints being reachable by require_role("admin", "editor").
  - metric_catalog.py's "Applied default metric template" call site
    hardcoded the actor as the literal string "admin" instead of using
    the real current_user["username"] that was already in scope.
  - auth.py's copy took a raw `payload` dict with no {"role": ...} key
    at all for "Password changed"/"Password reset requested"/"Password
    reset completed" -- so those entries always rendered with whatever
    fallback the frontend badge used, independent of the actual actor.

Every caller now imports write_audit() from here instead of keeping its
own copy, so this class of bug can't reappear by one module drifting
from the others.
"""
import json
import logging

from app.db import get_connection

logger = logging.getLogger(__name__)


def _client_ip(request) -> str | None:
    """Best-effort caller IP from a FastAPI Request. Never raises."""
    if request is None:
        return None
    try:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else None
    except Exception:
        return None


def write_audit(actor: str, action: str, detail: str = None, *,
                 role: str = None, payload: dict = None, request=None) -> None:
    """
    Write one row to audit_logs. Never raises -- a failed audit write
    logs a warning and returns, rather than failing the request whose
    side effect it's recording (matches every prior implementation's
    behavior).

    actor
        The real identity that performed the action -- current_user
        ["username"], or "system" ONLY for genuinely unattended
        background actions with no HTTP caller at all (e.g. the
        collector auto-enabling newly-discovered services). Never a
        hardcoded literal for an action a human triggered.

    action
        Short human-readable action name, e.g. "User deleted".

    detail
        Human-readable description, for callers using the common
        {"detail": ..., "role": ...} payload shape the Compliance UI
        already expects. Mutually exclusive with `payload`.

    role
        The actor's ACTUAL role at the time of the action (e.g.
        current_user["role"].upper()). Pass this explicitly whenever a
        current_user is available -- RBAC permissions are granted
        per-action, not 1:1 with the "admin" role, so editors/viewers
        with a specific permission can reach admin-adjacent endpoints
        too. Leave unset (None) only when the role genuinely isn't
        known (e.g. a failed login for a username that may not exist)
        -- the Compliance UI shows no role badge rather than guessing
        one, which is safer than fabricating "ADMIN" as a default ever
        was.

    payload
        Pass a raw dict instead of `detail` for callers that want full
        control over the JSON shape (e.g. auth.py's login/password
        events, which don't use the detail/role wrapper). If `role` is
        also given and not already a key in `payload`, it's merged in
        so every audit row consistently carries a role field one way
        or another.

    request
        Optional FastAPI Request. When given, the caller's source IP
        is recorded in the ip_address column -- best-effort; a missing
        or unparseable client address never blocks the write.
    """
    try:
        if payload is None:
            payload = {"detail": detail, "role": role}
        elif role is not None and "role" not in payload:
            payload = {**payload, "role": role}

        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO audit_logs (actor, action, payload, ip_address) VALUES (%s,%s,%s,%s)",
            (actor or "unknown", action, json.dumps(payload), _client_ip(request)),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning("Audit write failed (actor=%s action=%s): %s", actor, action, e)
