# app/api/escalation.py
"""
CRUD for escalation_policies (db/migrations/023_escalation_policies.sql,
roadmap phase 9). Gated on alerts.configure — same permission that
already governs threshold configuration (app/api/settings.py) — since
an escalation policy is, functionally, another kind of alert-behavior
configuration, not a separate privileged surface.

SECURITY: every endpoint below additionally enforces account scope via
get_accessible_account_ids() (app/auth/authorization.py) -- the same
deny-by-default access_scopes system already used by
app/api/admin/accounts.py and 14 other endpoint files. Before this fix,
none of the four endpoints here checked it at all: list_policies
returned every account's policies to any editor regardless of their
own access_scopes grants, create_policy accepted any aws_account_id
with no ownership check, and update_policy/delete_policy took a bare
policy_id with NO scope check whatsoever -- a pure IDOR, since neither
looked up which account the policy even belonged to before mutating
it. An editor scoped to exactly one client account could view, create,
modify, or delete another client's escalation policy. Found while
scoping Phase 3 of the RBAC audit plan (this is a live gap in the
CURRENT, already-enforced v1 access_scopes system, not something that
needs the v2 cutover to fix -- same pattern already proven in
app/api/admin/accounts.py).

A NULL aws_account_id policy is the org-wide fallback that applies to
every account not covered by a specific policy, so it affects
everyone -- creating, editing, or deleting one is treated as an
admin-rank action regardless of the caller's per-account scope grants.
"""
import logging
from fastapi import APIRouter, Body, HTTPException, Depends
from app.db import get_connection
from app.auth.permissions import require_permission
from app.auth.authorization import get_accessible_account_ids

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/escalation-policies", tags=["Escalation Policies"])


def _check_account_access(current_user: dict, account_id):
    """
    None (account_id) => the org-wide fallback policy -- admin only.
    Otherwise the caller must be admin or have this specific account
    in their get_accessible_account_ids() set. Raises 403 rather than
    404 for an inaccessible-but-real account: existence of another
    client's account row is not itself sensitive (accounts.py's own
    list already reveals it exists to anyone with accounts.view via
    other means), only its escalation configuration is being gated
    here.
    """
    if current_user["role"] == "admin":
        return
    if account_id is None:
        raise HTTPException(status_code=403, detail="Only an admin may manage the org-wide fallback escalation policy")
    accessible = get_accessible_account_ids(current_user)
    if accessible is not None and account_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")


@router.get("/groups")
def list_org_groups(current_user: dict = Depends(require_permission("escalation.view"))):
    """
    Lean read for the policy-creation dropdown — id/name/level only, no
    membership or policy detail. There is no existing org_groups list
    endpoint anywhere in the app today; this is intentionally the
    smallest possible addition rather than a general-purpose org_groups
    API, which is a separate piece of work this phase doesn't need.
    """
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT id, name, level, parent_group_id FROM org_groups ORDER BY level, name")
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


@router.get("")
def list_policies(current_user: dict = Depends(require_permission("escalation.view"))):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT ep.*, g.name AS group_name, acc.account_name
            FROM escalation_policies ep
            JOIN org_groups g ON g.id = ep.escalate_to_group_id
            LEFT JOIN aws_accounts acc ON acc.id = ep.aws_account_id
            ORDER BY ep.aws_account_id IS NULL, ep.severity
        """)
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()

    if current_user["role"] == "admin":
        return rows
    accessible = get_accessible_account_ids(current_user)
    if accessible is None:
        return rows
    # The org-wide fallback (aws_account_id IS NULL) is visible to
    # everyone with escalation.view -- it's read-only exposure of a
    # policy that already applies to every account, not a leak of any
    # one client's configuration.
    return [r for r in rows if r["aws_account_id"] is None or r["aws_account_id"] in accessible]


@router.post("")
def create_policy(payload: dict = Body(...), current_user: dict = Depends(require_permission("escalation.manage"))):
    severity = payload.get("severity")
    if severity not in ("WARNING", "CRITICAL"):
        raise HTTPException(status_code=400, detail="severity must be WARNING or CRITICAL")
    ack_sla_minutes = int(payload.get("ack_sla_minutes", 0))
    if ack_sla_minutes <= 0:
        raise HTTPException(status_code=400, detail="ack_sla_minutes must be positive")
    escalate_to_group_id = payload.get("escalate_to_group_id")
    if not escalate_to_group_id:
        raise HTTPException(status_code=400, detail="escalate_to_group_id is required")
    account_id = payload.get("aws_account_id")  # None = global fallback policy

    _check_account_access(current_user, account_id)

    conn = get_connection(); cur = conn.cursor()
    try:
        # current_user["id"], not "sub" -- the JWT claim itself is named
        # "sub", but app/auth/security.py's decode_token() already
        # unpacks it into current_user["id"] before this function ever
        # sees it (same as every other endpoint in this codebase reads
        # the actor's id). Reading "sub" here KeyErrors on every single
        # policy creation attempt -- this endpoint has never worked.
        cur.execute("""
            INSERT INTO escalation_policies
                (aws_account_id, severity, ack_sla_minutes, escalate_to_group_id, created_by)
            VALUES (%s, %s, %s, %s, %s)
        """, (account_id, severity, ack_sla_minutes, escalate_to_group_id, int(current_user["id"])))
        conn.commit()
        new_id = cur.lastrowid
    except Exception as e:
        conn.rollback()
        if "uniq_policy_scope" in str(e):
            raise HTTPException(status_code=409, detail="A policy for this account+severity already exists — edit or delete it instead")
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close(); conn.close()
    return {"status": "created", "id": new_id}


@router.patch("/{policy_id}")
def update_policy(policy_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("escalation.manage"))):
    fields, params = [], []
    if "ack_sla_minutes" in payload:
        fields.append("ack_sla_minutes = %s"); params.append(int(payload["ack_sla_minutes"]))
    if "escalate_to_group_id" in payload:
        fields.append("escalate_to_group_id = %s"); params.append(payload["escalate_to_group_id"])
    if "enabled" in payload:
        fields.append("enabled = %s"); params.append(int(payload["enabled"]))
    if not fields:
        raise HTTPException(status_code=400, detail="No updatable fields provided")
    params.append(policy_id)

    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT aws_account_id FROM escalation_policies WHERE id = %s", (policy_id,))
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Policy not found")
        _check_account_access(current_user, existing["aws_account_id"])

        cur2 = conn.cursor()
        cur2.execute(f"UPDATE escalation_policies SET {', '.join(fields)} WHERE id = %s", params)
        conn.commit()
        updated = cur2.rowcount
        cur2.close()
    finally:
        cur.close(); conn.close()
    if not updated:
        raise HTTPException(status_code=404, detail="Policy not found")
    return {"status": "updated"}


@router.delete("/{policy_id}")
def delete_policy(policy_id: int, current_user: dict = Depends(require_permission("escalation.manage"))):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT aws_account_id FROM escalation_policies WHERE id = %s", (policy_id,))
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Policy not found")
        _check_account_access(current_user, existing["aws_account_id"])

        cur2 = conn.cursor()
        cur2.execute("DELETE FROM escalation_policies WHERE id = %s", (policy_id,))
        conn.commit()
        deleted = cur2.rowcount
        cur2.close()
    finally:
        cur.close(); conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="Policy not found")
    return {"status": "deleted"}

