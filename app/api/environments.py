# app/api/environments.py
"""
Environment classification API (phase 4a).

  GET  /api/environments/summary                 counts per env / source, per account
  GET  /api/environments/resources               the Needs-classification queue, the Tagging view, or every resource
  GET  /api/environments/tag-plan                an AWS CLI script that writes the Environment tag for CloudOps decisions
                                                 (environments.manage; AWS accounts only; optional env / region / type filters)
  POST /api/environments/classify                mark one or many resources prod|nonprod|clear, or a whole
                                                 group ({"group": {"account_id", "resource_type"}}) of the resources
                                                 still waiting in the Needs-classification queue
  PUT  /api/environments/accounts/{id}/default   set (prod | nonprod) / clear an account's default environment
  GET  /api/environments/accounts/{id}/default/preview   what that change would do (read-only, nothing is written)
  GET  /api/environments/resources/{id}/history  who changed one resource's environment, and from what
  GET  /api/environments/compliance              tag state and decision source per account and resource type
  GET  /api/environments/compliance/trend        the daily snapshots behind the trend chart
  GET  /api/environments/compliance.csv          the compliance report as CSV
  GET  /api/environments/tagging-gaps.csv        resources missing standard tags, as CSV
  GET  /api/environments/digest/preview          what the weekly digest of new unknown resources would do (manage)

Permissions: environments.view (read), environments.manage (write), migration
083. Every row is additionally checked against the caller's account scope
(get_accessible_account_ids): a write that names even ONE resource outside that
scope is rejected as a whole, and reads only ever return in-scope accounts.
Changing an account default affects every unclassified resource in it, so it
needs the same scope check as a single resource.

Every change is written to audit_logs (app/audit.py): who, what, how many, which
accounts, and the before/after environment counts.

There are two environments, prod and nonprod. A resource nothing can place resolves to prod (the fail-safe) with
source "unknown" and waits in the Needs-classification queue until a person confirms it ("Mark PROD") or moves it
("Mark NON-PROD"). Nothing in this module can silence a resource.
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response

from app import environment_classifier as ec
from app.audit import write_audit
from app.auth.authorization import get_accessible_account_ids
from app.auth.permissions import require_permission
from app.collector.env_sync import (NON_ALERTABLE_TYPES, STALE_RESOURCE_HOURS,
                                    sync_resource_environments)
from app.collector.tag_plan import DEFAULT_SOURCES, build_tag_plan
from app.db import get_connection
from app import env_governance_db as gov
from app import environment_governance as eg

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/environments", tags=["Environments"])

MAX_BULK = 500
MAX_GROUP = 2000          # one account + one resource type, only what is still in the Needs-classification queue
MAX_QUEUE_GROUPS = 40
AUDIT_ID_SAMPLE = 100
_CHOICES = ec.MANUAL_CHOICES + ("clear",)
_VIA = ("suggestion",)             # how a bulk classify was reached, recorded in the audit entry
_QUEUE_WHERE = "(r.env_source = 'unknown' OR r.env_source IS NULL)"
_LIVE_WHERE = (
    "(r.instance_state IS NULL OR r.instance_state != 'terminated') "
    "AND (r.last_seen_at IS NULL OR r.last_seen_at >= DATE_SUB(NOW(), INTERVAL %s HOUR)) "
    "AND r.resource_type NOT IN ({types})"
).format(types=", ".join(["%s"] * len(NON_ALERTABLE_TYPES)))


def _live_params():
    return [STALE_RESOURCE_HOURS, *NON_ALERTABLE_TYPES]


def _scope(current_user):
    """-> (sql_fragment, params). Empty set of accounts matches nothing."""
    accessible = get_accessible_account_ids(current_user)
    if accessible is None:
        return "", []
    if not accessible:
        return " AND 1 = 0", []
    ids = sorted(accessible)
    return " AND r.aws_account_id IN (" + ", ".join(["%s"] * len(ids)) + ")", ids


def _require_account_access(current_user, account_ids):
    accessible = get_accessible_account_ids(current_user)
    if accessible is None:
        return
    denied = sorted(set(account_ids) - set(accessible))
    if denied:
        raise HTTPException(status_code=403, detail="You do not have access to one or more of these accounts")


def _conflict(override, tags):
    """A CloudOps manual choice that contradicts what the resource's own AWS tag says."""
    tag_env = ec.tag_environment(tags)
    return bool(override in ec.TIERS and tag_env and tag_env != override)


def _iso(v):
    return v.isoformat() + "Z" if isinstance(v, datetime) else v


@router.get("/summary")
def summary(current_user: dict = Depends(require_permission("environments.view"))):
    scope_sql, scope_params = _scope(current_user)
    warn_col = gov.has_warning_column()
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(f"""
                SELECT aa.id AS account_id, aa.account_name, aa.provider, aa.default_env,
                       (aa.created_at >= DATE_SUB(NOW(), INTERVAL {gov.RECENT_ONBOARD_DAYS} DAY)) AS recently_onboarded,
                       COUNT(r.id) AS total,
                       SUM(CASE WHEN {_QUEUE_WHERE} THEN 1 ELSE 0 END) AS needs_classification
                FROM aws_accounts aa
                LEFT JOIN resources r ON r.aws_account_id = aa.id AND {_LIVE_WHERE}{scope_sql}
                WHERE aa.status = 'active'
                GROUP BY aa.id, aa.account_name, aa.provider, aa.default_env, aa.created_at
                ORDER BY aa.account_name
            """, _live_params() + scope_params)
            accounts = cur.fetchall()
            cur.execute(f"""
                SELECT COALESCE(r.env_resolved, 'unclassified') AS env,
                       COALESCE(r.env_source, 'none')           AS source,
                       COUNT(*) AS n
                FROM resources r
                JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
                WHERE {_LIVE_WHERE}{scope_sql}
                GROUP BY env, source
            """, _live_params() + scope_params)
            breakdown = cur.fetchall()
            cur.execute(f"""
                SELECT COALESCE(r.tag_state, 'unclassified') AS state, COUNT(*) AS n
                FROM resources r
                JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
                WHERE {_LIVE_WHERE}{scope_sql}
                GROUP BY state
            """, _live_params() + scope_params)
            tag_states = {t["state"]: int(t["n"]) for t in cur.fetchall()}
            cur.execute(f"""
                SELECT r.tags, r.env_override FROM resources r
                JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
                WHERE {_LIVE_WHERE}{scope_sql} AND r.env_override IN ('prod', 'nonprod')
            """, _live_params() + scope_params)
            conflicts = sum(1 for r in cur.fetchall() if _conflict(r["env_override"], r["tags"]))
            mismatch_kinds = {}
            if warn_col:
                cur.execute(f"""
                    SELECT r.env_warning AS w, COUNT(*) AS n FROM resources r
                    JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
                    WHERE {_LIVE_WHERE}{scope_sql} AND r.env_warning IS NOT NULL
                    GROUP BY r.env_warning
                """, _live_params() + scope_params)
                for g in cur.fetchall():
                    kind = ec.warning_kind(g["w"])
                    if kind:
                        mismatch_kinds[kind] = mismatch_kinds.get(kind, 0) + int(g["n"])
            # what is waiting in the queue, by account and resource type: lets the page offer "place all 80
            # certificates in this account at once" instead of 50 checkboxes at a time
            cur.execute(f"""
                SELECT r.aws_account_id AS account_id, aa.account_name, r.resource_type, COUNT(*) AS n
                FROM resources r
                JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
                WHERE {_LIVE_WHERE}{scope_sql} AND {_QUEUE_WHERE}
                GROUP BY r.aws_account_id, aa.account_name, r.resource_type
                ORDER BY n DESC, aa.account_name, r.resource_type
                LIMIT {MAX_QUEUE_GROUPS}
            """, _live_params() + scope_params)
            queue_groups = [dict(g, n=int(g["n"])) for g in cur.fetchall()]
            # which tier each account mostly is, and what the filters can offer
            cur.execute(f"""
                SELECT r.aws_account_id AS account_id, COALESCE(r.env_resolved, 'unclassified') AS env, COUNT(*) AS n
                FROM resources r
                JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
                WHERE {_LIVE_WHERE}{scope_sql}
                GROUP BY r.aws_account_id, env
            """, _live_params() + scope_params)
            account_envs = {}
            for g in cur.fetchall():
                account_envs.setdefault(g["account_id"], {})[g["env"]] = int(g["n"])
            cur.execute(f"""
                SELECT r.resource_type AS value, COUNT(*) AS n
                FROM resources r
                JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
                WHERE {_LIVE_WHERE}{scope_sql}
                GROUP BY r.resource_type ORDER BY n DESC, r.resource_type LIMIT 80
            """, _live_params() + scope_params)
            types = [{"value": t["value"], "n": int(t["n"])} for t in cur.fetchall() if t["value"]]
            cur.execute(f"""
                SELECT r.region AS value, COUNT(*) AS n
                FROM resources r
                JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
                WHERE {_LIVE_WHERE}{scope_sql} AND r.region IS NOT NULL AND r.region != ''
                GROUP BY r.region ORDER BY n DESC, r.region LIMIT 60
            """, _live_params() + scope_params)
            regions = [{"value": t["value"], "n": int(t["n"])} for t in cur.fetchall()]
        finally:
            cur.close()
    finally:
        conn.close()

    allowed = None
    if scope_sql:
        allowed = set(scope_params)
    accounts = [a for a in accounts if allowed is None or a["account_id"] in allowed]
    by_env, by_source = {}, {}
    for b in breakdown:
        by_env[b["env"]] = by_env.get(b["env"], 0) + int(b["n"])
        by_source[b["source"]] = by_source.get(b["source"], 0) + int(b["n"])
    for a in accounts:
        a["total"] = int(a["total"] or 0)
        a["needs_classification"] = int(a["needs_classification"] or 0)
        a["provider"] = a.get("provider") or "aws"
        a["recently_onboarded"] = bool(a.get("recently_onboarded"))
        a["by_env"] = account_envs.get(a["account_id"], {})
    return {
        "needs_classification": sum(a["needs_classification"] for a in accounts),
        "by_env": by_env,
        "by_source": by_source,
        "by_tag_state": tag_states,
        "conflicts": conflicts,
        "mismatches": sum(mismatch_kinds.values()),
        "mismatch_kinds": mismatch_kinds,
        "queue_groups": queue_groups,
        "facets": {"resource_types": types, "regions": regions},
        "accounts": accounts,
    }


@router.get("/resources")
def list_resources(
    view: str = Query("needs_classification", pattern="^(needs_classification|tagging|mismatches|all)$"),
    env: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    tag_state: Optional[str] = Query(None),
    account_id: Optional[int] = Query(None),
    resource_type: Optional[str] = Query(None, max_length=60),
    region: Optional[str] = Query(None, max_length=40),
    q: Optional[str] = Query(None, max_length=100),
    warning_kind: Optional[str] = Query(None),
    suggest: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(require_permission("environments.view")),
):
    scope_sql, scope_params = _scope(current_user)
    where = _LIVE_WHERE + scope_sql
    params = _live_params() + scope_params
    warn_col = gov.has_warning_column()
    if warning_kind is not None and warning_kind not in ec.WARNING_KINDS:
        raise HTTPException(status_code=400, detail="warning_kind must be one of: " + ", ".join(ec.WARNING_KINDS))
    if view == "needs_classification":
        where += f" AND {_QUEUE_WHERE}"
    elif view == "tagging":
        where += " AND r.tag_state IN ('missing', 'invalid', 'nonstandard')"
    elif view == "mismatches":
        # without migration 098 nothing can be flagged yet: the tab is simply empty
        where += " AND r.env_warning IS NOT NULL" if warn_col else " AND 1 = 0"
    if warning_kind and warn_col:
        where += " AND r.env_warning LIKE %s"; params.append(warning_kind + ":%")
    if tag_state:
        if tag_state not in ec.TAG_STATES:
            raise HTTPException(status_code=400, detail="tag_state must be ok, nonstandard, invalid, missing or not_checked")
        where += " AND r.tag_state = %s"; params.append(tag_state)
    if env:
        if env == "unclassified":
            where += " AND r.env_resolved IS NULL"
        elif env in ec.TIERS:
            where += " AND r.env_resolved = %s"; params.append(env)
        else:
            raise HTTPException(status_code=400, detail="env must be prod, nonprod or unclassified")
    if source:
        if source not in ec.SOURCES:
            raise HTTPException(status_code=400, detail="source must be manual, tag, name, related, account or unknown")
        where += " AND r.env_source = %s"; params.append(source)
    if account_id is not None:
        where += " AND r.aws_account_id = %s"; params.append(account_id)
    if resource_type:
        where += " AND r.resource_type = %s"; params.append(resource_type)
    if region:
        where += " AND r.region = %s"; params.append(region)
    if q:
        like = f"%{q}%"
        where += " AND (r.name LIKE %s OR r.resource_id LIKE %s)"; params += [like, like]

    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(f"""SELECT COUNT(*) AS n FROM resources r
                            JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
                            WHERE {where}""", params)
            total = int(cur.fetchone()["n"])
            warn_select = ", r.env_warning AS warning" if warn_col else ""
            synced_sql = _synced_fragment(cur)      # NULL AS synced_tags unless the tag sync is on (workstream B)
            cur.execute(f"""
                SELECT r.id, r.aws_account_id AS account_id, aa.account_name, aa.provider, r.resource_type,
                       r.resource_id, r.name, r.region,
                       r.env_resolved AS env, r.env_source AS source, r.env_reason AS reason,
                       r.env_override AS override, r.env_override_by AS override_by,
                       r.env_override_at AS override_at, r.tag_state, r.tags, {synced_sql}{warn_select}
                FROM resources r
                JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
                WHERE {where}
                ORDER BY aa.account_name, r.resource_type, COALESCE(r.name, r.resource_id)
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            rows = cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()
    tags_by_pk = {}
    for r in rows:
        r["override_at"] = _iso(r["override_at"])
        tags = r.pop("tags")                 # raw tags are not returned, only what is derived from them
        r["warning"] = r.get("warning")
        r["warning_kind"] = ec.warning_kind(r["warning"])
        tags_by_pk[r["id"]] = tags           # raw stored tags, as before: suggestions are not fed by the tag sync
        synced = ec.parse_synced(r.pop("synced_tags", None))
        tags = ec.effective_tags(r["resource_type"], tags, synced)   # the tag sync's result for the types it covers
        r["missing_tags"] = ec.missing_tags(tags, ec.ESSENTIAL_TAGS) if r["tag_state"] and r["tag_state"] != "not_checked" else []
        r["tag_env"] = ec.tag_environment(tags)
        r["conflict"] = _conflict(r["override"], tags)
    if suggest and view == "needs_classification" and rows:
        try:
            gov.attach_suggestions(rows, tags_by_pk)         # proposals only; nothing is applied
        except Exception:
            logger.exception("environment suggestions failed")     # the queue still works without them
            for r in rows:
                r["suggestion"] = None
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/tag-plan")
def tag_plan(
    account_id: Optional[int] = Query(None),
    sources: str = Query(",".join(DEFAULT_SOURCES), max_length=60),
    env: Optional[str] = Query(None),
    region: Optional[str] = Query(None, max_length=40),
    resource_type: Optional[str] = Query(None, max_length=60),
    current_user: dict = Depends(require_permission("environments.manage")),
):
    """An AWS CLI script that writes Environment=<tier> onto EC2 instances and EBS volumes whose
    environment CloudOps decided but whose AWS tag is missing or non-standard. Generated text only:
    nothing is executed and CloudOps changes nothing in AWS.

    Needs environments.manage, not just view: the script names AWS account numbers and resource ids, and
    it is meant for the person who will act on it. AWS accounts only; other clouds have no script yet."""
    chosen = tuple(x for x in (v.strip() for v in sources.split(",")) if x)
    if not chosen or any(x not in ("manual", "name", "related", "account") for x in chosen):
        raise HTTPException(status_code=400, detail="sources must be a list of manual, name, related, account")
    if env and env not in ec.TIERS:
        raise HTTPException(status_code=400, detail="env must be prod or nonprod")
    if resource_type and resource_type not in ("ec2", "ebs"):
        raise HTTPException(status_code=400, detail="the script covers ec2 and ebs only")
    scope_sql, scope_params = _scope(current_user)
    where = _LIVE_WHERE + scope_sql
    params = _live_params() + scope_params
    if account_id is not None:
        where += " AND r.aws_account_id = %s"; params.append(account_id)
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(f"""
                SELECT r.id, r.aws_account_id, aa.account_id, aa.account_name, r.resource_type, r.resource_id, r.name,
                       r.region, r.tags, r.env_resolved, r.env_source, r.tag_state
                FROM resources r
                JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active' AND aa.provider = 'aws'
                WHERE {where}
            """, params)
            rows = cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()
    plan = build_tag_plan(rows, sources=chosen, env=env, region=region or None, resource_type=resource_type)
    return {"script": plan["script"], "resources": plan["resources"], "commands": plan["commands"],
            "accounts": plan["accounts"], "not_covered": plan["not_covered"],
            "skipped_invalid": plan["skipped_invalid"], "sources": list(chosen),
            "by_env": plan["by_env"], "groups": plan["groups"],
            "filters": {"account_id": account_id, "env": env, "region": region, "resource_type": resource_type}}


def _synced_fragment(cur):
    """Tag-sync result column: NULL unless TAG_SYNC_ENABLED and migration 095 are in place (workstream B)."""
    try:
        from app.collector.tag_sync import synced_select
        return synced_select(cur)
    except Exception:
        return "NULL AS synced_tags"


_EMPTY_REPORT = {"applied": 0, "by_env": {}}


def _resync(**kw):
    """Re-resolve after a change that is ALREADY COMMITTED. A failure here must not turn a saved change into a
    500 with no audit entry: the discovery loop re-resolves every resource on its next run anyway."""
    try:
        return sync_resource_environments(**kw), None
    except Exception:
        logger.exception("environment re-resolve after a saved change failed")
        return dict(_EMPTY_REPORT), ("Saved, but the environments could not be re-evaluated just now; "
                                     "they refresh on the next discovery run.")


def _env_counts(cur, pks):
    cur.execute("SELECT COALESCE(env_resolved, 'unclassified') AS env, COUNT(*) AS n "
                "FROM resources WHERE id IN (" + ", ".join(["%s"] * len(pks)) + ") GROUP BY env", list(pks))
    return {r["env"]: int(r["n"]) for r in cur.fetchall()}


def _previous(cur, pks):
    """{pk: [env_resolved, env_source]} before a change, for the audit entry (it feeds the change history)."""
    if not pks:
        return {}
    cur.execute("SELECT id, env_resolved, env_source FROM resources WHERE id IN (" + ", ".join(["%s"] * len(pks)) + ")",
                list(pks))
    return {str(r["id"]): [r["env_resolved"], r["env_source"]] for r in cur.fetchall()}


def _group_pks(current_user, group):
    """-> (resource pks, group info) for every live resource of one account + resource type that is still in the
    Needs-classification queue. Only the queue: a resource somebody already placed, or that has a tag or a
    recognised name, is never touched by a group action."""
    if not isinstance(group, dict):
        raise HTTPException(status_code=400, detail="group must be an object with account_id and resource_type")
    try:
        if isinstance(group.get("account_id"), bool):
            raise ValueError
        account_id = int(group.get("account_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="group.account_id must be an integer")
    rtype = group.get("resource_type")
    if not isinstance(rtype, str) or not rtype.strip() or len(rtype) > 60:
        raise HTTPException(status_code=400, detail="group.resource_type must be a resource type name")
    rtype = rtype.strip()
    _require_account_access(current_user, [account_id])
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(f"""
                SELECT r.id FROM resources r
                JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
                WHERE {_LIVE_WHERE} AND {_QUEUE_WHERE} AND r.aws_account_id = %s AND r.resource_type = %s
                ORDER BY r.id LIMIT %s
            """, _live_params() + [account_id, rtype, MAX_GROUP + 1])
            ids = [int(r["id"]) for r in cur.fetchall()]
        finally:
            cur.close()
    finally:
        conn.close()
    if len(ids) > MAX_GROUP:
        raise HTTPException(status_code=400, detail=f"More than {MAX_GROUP} resources in that group; place them in smaller batches")
    if not ids:
        raise HTTPException(status_code=404, detail="Nothing of that type in that account is waiting for an environment any more")
    return ids, {"account_id": account_id, "resource_type": rtype}


@router.post("/classify")
def classify(request: Request, payload: dict = Body(...),
             current_user: dict = Depends(require_permission("environments.manage"))):
    env = str(payload.get("env", "")).strip().lower()
    if env not in _CHOICES:
        raise HTTPException(status_code=400, detail="env must be one of: prod, nonprod, clear")
    via = payload.get("via")
    if via is not None and via not in _VIA:
        raise HTTPException(status_code=400, detail="via must be 'suggestion' when given")
    group_info = None
    if payload.get("group") is not None:
        if env == "clear":
            raise HTTPException(status_code=400, detail="A group can be marked prod or nonprod, not cleared")
        pks, group_info = _group_pks(current_user, payload.get("group"))
    else:
        raw_ids = payload.get("resource_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise HTTPException(status_code=400, detail="resource_ids must be a non-empty list")
        if len(raw_ids) > MAX_BULK:
            raise HTTPException(status_code=400, detail=f"At most {MAX_BULK} resources per request")
        try:
            if any(isinstance(i, bool) for i in raw_ids):
                raise ValueError
            pks = sorted({int(i) for i in raw_ids})
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="resource_ids must be integers")

    marks = ", ".join(["%s"] * len(pks))
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(f"SELECT id, aws_account_id FROM resources WHERE id IN ({marks})", pks)
            found = cur.fetchall()
            if len(found) != len(pks):
                raise HTTPException(status_code=404, detail="One or more resources do not exist")
            accounts = sorted({r["aws_account_id"] for r in found})
            _require_account_access(current_user, accounts)

            before = _env_counts(cur, pks)
            previous = _previous(cur, pks[:AUDIT_ID_SAMPLE])
            if env == "clear":
                cur.execute(f"UPDATE resources SET env_override = NULL, env_override_by = NULL, "
                            f"env_override_at = NULL WHERE id IN ({marks})", pks)
            else:
                cur.execute(f"UPDATE resources SET env_override = %s, env_override_by = %s, "
                            f"env_override_at = UTC_TIMESTAMP() WHERE id IN ({marks})",
                            [env, current_user["username"], *pks])
            conn.commit()
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            logger.exception("environment classify failed")
            raise HTTPException(status_code=500, detail="Could not save the classification")
        finally:
            cur.close()
    finally:
        conn.close()

    # Re-resolve just these rows so env_resolved/env_source reflect the change now.
    report, sync_warning = _resync(resource_pks=pks)

    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            after = _env_counts(cur, pks)
        finally:
            cur.close()
    finally:
        conn.close()

    write_audit(
        current_user["username"],
        "Environment classification changed",
        role=str(current_user.get("role", "")).upper(),
        payload={
            "detail": f"{len(pks)} resource(s) set to '{env}'"
                      + (" (accepted suggestions)" if via == "suggestion" else "")
                      + (f" (every {group_info['resource_type']} still waiting in account {group_info['account_id']})"
                         if group_info else ""),
            "env": env, "count": len(pks), "account_ids": accounts, "group": group_info,
            "resource_ids": pks[:AUDIT_ID_SAMPLE],
            "resource_ids_truncated": len(pks) > AUDIT_ID_SAMPLE,
            "before": before, "after": after, "previous": previous, "via": via,
        },
        request=request,
    )
    out = {"status": "ok", "updated": len(pks), "env": env, "after": after,
           "reclassified": report["applied"]}
    if sync_warning:
        out["warning"] = sync_warning
    return out


@router.put("/accounts/{account_id}/default")
def set_account_default(account_id: int, request: Request, payload: dict = Body(...),
                        current_user: dict = Depends(require_permission("environments.manage"))):
    raw = payload.get("env")
    if raw is None or str(raw).strip().lower() in ("", "none", "clear"):
        new_default = None
    else:
        new_default = str(raw).strip().lower()
        if new_default not in ec.TIERS:
            raise HTTPException(status_code=400, detail="env must be prod, nonprod or null")
    _require_account_access(current_user, [account_id])

    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT id, account_name, default_env FROM aws_accounts WHERE id = %s", (account_id,))
            acc = cur.fetchone()
            if not acc:
                raise HTTPException(status_code=404, detail="Account not found")
            cur.execute("UPDATE aws_accounts SET default_env = %s WHERE id = %s", (new_default, account_id))
            conn.commit()
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            logger.exception("set account default env failed")
            raise HTTPException(status_code=500, detail="Could not save the account default")
        finally:
            cur.close()
    finally:
        conn.close()

    report, sync_warning = _resync(account_id=account_id)
    write_audit(
        current_user["username"],
        "Account default environment changed",
        role=str(current_user.get("role", "")).upper(),
        payload={
            "detail": f"{acc['account_name']}: default environment "
                      f"{acc['default_env'] or 'none'} -> {new_default or 'none'}",
            "account_id": account_id, "before": acc["default_env"], "after": new_default,
            "reclassified": report["applied"], "by_env": report["by_env"],
        },
        request=request,
    )
    out = {"status": "ok", "account_id": account_id, "default_env": new_default,
           "reclassified": report["applied"]}
    if sync_warning:
        out["warning"] = sync_warning
    return out


# ---------------------------------------------------------------------------------------------
# Governance (workstream C). Reads need environments.view and are limited to the caller's accounts; the one write
# in this section's neighbourhood (classify) is above and unchanged apart from the audit detail it records.
# ---------------------------------------------------------------------------------------------
def _allowed_accounts(current_user):
    """None = every account, otherwise a sorted list (possibly empty)."""
    accessible = get_accessible_account_ids(current_user)
    return None if accessible is None else sorted(accessible)


def _parse_default(raw):
    if raw is None or str(raw).strip().lower() in ("", "none", "clear"):
        return None
    value = str(raw).strip().lower()
    if value not in ec.TIERS:
        raise HTTPException(status_code=400, detail="env must be prod, nonprod or none")
    return value


@router.get("/accounts/{account_id}/default/preview")
def preview_account_default(account_id: int, env: Optional[str] = Query(None),
                            current_user: dict = Depends(require_permission("environments.view"))):
    """What PUT .../default with the same env would do, without doing it. Counts come from a dry run of the very
    code the PUT runs, so they are the figures the write then reports. Writes nothing and records no audit entry."""
    new_default = _parse_default(env)
    _require_account_access(current_user, [account_id])
    out = gov.preview_default(account_id, new_default)
    if out is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return out


@router.get("/resources/{resource_pk}/history")
def resource_history(resource_pk: int, current_user: dict = Depends(require_permission("environments.view"))):
    out = gov.resource_history(resource_pk, _allowed_accounts(current_user))
    if out is None:
        raise HTTPException(status_code=404, detail="Resource not found")
    return out


@router.get("/compliance")
def compliance(account_id: Optional[int] = Query(None),
               current_user: dict = Depends(require_permission("environments.view"))):
    if account_id is not None:
        _require_account_access(current_user, [account_id])
    return gov.compliance_report(_allowed_accounts(current_user), account_id)


@router.get("/compliance/trend")
def compliance_trend(account_id: Optional[int] = Query(None), days: int = Query(30, ge=1, le=365),
                     current_user: dict = Depends(require_permission("environments.view"))):
    if account_id is not None:
        _require_account_access(current_user, [account_id])
    try:
        points = gov.compliance_trend(_allowed_accounts(current_user), days, account_id)
    except Exception as e:
        if getattr(e, "errno", None) == 1146:           # migration 099 not applied yet
            return {"points": [], "available": False}
        raise
    return {"points": points, "available": True}


def _csv_response(text, filename):
    return Response(content="\ufeff" + text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


@router.get("/compliance.csv")
def compliance_csv(account_id: Optional[int] = Query(None),
                   current_user: dict = Depends(require_permission("environments.view"))):
    if account_id is not None:
        _require_account_access(current_user, [account_id])
    head, body = eg.compliance_csv_rows(gov.compliance_report(_allowed_accounts(current_user), account_id))
    return _csv_response(eg.csv_text(head, body), "environment-tag-compliance.csv")


@router.get("/tagging-gaps.csv")
def tagging_gaps_csv(account_id: Optional[int] = Query(None), resource_type: Optional[str] = Query(None, max_length=60),
                     essential_only: bool = Query(False),
                     current_user: dict = Depends(require_permission("environments.view"))):
    """Resources whose own tags are known and that lack at least one standard tag (docs/TAGGING_STANDARD.md), with
    the missing tag names. `essential_only` limits the check to the four tags CloudOps uses. Types whose tags
    CloudOps does not read are not listed: they are not_checked, not untagged."""
    if account_id is not None:
        _require_account_access(current_user, [account_id])
    rows, truncated = gov.tagging_gap_rows(_allowed_accounts(current_user), account_id, resource_type or None, essential_only)
    resp = _csv_response(eg.csv_text(gov.GAP_HEADER, rows), "tagging-gaps.csv")
    if truncated:
        resp.headers["X-Rows-Truncated"] = "true"
    return resp


@router.get("/digest/preview")
def digest_preview(current_user: dict = Depends(require_permission("environments.manage"))):
    """What the weekly digest of new unknown resources would do this week, per account and in the caller's scope.
    Addresses are never returned, only counts and the group name match. Sends nothing and writes nothing."""
    from app import env_digest
    out = env_digest.preview()
    allowed = _allowed_accounts(current_user)
    if allowed is not None:
        out["accounts"] = [a for a in out["accounts"] if a["account_id"] in set(allowed)]
    return out
