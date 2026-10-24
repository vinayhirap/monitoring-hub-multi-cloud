# app/api/host_agents.py
"""
Host agent, phase B: enrollment, enrollment keys, host list / revoke / config. (NOT the AI agent in app/agent/*.)

Every route answers 404 unless HOST_AGENT_ENABLED=true (router-level gate, checked before authentication so a
disabled feature reveals nothing). The router is always included in app/main.py so the routes appear in the RBAC
matrix.

  POST   /api/host-agents/enroll                    public (enrollment key), rate-limited per IP
  POST   /api/host-agents/ingest                    public (agent bearer token); implemented in app/hostagents/ingest.py
  GET    /api/host-agents                           hostagents.view, scoped by get_accessible_account_ids
  GET    /api/host-agents/{id}/metrics              hostagents.view, scoped the same way; latest stored reading per metric
  GET    /api/host-agents/{id}/services             hostagents.view, scoped the same way; watched services with their latest
                                                    state, plus the host's current config (what Configure edits)
  POST   /api/host-agents/enrollment-keys           hostagents.manage
  GET    /api/host-agents/enrollment-keys           hostagents.manage (never returns a key)
  DELETE /api/host-agents/enrollment-keys/{id}      hostagents.manage
  POST   /api/host-agents/{id}/revoke               hostagents.manage
  PATCH  /api/host-agents/{id}/config               hostagents.manage

Secrets: tokens and enrollment keys are returned exactly once, stored only as SHA-256, and never written to logs
or audit rows. Every create / revoke / config change / enrollment writes an audit row. Times are UTC.
"""
import json
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.account_names import account_label
from app.audit import write_audit
from app.auth.authorization import get_accessible_account_ids
from app.auth.permissions import require_permission
from app.auth.rate_limit import check_rate_limit, _client_ip
from app.db import get_connection
from app.hostagents import auth as agent_auth
from app.hostagents import metric_map, settings

logger = logging.getLogger(__name__)


def host_agents_enabled_gate():
    # Deliberately not named require_*: the RBAC matrix would list the public routes as "custom" guards.
    if not settings.enabled():
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(prefix="/api/host-agents", tags=["Host Agents"],
                   dependencies=[Depends(host_agents_enabled_gate)])

_SAFE_TEXT = r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]*$"
_HOSTNAME = r"^[A-Za-z0-9][A-Za-z0-9._\-]*$"
_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:\-]*$")
MAX_WATCH_SERVICES = 100
INVALID_KEY = "Invalid or expired enrollment key"


# ── request bodies ───────────────────────────────────────────────────────────

class EnrollRequest(BaseModel):
    enrollment_key: str = Field(min_length=1, max_length=64)
    hostname: str = Field(min_length=1, max_length=255, pattern=_HOSTNAME)
    instance_id: Optional[str] = Field(default=None, max_length=64, pattern=_SAFE_TEXT)
    os: str = Field(min_length=1, max_length=40, pattern=_SAFE_TEXT)
    arch: str = Field(min_length=1, max_length=20, pattern=_SAFE_TEXT)
    agent_version: str = Field(min_length=1, max_length=40, pattern=_SAFE_TEXT)


class EnrollmentKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aws_account_id: int = Field(ge=1)
    label: Optional[str] = Field(default=None, max_length=100)
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_in_hours: int = Field(default=24, ge=1, le=8760)


class HostConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    watch_services: Optional[List[str]] = Field(default=None, max_length=MAX_WATCH_SERVICES)
    interval_seconds: Optional[int] = Field(default=None, ge=settings.INTERVAL_MIN_SECONDS,
                                            le=settings.INTERVAL_MAX_SECONDS)


# ── helpers ──────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() + "Z" if isinstance(value, datetime) else str(value)


def _actor(user: dict) -> dict:
    return {"actor": user.get("username"), "role": str(user.get("role") or "").upper() or None}


def _in_scope(accessible, account_id) -> bool:
    """accessible is None (full access) or a set of aws_accounts.id."""
    return accessible is None or account_id in accessible


def _parse_json(raw) -> dict:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _effective_config(config_json) -> dict:
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in settings.DEFAULT_CONFIG.items()}
    cfg.update({k: v for k, v in _parse_json(config_json).items() if k in cfg})
    return cfg


def _derive_state(status, last_seen_at, now: datetime, stale: int) -> str:
    """online: seen within stale seconds | stale: up to 5x that | offline: beyond (or never seen) | revoked."""
    if status == "revoked":
        return "revoked"
    if last_seen_at is None:
        return "offline"
    age = (now - last_seen_at).total_seconds()
    if age <= stale:
        return "online"
    return "stale" if age <= stale * 5 else "offline"


def _key_status(row: dict, now: datetime) -> str:
    if row["revoked"]:
        return "revoked"
    if row["expires_at"] is not None and row["expires_at"] <= now:
        return "expired"
    if row["used_count"] >= row["max_uses"]:
        return "exhausted"
    return "active"


def _account_exists(cur, account_id: int) -> bool:
    cur.execute("SELECT id FROM aws_accounts WHERE id = %s", (account_id,))
    return cur.fetchone() is not None


def _scoped_clause(accessible, column: str):
    """-> (sql fragment starting with ' WHERE ' or '', params). An empty set means no accounts at all."""
    if accessible is None:
        return "", ()
    ids = sorted(accessible)
    if not ids:
        return " WHERE 1 = 0", ()
    return f" WHERE {column} IN ({', '.join(['%s'] * len(ids))})", tuple(ids)


# ── enroll (public; the enrollment key is the credential) ────────────────────

@router.post("/enroll")
def enroll(body: EnrollRequest, request: Request):
    check_rate_limit(f"host-agent-enroll:{_client_ip(request)}", settings.enroll_rate_per_hour(), 3600)
    if not agent_auth.looks_like_enrollment_key(body.enrollment_key):
        raise HTTPException(status_code=401, detail=INVALID_KEY)
    key_hash = agent_auth.hash_secret(body.enrollment_key)
    token = agent_auth.generate_token()

    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            # One atomic conditional UPDATE is the whole validity check: two concurrent enrolls on a
            # single-use key cannot both see rowcount 1.
            cur.execute(
                "UPDATE host_agent_enrollment_keys SET used_count = used_count + 1 "
                "WHERE key_hash = %s AND revoked = 0 AND used_count < max_uses "
                "AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())",
                (key_hash,),
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise HTTPException(status_code=401, detail=INVALID_KEY)
            cur.execute("SELECT id, aws_account_id FROM host_agent_enrollment_keys WHERE key_hash = %s", (key_hash,))
            key_row = cur.fetchone()
            if not key_row or not _account_exists(cur, key_row["aws_account_id"]):
                conn.rollback()
                raise HTTPException(status_code=401, detail=INVALID_KEY)
            account_id = key_row["aws_account_id"]

            # Atomic upsert (no SELECT .. FOR UPDATE gap-lock deadlock between two first enrolls of one host).
            cur.execute(
                "INSERT INTO host_agents (aws_account_id, hostname, instance_id, os, arch, agent_version, status, last_seq) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'active', 0) "
                "ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id), instance_id = VALUES(instance_id), os = VALUES(os), "
                "arch = VALUES(arch), agent_version = VALUES(agent_version), status = 'active', last_seq = 0",
                (account_id, body.hostname, body.instance_id, body.os, body.arch, body.agent_version),
            )
            host_id = cur.lastrowid
            cur.execute("SELECT COUNT(*) AS n FROM host_agent_tokens WHERE host_agent_id = %s", (host_id,))
            had_tokens = (cur.fetchone() or {}).get("n", 0) > 0
            cur.execute("UPDATE host_agent_tokens SET revoked_at = UTC_TIMESTAMP() "
                        "WHERE host_agent_id = %s AND revoked_at IS NULL", (host_id,))
            rotated = cur.rowcount
            cur.execute("INSERT INTO host_agent_tokens (host_agent_id, token_hash) VALUES (%s, %s)",
                        (host_id, agent_auth.hash_secret(token)))
            cur.execute("SELECT config_json FROM host_agents WHERE id = %s", (host_id,))
            config = _effective_config((cur.fetchone() or {}).get("config_json"))
            conn.commit()
        finally:
            cur.close()
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

    kind = "re-enrolled (previous tokens revoked)" if had_tokens else "enrolled"
    write_audit("host-agent", "Host agent enrolled",
                f"Host agent {host_id} {kind} in {account_label(account_id)} via enrollment key {key_row['id']}; "
                f"{rotated} earlier token(s) revoked", role="AGENT", request=request)
    logger.info("[host_agents] host_agent_id=%s %s", host_id, "re-enrolled" if had_tokens else "enrolled")
    return {"host_agent_id": host_id, "agent_token": token, "config": config}


# ── list hosts ───────────────────────────────────────────────────────────────

@router.get("")
def list_host_agents(current_user: dict = Depends(require_permission("hostagents.view"))):
    where, params = _scoped_clause(get_accessible_account_ids(current_user), "h.aws_account_id")
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                "SELECT h.id, h.aws_account_id, a.account_name, h.hostname, h.instance_id, h.os, h.arch, "
                "h.agent_version, h.status, h.last_seen_at FROM host_agents h "
                "LEFT JOIN aws_accounts a ON a.id = h.aws_account_id" + where + " ORDER BY h.hostname, h.id", params)
            rows = cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()
    now, stale = _utcnow(), settings.stale_seconds()
    return [{
        "id": r["id"], "aws_account_id": r["aws_account_id"], "account": r.get("account_name"),
        "hostname": r["hostname"], "instance_id": r["instance_id"], "os": r["os"], "arch": r["arch"],
        "agent_version": r["agent_version"], "status": r["status"], "last_seen_at": _iso(r["last_seen_at"]),
        "state": _derive_state(r["status"], r["last_seen_at"], now, stale),
    } for r in rows]


# ── current metrics of one host ──────────────────────────────────────────────

HOST_RESOURCE_PREFIX = "host:"          # same value as ingest.HOST_RESOURCE_PREFIX (resources.resource_id of a 'host' row)
HISTORY_FALLBACK_HOURS = 24

SQL_HOST_FOR_METRICS = (
    "SELECT id, aws_account_id, hostname, status, resource_id, last_seen_at FROM host_agents WHERE id = %s")
SQL_METRICS_RESOURCE = (
    "SELECT id, resource_type FROM resources WHERE aws_account_id = %s AND resource_type = %s AND resource_id = %s")
SQL_METRICS_LAST = "SELECT metric_name, metric_value, metric_timestamp FROM metrics WHERE resource_id = %s"
SQL_METRICS_HISTORY_LATEST = (
    "SELECT h.metric_name, h.metric_value, h.metric_timestamp FROM metric_history h "
    "JOIN (SELECT metric_name, MAX(metric_timestamp) AS ts FROM metric_history "
    "WHERE resource_id = %s AND metric_timestamp >= UTC_TIMESTAMP() - INTERVAL " + str(HISTORY_FALLBACK_HOURS) + " HOUR "
    "GROUP BY metric_name) m ON m.metric_name = h.metric_name AND m.ts = h.metric_timestamp "
    "WHERE h.resource_id = %s")


def _agent_rows(rows):
    """Keep only rows whose name a host agent can feed, with the display fields; newest row wins per name."""
    out = {}
    for r in rows:
        name, value, ts = r["metric_name"], r["metric_value"], r["metric_timestamp"]
        info = metric_map.describe_stored_name(name)
        if info is None or value is None or ts is None:
            continue
        cur = out.get(name)
        if cur is None or ts >= cur["ts"]:
            out[name] = {"info": info, "name": name, "value": float(value), "ts": ts}
    return out


@router.get("/{host_id}/metrics")
def host_agent_metrics(host_id: int, current_user: dict = Depends(require_permission("hostagents.view"))):
    """Latest stored reading per agent metric for one host. DB read only: no cloud call, nothing computed or estimated.

    The rows come from `metrics` (the last-value row the ingest upserts). Only when that table holds none of this host's
    agent series does the newest row of the last 24 h of `metric_history` stand in. Names an agent cannot feed (e.g. the
    CloudWatch series of an attached EC2 resource) are never returned. `shared_series` is true when the host is attached to
    an EC2 resource: mem_used_percent / disk_used_percent* are the same stored names the CloudWatch agent collector writes,
    so for those the stored value may come from either source (the table cannot tell them apart)."""
    accessible = get_accessible_account_ids(current_user)
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(SQL_HOST_FOR_METRICS, (host_id,))
            host = cur.fetchone()
            if not host or not _in_scope(accessible, host["aws_account_id"]):
                raise HTTPException(status_code=404, detail="Host agent not found")
            stored_rid = host["resource_id"]
            resource_type = None
            found = {}
            source = None
            if stored_rid:
                resource_type = "host" if stored_rid.startswith(HOST_RESOURCE_PREFIX) else "ec2"
                cur.execute(SQL_METRICS_RESOURCE, (host["aws_account_id"], resource_type, stored_rid))
                res = cur.fetchone()
                if res:
                    cur.execute(SQL_METRICS_LAST, (res["id"],))
                    found = _agent_rows(cur.fetchall())
                    source = "metrics"
                    if not found:
                        cur.execute(SQL_METRICS_HISTORY_LATEST, (res["id"], res["id"]))
                        found = _agent_rows(cur.fetchall())
                        source = "metric_history" if found else None
        finally:
            cur.close()
    finally:
        conn.close()

    now = _utcnow()
    items = sorted(found.values(), key=lambda m: (m["info"]["order"], m["info"]["label"] or "", m["name"]))
    return {
        "host_agent_id": host["id"],
        "hostname": host["hostname"],
        "resource_type": resource_type,
        "shared_series": resource_type == "ec2",
        "source": source,
        "metrics": [{
            "name": m["name"],
            "metric": m["info"]["contract"],
            "title": m["info"]["title"],
            "label": m["info"]["label"],
            "label_key": m["info"]["label_key"],
            "unit": m["info"]["unit"],
            "value": m["value"],
            "timestamp": _iso(m["ts"]),
            "age_seconds": max(0, int((now - m["ts"]).total_seconds())),
        } for m in items],
    }


# ── watched services + current config of one host ────────────────────────────

SQL_HOST_FOR_SERVICES = (
    "SELECT id, aws_account_id, hostname, status, config_version, config_json FROM host_agents WHERE id = %s")
SQL_SERVICES = ("SELECT service_name, state, state_since, observed_at FROM host_agent_services "
                "WHERE host_agent_id = %s ORDER BY service_name")


def _service_view(rows, watch, now: datetime) -> dict:
    """Pure. Rows are stored states; `watch` is the configured list. Only configured names are shown (a row for a name
    that is no longer configured is a leftover and must not be presented as watched); a configured name with no row yet
    is `pending`, never a guessed state."""
    watched = list(watch or [])
    wanted = set(watched)
    by_name = {r["service_name"]: r for r in rows if r["service_name"] in wanted}
    items = []
    for name in watched:                                   # the admin's order, not alphabetical
        r = by_name.get(name)
        if r is None:
            continue
        since, seen = r["state_since"], r["observed_at"]
        items.append({
            "name": name, "state": r["state"],
            "since": _iso(since), "observed_at": _iso(seen),
            "since_seconds": max(0, int((now - since).total_seconds())) if since else None,
            "age_seconds": max(0, int((now - seen).total_seconds())) if seen else None,
        })
    summary = {"total": len(watched), "running": 0, "stopped": 0, "failed": 0, "unknown": 0, "pending": 0}
    for it in items:
        summary[it["state"]] = summary.get(it["state"], 0) + 1
    pending = [n for n in watched if n not in by_name]
    summary["pending"] = len(pending)
    return {"services": items, "pending": pending, "summary": summary}


@router.get("/{host_id}/services")
def host_agent_services(host_id: int, current_user: dict = Depends(require_permission("hostagents.view"))):
    """The host's configured watch list and interval (the values the Configure dialog edits, so it can show them instead of
    asking for blind input) and the latest stored state of each watched service. DB read only. `observed_at` is when the
    agent last reported the state; it is not live, and for an offline host it is simply the last thing the agent said."""
    accessible = get_accessible_account_ids(current_user)
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(SQL_HOST_FOR_SERVICES, (host_id,))
            host = cur.fetchone()
            if not host or not _in_scope(accessible, host["aws_account_id"]):
                raise HTTPException(status_code=404, detail="Host agent not found")
            cur.execute(SQL_SERVICES, (host_id,))
            rows = cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()
    cfg = _effective_config(host["config_json"])
    out = _service_view(rows, cfg.get("watch_services"), _utcnow())
    return {
        "host_agent_id": host["id"], "hostname": host["hostname"], "status": host["status"],
        "config_version": host["config_version"],
        "interval_seconds": cfg.get("interval_seconds"), "watch_services": list(cfg.get("watch_services") or []),
        **out,
    }


# ── enrollment keys ──────────────────────────────────────────────────────────

@router.post("/enrollment-keys")
def create_enrollment_key(body: EnrollmentKeyCreate, request: Request,
                          current_user: dict = Depends(require_permission("hostagents.manage"))):
    accessible = get_accessible_account_ids(current_user)
    if not _in_scope(accessible, body.aws_account_id):
        raise HTTPException(status_code=404, detail="Account not found")
    key = agent_auth.generate_enrollment_key()
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            if not _account_exists(cur, body.aws_account_id):
                raise HTTPException(status_code=404, detail="Account not found")
            cur.execute(
                "INSERT INTO host_agent_enrollment_keys (aws_account_id, key_hash, label, max_uses, expires_at, created_by) "
                "VALUES (%s, %s, %s, %s, UTC_TIMESTAMP() + INTERVAL %s HOUR, %s)",
                (body.aws_account_id, agent_auth.hash_secret(key), body.label, body.max_uses,
                 body.expires_in_hours, current_user.get("username")))
            key_id = cur.lastrowid
            conn.commit()
        finally:
            cur.close()
    finally:
        conn.close()
    write_audit(current_user.get("username"), "Host agent enrollment key created",
                f"Enrollment key {key_id} for {account_label(body.aws_account_id)}: label={body.label!r}, "
                f"max_uses={body.max_uses}, expires_in_hours={body.expires_in_hours}",
                role=_actor(current_user)["role"], request=request)
    return {"key": key, "id": key_id}


@router.get("/enrollment-keys")
def list_enrollment_keys(current_user: dict = Depends(require_permission("hostagents.manage"))):
    where, params = _scoped_clause(get_accessible_account_ids(current_user), "k.aws_account_id")
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                "SELECT k.id, k.aws_account_id, a.account_name, k.label, k.max_uses, k.used_count, k.expires_at, "
                "k.revoked, k.created_by, k.created_at FROM host_agent_enrollment_keys k "
                "LEFT JOIN aws_accounts a ON a.id = k.aws_account_id" + where + " ORDER BY k.id DESC", params)
            rows = cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()
    now = _utcnow()
    return [{
        "id": r["id"], "aws_account_id": r["aws_account_id"], "account": r.get("account_name"),
        "label": r["label"], "max_uses": r["max_uses"], "used_count": r["used_count"],
        "expires_at": _iso(r["expires_at"]), "revoked": bool(r["revoked"]), "status": _key_status(r, now),
        "created_by": r["created_by"], "created_at": _iso(r["created_at"]),
    } for r in rows]


@router.delete("/enrollment-keys/{key_id}")
def revoke_enrollment_key(key_id: int, request: Request,
                          current_user: dict = Depends(require_permission("hostagents.manage"))):
    accessible = get_accessible_account_ids(current_user)
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT id, aws_account_id, revoked FROM host_agent_enrollment_keys WHERE id = %s FOR UPDATE",
                        (key_id,))
            row = cur.fetchone()
            if not row or not _in_scope(accessible, row["aws_account_id"]):
                conn.rollback()
                raise HTTPException(status_code=404, detail="Enrollment key not found")
            changed = not row["revoked"]
            if changed:
                cur.execute("UPDATE host_agent_enrollment_keys SET revoked = 1 WHERE id = %s", (key_id,))
            conn.commit()
        finally:
            cur.close()
    finally:
        conn.close()
    if changed:
        write_audit(current_user.get("username"), "Host agent enrollment key revoked",
                    f"Enrollment key {key_id} for {account_label(row['aws_account_id'])} revoked",
                    role=_actor(current_user)["role"], request=request)
    return {"id": key_id, "revoked": True}


# ── revoke a host / change its config ────────────────────────────────────────

@router.post("/{host_id}/revoke")
def revoke_host_agent(host_id: int, request: Request,
                      current_user: dict = Depends(require_permission("hostagents.manage"))):
    accessible = get_accessible_account_ids(current_user)
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT id, aws_account_id, hostname, status FROM host_agents WHERE id = %s FOR UPDATE", (host_id,))
            row = cur.fetchone()
            if not row or not _in_scope(accessible, row["aws_account_id"]):
                conn.rollback()
                raise HTTPException(status_code=404, detail="Host agent not found")
            cur.execute("UPDATE host_agent_tokens SET revoked_at = UTC_TIMESTAMP() "
                        "WHERE host_agent_id = %s AND revoked_at IS NULL", (host_id,))
            tokens_revoked = cur.rowcount
            was_active = row["status"] != "revoked"
            if was_active:
                cur.execute("UPDATE host_agents SET status = 'revoked' WHERE id = %s", (host_id,))
            conn.commit()
        finally:
            cur.close()
    finally:
        conn.close()
    if was_active or tokens_revoked:
        write_audit(current_user.get("username"), "Host agent revoked",
                    f"Host agent {host_id} ({row['hostname']}) in {account_label(row['aws_account_id'])} revoked; "
                    f"{tokens_revoked} token(s) revoked", role=_actor(current_user)["role"], request=request)
    return {"id": host_id, "status": "revoked", "tokens_revoked": tokens_revoked}


@router.patch("/{host_id}/config")
def patch_host_agent_config(host_id: int, body: HostConfigPatch, request: Request,
                            current_user: dict = Depends(require_permission("hostagents.manage"))):
    changes = {}
    if body.watch_services is not None:
        services = []
        for name in body.watch_services:
            name = name.strip()
            if len(name) > 100 or not _SERVICE_RE.match(name):
                raise HTTPException(status_code=422, detail=f"Invalid service name: {name[:40]!r}")
            if name not in services:
                services.append(name)
        changes["watch_services"] = services
    if body.interval_seconds is not None:
        changes["interval_seconds"] = body.interval_seconds
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to change: send watch_services and/or interval_seconds")

    accessible = get_accessible_account_ids(current_user)
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT id, aws_account_id, hostname, status, config_version, config_json "
                        "FROM host_agents WHERE id = %s FOR UPDATE", (host_id,))
            row = cur.fetchone()
            if not row or not _in_scope(accessible, row["aws_account_id"]):
                conn.rollback()
                raise HTTPException(status_code=404, detail="Host agent not found")
            if row["status"] == "revoked":
                conn.rollback()
                raise HTTPException(status_code=409, detail="Host agent is revoked")
            stored = {k: v for k, v in _parse_json(row["config_json"]).items()
                      if k in ("watch_services", "interval_seconds")}
            stored.update(changes)
            cur.execute("UPDATE host_agents SET config_json = %s, config_version = config_version + 1 WHERE id = %s",
                        (json.dumps(stored), host_id))
            if "watch_services" in changes:
                # A service taken off the list must disappear now, not linger until the agent next reports.
                kept = changes["watch_services"]
                if kept:
                    cur.execute("DELETE FROM host_agent_services WHERE host_agent_id = %s AND service_name NOT IN ("
                                + ", ".join(["%s"] * len(kept)) + ")", (host_id, *kept))
                else:
                    cur.execute("DELETE FROM host_agent_services WHERE host_agent_id = %s", (host_id,))
            conn.commit()
            new_version = row["config_version"] + 1
        finally:
            cur.close()
    finally:
        conn.close()
    write_audit(current_user.get("username"), "Host agent config changed",
                f"Host agent {host_id} ({row['hostname']}) in {account_label(row['aws_account_id'])}: "
                f"{json.dumps(changes)} (config_version {new_version})",
                role=_actor(current_user)["role"], request=request)
    return {"id": host_id, "config_version": new_version, "config": _effective_config(stored)}
