# app/api/setup_status.py
"""
First-run setup status (audit B9). Several features are fully built but ship empty - status page, SLOs, synthetic
checks, escalation policies, notification channels - and nothing told an administrator they existed or what to do
first, so `/status` just said "No services configured".

GET /api/setup/status  ->  {"steps": [{key, done, hint}], "done": n, "total": n}

Booleans and counts only, for tables that hold no customer data. Whether a given user is *shown* the checklist, and
which steps they get links for, is decided in the UI from their permissions; the page each step links to enforces its
own permission, so this endpoint grants nothing.
"""
from fastapi import APIRouter, Depends

from app.auth.deps import get_current_user
from app.db import get_connection

router = APIRouter(prefix="/api/setup", tags=["Setup"])

# key -> (table, extra WHERE). Constants only (never user input), so interpolation is safe.
_STEPS = (
    ("accounts",      "aws_accounts",           "status = 'active'"),
    ("notifications", "notification_channels",  "enabled = 1"),
    ("synthetic",     "synthetic_checks",       "1 = 1"),
    ("slo",           "slo_definitions",        "1 = 1"),
    ("status_page",   "status_page_components", "1 = 1"),
    ("escalation",    "escalation_policies",    "enabled = 1"),
)

HINTS = {
    "accounts":      "Connect a cloud account so CloudOps has something to watch.",
    "notifications": "Add a Slack, Teams, webhook or email channel so alerts reach people outside the browser.",
    "synthetic":     "Add an uptime check (for example an HTTP check on your load balancer address).",
    "slo":           "Define a service level objective to track an error budget.",
    "status_page":   "Add a component so the public status page has something to show.",
    "escalation":    "Set an escalation policy so unacknowledged alerts are handed to a group.",
}


def build_steps(counts: dict) -> dict:
    steps = [{"key": key, "done": counts.get(key, 0) > 0, "hint": HINTS[key]} for key, _t, _w in _STEPS]
    return {"steps": steps, "done": sum(1 for s in steps if s["done"]), "total": len(steps)}


def _count(cursor, table: str, where: str) -> int:
    try:
        cursor.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}")
        return int((cursor.fetchone() or {}).get("n") or 0)
    except Exception:
        return 0       # a table that does not exist yet (older schema) counts as "not set up", never a 500


@router.get("/status")
def setup_status(current_user: dict = Depends(get_current_user)):
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        counts = {key: _count(cursor, table, where) for key, table, where in _STEPS}
    finally:
        conn.close()
    return build_steps(counts)
