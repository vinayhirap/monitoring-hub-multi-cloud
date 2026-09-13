# app/api/escalation.py
"""
CRUD for escalation_policies (db/migrations/023_escalation_policies.sql,
roadmap phase 9). Gated on alerts.configure — same permission that
already governs threshold configuration (app/api/settings.py) — since
an escalation policy is, functionally, another kind of alert-behavior
configuration, not a separate privileged surface.
"""
import logging
from fastapi import APIRouter, Body, HTTPException, Depends
from app.db import get_connection
from app.auth.permissions import require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/escalation-policies", tags=["Escalation Policies"])


@router.get("/groups")
def list_org_groups(current_user: dict = Depends(require_permission("alerts.configure"))):
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
def list_policies(current_user: dict = Depends(require_permission("alerts.configure"))):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT ep.*, g.name AS group_name, acc.account_name
            FROM escalation_policies ep
            JOIN org_groups g ON g.id = ep.escalate_to_group_id
            LEFT JOIN aws_accounts acc ON acc.id = ep.aws_account_id
            ORDER BY ep.aws_account_id IS NULL, ep.severity
        """)
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


@router.post("")
def create_policy(payload: dict = Body(...), current_user: dict = Depends(require_permission("alerts.configure"))):
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
def update_policy(policy_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("alerts.configure"))):
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

    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute(f"UPDATE escalation_policies SET {', '.join(fields)} WHERE id = %s", params)
        conn.commit()
        updated = cur.rowcount
    finally:
        cur.close(); conn.close()
    if not updated:
        raise HTTPException(status_code=404, detail="Policy not found")
    return {"status": "updated"}


@router.delete("/{policy_id}")
def delete_policy(policy_id: int, current_user: dict = Depends(require_permission("alerts.configure"))):
    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute("DELETE FROM escalation_policies WHERE id = %s", (policy_id,))
        conn.commit()
        deleted = cur.rowcount
    finally:
        cur.close(); conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="Policy not found")
    return {"status": "deleted"}
