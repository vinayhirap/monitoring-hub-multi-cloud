# app/api/topology.py
"""
Topology/dependency graph — roadmap phase 4/7 (2026-09-13).

Two edge sources, both read from resource_relationships
(db/migrations/021_resource_relationships.sql):
  - 'auto'   -- written by app/aws/describe_polling.py's
               poll_alb_target_health() from ALB target-health data it
               already fetches every cycle. Never written here.
  - 'manual' -- added/removed only through this file's POST/DELETE
               endpoints, for relationships no Describe API can tell us
               (an app-level dependency, a cross-account call, etc).

This ties directly to the 2026-08-26 Mumbai RCA: nodes returned here are
exactly the account's `resources` rows, so a resource AWS reports (via
the target-health/discovery calls elsewhere in this app) but that never
made it into `resources` shows up as a dangling edge endpoint with no
matching node -- the same class of silent gap that RCA had to be found
by hand. The frontend should render an edge with no matching node
distinctly (e.g. a "missing resource" badge) rather than silently
dropping it.
"""
import logging
from fastapi import APIRouter, HTTPException, Body, Depends
from app.db import get_connection
from app.auth.permissions import require_permission
from app.auth.authorization import get_accessible_account_ids

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/topology", tags=["Topology"])


def _require_account_access(account_id: int, current_user: dict) -> None:
    """Same account_id-not-in-accessible pattern used throughout
    app/api/settings.py, app/api/live_data.py, app/api/alerts.py."""
    accessible = get_accessible_account_ids(current_user)
    if accessible is not None and account_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")


@router.get("/{account_id}")
def get_topology(account_id: int, current_user: dict = Depends(require_permission("resources.view"))):
    """
    Returns { nodes: [...], edges: [...] } for one account.

    nodes come straight from `resources` for this account -- id,
    resource_type, name, region, instance_state where applicable.

    edges are every resource_relationships row for this account, PLUS a
    `node_gap` flag per edge marking whether source/target actually has
    a matching row in `resources` (see module docstring -- this is the
    RCA-tie-in: a 'routes_to' edge pointing at a resource_id this app
    never discovered is surfaced, not silently dropped).
    """
    _require_account_access(account_id, current_user)
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT resource_id, resource_type, name, region, instance_state
            FROM resources
            WHERE aws_account_id = %s
        """, (account_id,))
        nodes = cur.fetchall()
        known_ids = {n["resource_id"] for n in nodes}

        cur.execute("""
            SELECT id, source_resource_id, target_resource_id,
                   relationship_type, source, created_at
            FROM resource_relationships
            WHERE aws_account_id = %s
        """, (account_id,))
        edges = cur.fetchall()
        for e in edges:
            e["source_missing"] = e["source_resource_id"] not in known_ids
            e["target_missing"] = e["target_resource_id"] not in known_ids
            if e.get("created_at"):
                e["created_at"] = e["created_at"].strftime("%Y-%m-%dT%H:%M:%SZ")

        return {"nodes": nodes, "edges": edges}
    finally:
        cur.close(); conn.close()


@router.post("/{account_id}/manual-edge")
def add_manual_edge(
    account_id: int,
    payload: dict = Body(...),
    current_user: dict = Depends(require_permission("topology.manage")),
):
    """
    Adds one operator-declared edge -- e.g. "this Lambda calls that RDS
    instance" -- which no Describe API can tell us.

    Gated on topology.manage, NOT resources.view (see
    db/migrations/024_topology_manage_permission.sql) -- resources.view
    is granted to the viewer role for the read-only GET below, and
    declaring/removing a dependency is a write action that shouldn't
    ride along with a read permission the way it originally did.

    Body: { "source_resource_id": "...", "target_resource_id": "...",
            "relationship_type": "depends_on" (optional, default) }
    """
    _require_account_access(account_id, current_user)
    source_id = payload.get("source_resource_id")
    target_id = payload.get("target_resource_id")
    if not source_id or not target_id:
        raise HTTPException(status_code=400, detail="source_resource_id and target_resource_id are required")
    if source_id == target_id:
        raise HTTPException(status_code=400, detail="A resource cannot depend on itself")
    relationship_type = payload.get("relationship_type", "depends_on")

    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO resource_relationships
                (aws_account_id, source_resource_id, target_resource_id,
                 relationship_type, source)
            VALUES (%s, %s, %s, %s, 'manual')
            ON DUPLICATE KEY UPDATE source = 'manual'
        """, (account_id, source_id, target_id, relationship_type))
        conn.commit()
        new_id = cur.lastrowid
    finally:
        cur.close(); conn.close()
    return {"status": "created", "id": new_id}


@router.delete("/{account_id}/manual-edge/{edge_id}")
def delete_manual_edge(
    account_id: int,
    edge_id: int,
    current_user: dict = Depends(require_permission("topology.manage")),
):
    """Only deletes edges with source='manual' -- an 'auto' edge is
    re-derived every describe-poll cycle, so deleting it here would just
    have it reappear on the next cycle; the only real way to remove an
    auto edge is for the underlying AWS target registration to change."""
    _require_account_access(account_id, current_user)
    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            DELETE FROM resource_relationships
            WHERE id = %s AND aws_account_id = %s AND source = 'manual'
        """, (edge_id, account_id))
        conn.commit()
        deleted = cur.rowcount
    finally:
        cur.close(); conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="Manual edge not found (or it is an auto-derived edge, which cannot be deleted directly)")
    return {"status": "deleted"}
