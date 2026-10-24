# app/hostagents/ingest.py
"""
POST /api/host-agents/ingest  --  Phase C of the host agent (a small binary on a server that PUSHES metrics; not the AI
agent in app/agent/*).

Request order (cheapest rejection first, nothing touches the database before authentication):
  1 flag off -> 404            2 Content-Length / streamed size cap -> 413     3 Bearer token shape -> 401
  4 authenticate_agent()       5 per-agent rate limit -> 429                    6 JSON + payload validation -> 400/413
  7 one DB transaction on ONE connection:
        heartbeat (no samples)  -> a single UPDATE of host_agents
        samples                 -> claim seq (UPDATE) -> resolve resource -> executemany metrics -> executemany history
                                   -> COMMIT once; duplicates (seq <= last_seq within 10 min) answer identically, write nothing

Identity comes from the token (authenticate_agent), never from the payload: the payload hostname is ignored and the
payload instance_id only selects an attach candidate inside the agent's own account.

RESOURCE MAPPING (decision): host.instance_id matching resources(aws_account_id, 'ec2', instance_id) attaches to that row
(discovery keeps owning its name/tags/state; the agent never writes them). Otherwise a resource of NEW type 'host' keyed
'host:<hostname>' is created. 'host' is safe because every existing resource_type filter is either an allowlist of real
types (core collector, EC2 pages) which simply never selects it, or a NOT IN list that includes it harmlessly; the alert
cadence class treats unknown AWS types as 'extended'. host_agents.resource_id stores resources.resource_id ('i-...' when
attached, 'host:<hostname>' otherwise), which is how the type is told apart without another column.
"""
import json
import logging
import re
import threading
import zlib
from contextlib import contextmanager
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.db import get_connection
from app.hostagents import metric_map, prefer, settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/host-agents", tags=["Host agents"])

TOKEN_RE = re.compile(r"^cah_[A-Za-z0-9_-]{40}$")
DUP_WINDOW_SECONDS = 600
HOST_RESOURCE_TYPE = "host"
HOST_RESOURCE_PREFIX = "host:"
RETRYABLE_ERRNOS = {1213, 1205}          # deadlock / lock wait timeout, same set as app/collector/metrics_writer.py
SLOT_WAIT_SECONDS = 5

# Single source of truth is settings.DEFAULT_CONFIG (phase B); stored config_json overrides these keys.
DEFAULT_CONFIG = settings.DEFAULT_CONFIG

_SLOTS = threading.BoundedSemaphore(settings.ingest_concurrency())
_REGISTERED = set()                      # (account_id, metric_name) already registered by this process

SQL_HEARTBEAT = (
    # last_seq is assigned FIRST on purpose: MySQL and MariaDB evaluate single-table UPDATE assignments left to right,
    # so the IF() must read last_seen_at before the next assignment overwrites it.
    "UPDATE host_agents SET "
    "last_seq = IF(%s > last_seq OR last_seen_at IS NULL OR last_seen_at < UTC_TIMESTAMP() - INTERVAL " + str(DUP_WINDOW_SECONDS) + " SECOND, %s, last_seq), "
    "last_seen_at = UTC_TIMESTAMP(), agent_version = %s "
    "WHERE id = %s AND status = 'active'"
)
SQL_CLAIM_SEQ = (
    "UPDATE host_agents SET last_seq = %s, last_seen_at = UTC_TIMESTAMP(), agent_version = %s "
    "WHERE id = %s AND status = 'active' AND "
    "(last_seq < %s OR last_seen_at IS NULL OR last_seen_at < UTC_TIMESTAMP() - INTERVAL " + str(DUP_WINDOW_SECONDS) + " SECOND)"
)
SQL_TOUCH = "UPDATE host_agents SET last_seen_at = UTC_TIMESTAMP(), agent_version = %s WHERE id = %s AND status = 'active'"
SQL_UPSERT_METRICS = (
    "INSERT INTO metrics (resource_id, metric_name, metric_value, metric_timestamp) VALUES (%s, %s, %s, %s) "
    "ON DUPLICATE KEY UPDATE metric_value = VALUES(metric_value), metric_timestamp = VALUES(metric_timestamp)"
)
SQL_UPSERT_HISTORY = (
    "INSERT INTO metric_history (resource_id, metric_name, metric_value, metric_timestamp) VALUES (%s, %s, %s, %s) "
    "ON DUPLICATE KEY UPDATE metric_value = VALUES(metric_value)"
)


# ── HTTP layer ───────────────────────────────────────────────────────────

async def _read_capped(request: Request) -> bytes:
    cap = settings.max_body_bytes()
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit():
            raise HTTPException(400, "invalid Content-Length")
        if int(declared) > cap:
            raise HTTPException(413, "payload too large")
    chunks, total = [], 0
    async for chunk in request.stream():          # also caps a body whose Content-Length is absent or wrong
        total += len(chunk)
        if total > cap:
            raise HTTPException(413, "payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_body(raw: bytes, encoding: str) -> bytes:
    """Content-Encoding: gzip is what the agent sends by default (agent.json no_gzip turns it off); identity / absent is
    plain JSON. Anything else is 415. The cap applies to the DECOMPRESSED size as well, so a few-KB bomb cannot expand
    past HOST_AGENT_MAX_BODY_BYTES (413); a corrupt or truncated stream is 400."""
    enc = (encoding or "").strip().lower()
    if enc in ("", "identity"):
        return raw
    if enc != "gzip":
        raise HTTPException(415, "unsupported Content-Encoding (use gzip or none)")
    cap = settings.max_body_bytes()
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(raw, cap + 1)
    except zlib.error:
        raise HTTPException(400, "body is not valid gzip")
    if len(out) > cap:
        raise HTTPException(413, "payload too large")
    if not d.eof:
        raise HTTPException(400, "body is not valid gzip (truncated)")
    return out


def _bearer(request: Request) -> str:
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not TOKEN_RE.match(token):
        raise HTTPException(401, "invalid or missing agent token", headers={"WWW-Authenticate": "Bearer"})
    return token


def _authenticate(token: str):
    from app.hostagents.auth import authenticate_agent      # phase B; imported inside the call so tests can stub it
    try:
        return authenticate_agent(token)
    except Exception as e:
        logger.error("authenticate_agent failed: %s", e)
        raise HTTPException(503, "host agent authentication is temporarily unavailable")


def _rate_limit(host_agent_id) -> None:
    from app.auth.rate_limit import check_rate_limit          # raises 429; fails open when Redis is down
    check_rate_limit(f"hostagent:ingest:{int(host_agent_id)}", settings.ingest_rate_per_min(), 60)


@router.post("/ingest")
async def ingest(request: Request):
    if not settings.enabled():
        raise HTTPException(404, "Not Found")
    raw = await _read_capped(request)
    token = _bearer(request)
    agent = await run_in_threadpool(_authenticate, token)
    if not agent:
        raise HTTPException(401, "invalid or missing agent token", headers={"WWW-Authenticate": "Bearer"})
    await run_in_threadpool(_rate_limit, agent["host_agent_id"])

    raw = _decode_body(raw, request.headers.get("content-encoding", ""))
    try:
        body = json.loads(raw)
    except Exception:
        raise HTTPException(400, "body is not valid JSON")
    try:
        max_batch = DEFAULT_CONFIG["max_batch_metrics"]
        clean, rejected = metric_map.validate_payload(body, max_batch_metrics=max_batch)
    except metric_map.PayloadError as e:
        raise HTTPException(e.status, e.message)
    return await run_in_threadpool(process_ingest, agent, clean, rejected, request)


# ── database layer ───────────────────────────────────────────────────────

@contextmanager
def _slot():
    if not _SLOTS.acquire(timeout=SLOT_WAIT_SECONDS):
        raise HTTPException(503, "ingest busy, retry shortly", headers={"Retry-After": str(SLOT_WAIT_SECONDS)})
    try:
        yield
    finally:
        _SLOTS.release()


def process_ingest(agent: dict, clean: dict, rejected: int, request=None) -> dict:
    """Run the transaction (retrying deadlocks) and build the response. One connection per call."""
    with _slot():
        attempts = 3
        for attempt in range(1, attempts + 1):
            conn = get_connection()
            try:
                result = _transact(conn, agent, clean)
                conn.commit()
                break
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                if getattr(e, "errno", None) in RETRYABLE_ERRNOS and attempt < attempts:
                    logger.warning("host agent ingest deadlock (attempt %d/%d): %s", attempt, attempts, e)
                    continue
                logger.error("host agent ingest failed [agent %s]: %s", agent.get("host_agent_id"), e)
                raise HTTPException(500, "ingest failed")
            finally:
                conn.close()

    _after_commit(agent, clean, result, request)
    resp = {"accepted": len(clean["samples"]), "rejected": rejected, "config_version": result["config_version"]}
    if result.get("config") is not None:
        resp["config"] = result["config"]
    return resp


def _transact(conn, agent, clean):
    cur = conn.cursor(dictionary=True)
    try:
        agent_id, seq, version = agent["host_agent_id"], clean["seq"], clean["agent_version"]
        samples = clean["samples"]
        out = {"config_version": agent.get("config_version"), "resource_db_id": None, "events": [], "fed": []}

        if not samples:                                   # HEARTBEAT: exactly one UPDATE
            cur.execute(SQL_HEARTBEAT, (seq, seq, version, agent_id))
        else:
            cur.execute(SQL_CLAIM_SEQ, (seq, version, agent_id, seq))
            if cur.rowcount == 0:                         # duplicate (or revoked meanwhile): alive, but write nothing
                cur.execute(SQL_TOUCH, (version, agent_id))
            else:
                rid, rtype, events = _resolve_resource(cur, agent, clean)
                _write_samples(cur, rid, samples)
                out.update(resource_db_id=rid, resource_type=rtype, events=events,
                           fed=sorted({s[0] for s in samples}))

        if clean["config_version"] != agent.get("config_version"):
            cur.execute("SELECT config_json, config_version FROM host_agents WHERE id = %s", (agent_id,))
            row = cur.fetchone()
            if row:
                out["config_version"] = row["config_version"]
                if clean["config_version"] != row["config_version"]:
                    out["config"] = _merged_config(row.get("config_json"))
        return out
    finally:
        cur.close()


def _merged_config(stored) -> dict:
    if isinstance(stored, (bytes, bytearray)):
        stored = stored.decode("utf-8", "replace")
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except ValueError:
            stored = None
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_CONFIG.items()}
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
    return cfg


def _find_ec2(cur, account_id, instance_id):
    cur.execute("SELECT id FROM resources WHERE aws_account_id = %s AND resource_type = 'ec2' AND resource_id = %s",
                (account_id, instance_id))
    row = cur.fetchone()
    return row["id"] if row else None


def _resolve_resource(cur, agent, clean):
    """-> (resources.id, resource_type, audit_events). Attaches to an EC2 row when it can, else uses a 'host' resource."""
    account_id, agent_id, hostname = agent["aws_account_id"], agent["host_agent_id"], agent["hostname"]
    current = agent.get("resource_id")
    instance_id = clean["host"]["instance_id"]
    events = []

    if current and not current.startswith(HOST_RESOURCE_PREFIX):          # already attached to an EC2 row
        rid = _find_ec2(cur, account_id, current)
        if rid:
            return rid, "ec2", events

    if instance_id:                                                       # (re)try attaching
        rid = _find_ec2(cur, account_id, instance_id)
        if rid:
            cur.execute("SELECT 1 AS x FROM host_agents WHERE aws_account_id = %s AND resource_id = %s "
                        "AND id <> %s AND status = 'active' LIMIT 1", (account_id, instance_id, agent_id))
            if cur.fetchone() is None:                                    # another agent must not hijack an attached row
                cur.execute("UPDATE host_agents SET resource_id = %s, instance_id = COALESCE(instance_id, %s) "
                            "WHERE id = %s", (instance_id, instance_id, agent_id))
                events.append(("Host agent attached to EC2 resource",
                               f"host {hostname} -> {instance_id} (account {account_id})"))
                return rid, "ec2", events

    host_rid = (current if current and current.startswith(HOST_RESOURCE_PREFIX)
                else HOST_RESOURCE_PREFIX + hostname)[:512]
    host = clean["host"]
    tags = json.dumps({"_host_agent": True, "_hostname": hostname, "_os": host.get("os"), "_arch": host.get("arch"),
                       "_agent_version": clean["agent_version"]})
    cur.execute(
        "INSERT INTO resources (aws_account_id, resource_type, resource_id, name, tags, last_seen_at) "
        "VALUES (%s, %s, %s, %s, %s, NOW()) "
        "ON DUPLICATE KEY UPDATE name = VALUES(name), tags = VALUES(tags), last_seen_at = NOW()",
        (account_id, HOST_RESOURCE_TYPE, host_rid, hostname[:255], tags))
    cur.execute("SELECT id FROM resources WHERE aws_account_id = %s AND resource_type = %s AND resource_id = %s",
                (account_id, HOST_RESOURCE_TYPE, host_rid))
    rid = cur.fetchone()["id"]
    if host_rid != current:
        cur.execute("UPDATE host_agents SET resource_id = %s WHERE id = %s", (host_rid, agent_id))
        events.append(("Host agent resource created", f"host {hostname} -> {host_rid} (account {account_id})"))
    return rid, HOST_RESOURCE_TYPE, events


def _write_samples(cur, rid, samples):
    now = datetime.utcnow()
    latest = metric_map.newest_per_series(samples)
    last_rows = sorted(((rid, name, round(v, 6), now) for name, (v, _ts) in latest.items()),
                       key=lambda r: (r[0], r[1]))                       # lock in UNIQUE-key order (fewer deadlocks)
    hist_rows = sorted(((rid, s[0], round(s[3], 6), datetime.utcfromtimestamp(s[4])) for s in samples),
                       key=lambda r: (r[0], r[1], r[3]))
    cur.executemany(SQL_UPSERT_METRICS, last_rows)
    cur.executemany(SQL_UPSERT_HISTORY, hist_rows)


# ── after commit (best effort, never fails the request) ──────────────────

def _after_commit(agent, clean, result, request):
    for action, detail in result.get("events", []):
        try:
            from app.audit import write_audit
            write_audit(f"host-agent:{agent['hostname']}", action, detail, role="AGENT", request=request)
        except Exception as e:
            logger.warning("host agent audit write failed: %s", e)
    rid = result.get("resource_db_id")
    if rid is None:
        return
    if result.get("resource_type") == "ec2":
        _register_mounts(agent["aws_account_id"], clean)
    prefer.mark_feed(rid, result.get("fed", []))


def _register_mounts(account_id, clean):
    """Non-root mounts of an EC2-attached host get the same catalog/threshold registration the CWAgent path performs, so
    they alert through the unmodified evaluator join. Once per process per name; never for 'host' resources (that would
    create ENABLED thresholds for a type nobody asked to alert on)."""
    todo = [(s[0], s[2]) for s in clean["samples"]
            if s[1] == "disk_used_percent" and s[0] != metric_map.BASE_DISK_METRIC and (account_id, s[0]) not in _REGISTERED]
    for name, path in dict(todo).items():
        try:
            from app.collector.disk_mounts import ensure_disk_mount_metric_registered
            ensure_disk_mount_metric_registered(account_id, "ec2", name, path)
            _REGISTERED.add((account_id, name))
        except Exception as e:
            logger.warning("host agent mount registration failed [%s]: %s", name, e)
