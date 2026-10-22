# app/collector/host_agent_watchdog.py
"""
Host agent health watchdog -- PHASE D of the host-agent feature (migration 121).

Every critical-tier tick (2 min, leader only, see scheduler.py) this derives a state for each ACTIVE host agent
from host_agents.last_seen_at and raises ONE alert per host through the existing alert pipeline:

    online   age <= HOST_AGENT_STALE_SECONDS                 no alert; an open one auto-resolves ("recovered")
    stale    age <= 5 x HOST_AGENT_STALE_SECONDS             WARNING   metric_name 'host_agent_heartbeat'
    offline  age >  5 x HOST_AGENT_STALE_SECONDS             CRITICAL  (same alert row, escalated in place)

FLAP DAMPING. Nothing is raised until HOST_AGENT_STALE_SECONDS is exceeded (default 180 s = 3 missed 60 s beats), so one
missed beat never alerts. The same row is updated across cycles (dedupe on account + resource + metric, exactly like
alert_evaluator / synthetic.py), so a host that stays silent never produces a second alert.

AGENT DOWN vs HOST DOWN. The EC2 row the discovery job already keeps current (resources.instance_state, and the
'statuscheckfailed' value describe_polling.py already writes to `metrics`) is read; NO cloud API call is made here.
  * instance stopped / stopping / terminated / shutting-down -> the agent is recorded offline QUIETLY: no alert, and any
    open one is resolved with reason 'instance_stopped' (the same reason alert_evaluator uses).
  * instance running, agent silent -> alert text says the agent (or its network path) is down, not the host; if EC2's
    status checks are failing as well it says the host itself may be impaired.
  * no EC2 row for the host (non-EC2 server, instance_id not discovered) -> alert text says the two cannot be told apart.

WHICH RESOURCE THE ALERT HANGS ON. host_agents.resource_id, else the host's EC2 row (instance_id), else a `resources`
row of type 'host_agent' created here (same technique as synthetic.py's 'synthetic_check' rows). Because alerts.resource_id
is a real resources row, incident correlation (correlate.py), RCA (rca.py), health scores (health_score.py), priority,
environment, escalation, status page and notifications all treat the host's resource normally with no changes.

MAINTENANCE / SILENCES. A resource covered by an active maintenance window gets its alert born silenced
(maintenance.active_silenced_map, same as alert_evaluator); existing alerts are kept in sync by
maintenance.sync_maintenance_silencing(). Silenced or muted alerts never notify.

Inert unless HOST_AGENT_ENABLED=true: run_watchdog() returns before touching the database.
"""
import logging
import os

from app.db import get_connection

logger = logging.getLogger(__name__)

METRIC = "host_agent_heartbeat"
OFFLINE_FACTOR = 5                      # offline = silent for more than this many x HOST_AGENT_STALE_SECONDS
DEFAULT_STALE_SECONDS = 180
STOPPED_STATES = ("stopped", "stopping", "terminated", "shutting-down")
STATUS_CHECK_WINDOW_MINUTES = 15
OPEN_STATUSES = ("active", "acknowledged")
ENVIRONMENT_MAX_LEN = 50
DETAIL_MAX_LEN = 500

_TRUE = ("true", "1", "yes")


def enabled() -> bool:
    return os.getenv("HOST_AGENT_ENABLED", "false").strip().lower() in _TRUE


def stale_seconds() -> int:
    try:
        value = int(os.getenv("HOST_AGENT_STALE_SECONDS", str(DEFAULT_STALE_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_STALE_SECONDS
    return value if value > 0 else DEFAULT_STALE_SECONDS


def classify(age_seconds, stale_s=None):
    """age of the last heartbeat (seconds) -> 'online' | 'stale' | 'offline'. None (no timestamp at all) -> None."""
    if age_seconds is None:
        return None
    stale_s = stale_s or stale_seconds()
    if age_seconds <= stale_s:
        return "online"
    if age_seconds <= stale_s * OFFLINE_FACTOR:
        return "stale"
    return "offline"


def _fmt_age(seconds) -> str:
    seconds = int(max(0, seconds))
    if seconds < 120:
        return f"{seconds}s"
    minutes, rem = divmod(seconds, 60)
    if minutes < 120:
        return f"{minutes}m {rem:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes:02d}m"
    return f"{hours // 24}d {hours % 24}h"


def build_detail(hostname, state, age_seconds, stale_s, instance):
    """Human text for the alert (notification body + watchdog state row). `instance` is None when no EC2 row exists,
    else {"state": <instance_state>, "status_check_failed": True|False|None}."""
    level = "offline" if state == "offline" else "stale"
    text = (f"Host agent on {hostname} is {level}: no heartbeat for {_fmt_age(age_seconds)} "
            f"(stale after {stale_s}s).")
    if instance is None:
        text += " No EC2 state is known for this host, so an agent outage cannot be told apart from a host outage."
    elif instance.get("state") == "running":
        text += " The EC2 instance is running, so the agent or its network path is down, not the instance."
        if instance.get("status_check_failed"):
            text += " EC2 status checks are failing too, so the host itself may be impaired."
    else:
        text += f" The EC2 instance state is '{instance.get('state')}'."
    return text[:DETAIL_MAX_LEN]


# ── data access ──────────────────────────────────────────────────────────

def _env_cols():
    from app.environment_classifier import flag_enabled
    return ", r.env_resolved AS env_resolved" if flag_enabled() else ""


def _find_target(cursor, host):
    """The existing `resources` row this host's alert should hang on, or None."""
    env_col = _env_cols()
    acct = host["aws_account_id"]
    for rid in (host.get("resource_id"), host.get("instance_id")):
        if not rid:
            continue
        cursor.execute(f"""
            SELECT r.id AS db_id, r.resource_id, r.resource_type, r.name, r.tags{env_col}
            FROM resources r
            WHERE r.aws_account_id = %s AND r.resource_id = %s
            ORDER BY (r.resource_type = 'ec2') DESC
            LIMIT 1
        """, (acct, rid))
        row = cursor.fetchone()
        if row:
            return row
    cursor.execute(f"""
        SELECT r.id AS db_id, r.resource_id, r.resource_type, r.name, r.tags{env_col}
        FROM resources r
        WHERE r.aws_account_id = %s AND r.resource_type = 'host_agent' AND r.resource_id = %s
        LIMIT 1
    """, (acct, _own_resource_id(host)))
    return cursor.fetchone()


def _own_resource_id(host):
    return f"host-agent-{host['id']}"


def _ensure_target(cursor, host):
    row = _find_target(cursor, host)
    if row:
        return row
    rid = _own_resource_id(host)
    # last_seen_at deliberately omitted (NULL): alert_evaluator's "resource_gone" sweep only ages rows that HAVE one.
    cursor.execute("""
        INSERT INTO resources (aws_account_id, resource_type, resource_id, name, tags)
        VALUES (%s, 'host_agent', %s, %s, %s)
        ON DUPLICATE KEY UPDATE name = VALUES(name)
    """, (host["aws_account_id"], rid, host["hostname"], "{}"))
    return _find_target(cursor, host)


def _instance_info(cursor, host):
    """EC2 state for the host's instance from data the collector already holds. None = unknown."""
    iid = host.get("instance_id")
    if not iid:
        return None
    cursor.execute("""
        SELECT id AS db_id, instance_state FROM resources
        WHERE aws_account_id = %s AND resource_type = 'ec2' AND resource_id = %s
        LIMIT 1
    """, (host["aws_account_id"], iid))
    row = cursor.fetchone()
    if not row or not row.get("instance_state"):
        return None
    info = {"state": str(row["instance_state"]).lower(), "status_check_failed": None}
    if info["state"] == "running":
        cursor.execute(f"""
            SELECT metric_value FROM metrics
            WHERE resource_id = %s AND metric_name = 'statuscheckfailed'
              AND metric_timestamp >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL {STATUS_CHECK_WINDOW_MINUTES} MINUTE)
            LIMIT 1
        """, (row["db_id"],))
        m = cursor.fetchone()
        if m and m.get("metric_value") is not None:
            info["status_check_failed"] = float(m["metric_value"]) >= 1
    return info


def _environment(row):
    """Same rule as alert_evaluator: classified environment when ENV_AWARE_CLASSIFICATION is on, else the tag (default prod)."""
    import json
    from app.environment_classifier import flag_enabled, resolve_environment, IGNORE, UNKNOWN
    if flag_enabled():
        env = row.get("env_resolved")
        if not env:
            env = resolve_environment(tags=row.get("tags"), resource_id=row.get("resource_id")).env
        if env == IGNORE:
            env = UNKNOWN
        return str(env)[:ENVIRONMENT_MAX_LEN]
    try:
        tags = json.loads(row.get("tags") or "{}")
    except Exception:
        tags = {}
    if not isinstance(tags, dict):
        tags = {}
    env = tags.get("environment", tags.get("Environment", "prod"))
    env = str(env).strip().lower() if env is not None else "prod"
    return (env or "prod")[:ENVIRONMENT_MAX_LEN]


def _open_alert(cursor, account_id, resource_id):
    cursor.execute("""
        SELECT id, severity, status, silenced,
               (muted_until IS NOT NULL AND muted_until > UTC_TIMESTAMP()) AS muted
        FROM alerts
        WHERE aws_account_id = %s AND resource_id = %s AND metric_name = %s
          AND status IN ('active', 'acknowledged')
        ORDER BY id
        LIMIT 1
    """, (account_id, resource_id, METRIC))
    return cursor.fetchone()


def _resolve(cursor, alert_id, reason):
    cursor.execute("""
        UPDATE alerts
        SET status = 'resolved', resolved_at = UTC_TIMESTAMP(), last_seen_at = UTC_TIMESTAMP(),
            resolution_reason = %s, resolved_by = 'system'
        WHERE id = %s AND status IN ('active', 'acknowledged')
    """, (reason, alert_id))
    return cursor.rowcount


def _account_name(cursor, account_id):
    try:
        cursor.execute("SELECT account_name FROM aws_accounts WHERE id = %s", (account_id,))
        return (cursor.fetchone() or {}).get("account_name") or ""
    except Exception:
        return ""


def _save_state(cursor, host, state, offline_reason, alert_id, resource_id, detail):
    # state_since must be assigned BEFORE state: MySQL and MariaDB evaluate ON DUPLICATE KEY assignments left to right.
    cursor.execute("""
        INSERT INTO host_agent_watchdog_state
            (host_agent_id, aws_account_id, state, state_since, offline_reason, alert_id, alert_resource_id,
             detail, evaluated_at)
        VALUES (%s, %s, %s, UTC_TIMESTAMP(), %s, %s, %s, %s, UTC_TIMESTAMP())
        ON DUPLICATE KEY UPDATE
            state_since = IF(state <> VALUES(state), UTC_TIMESTAMP(), state_since),
            state = VALUES(state),
            aws_account_id = VALUES(aws_account_id),
            offline_reason = VALUES(offline_reason),
            alert_id = VALUES(alert_id),
            alert_resource_id = VALUES(alert_resource_id),
            detail = VALUES(detail),
            evaluated_at = UTC_TIMESTAMP()
    """, (host["id"], host["aws_account_id"], state, offline_reason, alert_id, resource_id, detail))


# ── one host ─────────────────────────────────────────────────────────────

def _evaluate_host(cursor, host, stale_s, silenced_map, stats, after_commit):
    """Evaluate the host that represents its alert resource; returns nothing, writes via `cursor`."""
    age = host["age_seconds"]
    state = classify(age, stale_s)
    target = _find_target(cursor, host)
    existing = _open_alert(cursor, host["aws_account_id"], target["resource_id"]) if target else None

    if state == "online":
        if existing:
            if _resolve(cursor, existing["id"], "recovered"):
                stats["resolved"] += 1
                after_commit.append(("resolved", existing["id"], host["aws_account_id"]))
        _save_state(cursor, host, "online", None, None, target["resource_id"] if target else None, None)
        return

    instance = _instance_info(cursor, host)

    if instance is not None and instance["state"] in STOPPED_STATES:
        # HOST down, not an agent fault: record the agent offline, say nothing, clear anything already open.
        if existing and _resolve(cursor, existing["id"], "instance_stopped"):
            stats["resolved"] += 1
            after_commit.append(("resolved", existing["id"], host["aws_account_id"]))
        stats["quiet_offline"] += 1
        _save_state(cursor, host, "offline", "instance_stopped", None,
                    target["resource_id"] if target else None,
                    f"Instance is {instance['state']}; agent offline is expected."[:DETAIL_MAX_LEN])
        return

    severity = "CRITICAL" if state == "offline" else "WARNING"
    threshold = stale_s * OFFLINE_FACTOR if state == "offline" else stale_s
    detail = build_detail(host["hostname"], state, age, stale_s, instance)
    reason = "agent_silent"

    if existing:
        sets = ["current_value = %s", "threshold = %s", "last_seen_at = UTC_TIMESTAMP()"]
        params = [age, threshold]
        escalated = False
        if existing["severity"] != severity:
            sets.append("severity = %s")
            params.append(severity)
            if severity == "CRITICAL":
                escalated = True
                if existing["status"] == "acknowledged":
                    # it got worse after a human acknowledged the milder state (same rule as alert_evaluator)
                    sets += ["status = 'active'", "acked = 0", "acked_by = NULL", "acked_at = NULL"]
        params.append(existing["id"])
        cursor.execute(f"UPDATE alerts SET {', '.join(sets)} WHERE id = %s", params)
        stats["updated"] += 1
        if escalated and not existing["silenced"] and not existing["muted"]:
            after_commit.append(("escalated", existing["id"], host, target, severity, age, threshold, detail))
        _save_state(cursor, host, state, reason, existing["id"], target["resource_id"], detail)
        return

    target = target or _ensure_target(cursor, host)
    environment = _environment(target)
    silence_reason = silenced_map.get((host["aws_account_id"], target["resource_id"]))
    group_key = f"{host['aws_account_id']}:{target['resource_type']}:{METRIC}"
    cursor.execute("""
        INSERT INTO alerts
            (aws_account_id, resource_id, metric_name, severity, environment, group_key, status,
             triggered_at, last_seen_at, cycle_at, healthy_streak, current_value, threshold,
             breach_value, breach_threshold, silenced, silenced_reason)
        VALUES (%s, %s, %s, %s, %s, %s, 'active', UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP(), 0,
                %s, %s, %s, %s, %s, %s)
    """, (host["aws_account_id"], target["resource_id"], METRIC, severity, environment, group_key,
          age, threshold, age, threshold, 1 if silence_reason else 0, silence_reason))
    alert_id = cursor.lastrowid
    stats["opened"] += 1
    if silence_reason:
        stats["born_silenced"] += 1
    else:
        after_commit.append(("opened", alert_id, host, target, severity, age, threshold, detail))
    _save_state(cursor, host, state, reason, alert_id, target["resource_id"], detail)


def _notify(kind, alert_id, host, target, severity, age, threshold, detail, account_name):
    """Runs AFTER commit. Same fan-out as alert_evaluator; each step is independent and never raises."""
    priority = None
    try:
        from app.priority import enabled as priority_enabled, compute_priority
        if priority_enabled():
            priority = compute_priority(_environment(target), severity, target.get("tags"))
    except Exception:
        logger.warning("[host-agent-watchdog] priority for popup failed", exc_info=True)
    try:
        from app.ws.publisher import publish_alert
        publish_alert(alert_id=alert_id, severity=severity, metric=METRIC, value=age, threshold=threshold,
                      account_id=host["aws_account_id"], account_name=account_name, region=None, priority=priority)
    except Exception:
        logger.warning("[host-agent-watchdog] publish_alert failed", exc_info=True)
    try:
        from app.notifications.sender import notify_alert_event
        notify_alert_event(kind, {
            "id": alert_id, "severity": severity, "metric_name": METRIC, "resource_id": target["resource_id"],
            "account_name": account_name, "aws_account_id": host["aws_account_id"], "value": age,
            "threshold": threshold, "silenced": False, "priority": priority, "detail": detail,
        })
    except Exception:
        logger.warning("[host-agent-watchdog] notify_alert_event failed", exc_info=True)


def _clean_departed(cursor, stats, after_commit):
    """Hosts that were revoked or deleted since the last cycle: close their open alert, drop their state row."""
    cursor.execute("""
        SELECT s.host_agent_id, s.aws_account_id, s.alert_id
        FROM host_agent_watchdog_state s
        LEFT JOIN host_agents h ON h.id = s.host_agent_id
        WHERE h.id IS NULL OR h.status <> 'active'
    """)
    for row in cursor.fetchall():
        if row.get("alert_id") and _resolve(cursor, row["alert_id"], "agent_revoked"):
            stats["resolved"] += 1
            after_commit.append(("resolved", row["alert_id"], row["aws_account_id"]))
        cursor.execute("DELETE FROM host_agent_watchdog_state WHERE host_agent_id = %s", (row["host_agent_id"],))
        stats["departed"] += 1


# ── entry point ──────────────────────────────────────────────────────────

def run_watchdog() -> dict:
    """One watchdog pass. Returns counters. No-op (no DB access) unless HOST_AGENT_ENABLED=true."""
    if not enabled():
        return {"skipped": "disabled"}
    stale_s = stale_seconds()
    stats = {"hosts": 0, "opened": 0, "updated": 0, "resolved": 0, "quiet_offline": 0,
             "born_silenced": 0, "departed": 0}
    after_commit = []
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        _clean_departed(cursor, stats, after_commit)

        cursor.execute("""
            SELECT h.id, h.aws_account_id, h.hostname, h.instance_id, h.resource_id,
                   TIMESTAMPDIFF(SECOND, COALESCE(h.last_seen_at, h.enrolled_at), UTC_TIMESTAMP()) AS age_seconds
            FROM host_agents h
            JOIN aws_accounts acc ON acc.id = h.aws_account_id AND acc.status = 'active'
            WHERE h.status = 'active'
            ORDER BY h.id
        """)
        hosts = [h for h in cursor.fetchall() if h.get("age_seconds") is not None]
        stats["hosts"] = len(hosts)

        silenced_map = {}
        try:
            from app.collector.maintenance import active_silenced_map
            silenced_map = active_silenced_map(cursor)
        except Exception:
            logger.warning("[host-agent-watchdog] could not read maintenance windows", exc_info=True)

        # Hosts that share one alert resource (an agent re-enrolled under a new hostname leaves its old row active):
        # the freshest heartbeat decides, so the stale twin cannot hold a false alert open.
        groups = {}
        for host in hosts:
            target = _find_target(cursor, host)
            key = (host["aws_account_id"], target["resource_id"] if target else _own_resource_id(host))
            groups.setdefault(key, []).append(host)

        for members in groups.values():
            members.sort(key=lambda h: (h["age_seconds"], h["id"]))
            lead = members[0]
            _evaluate_host(cursor, lead, stale_s, silenced_map, stats, after_commit)
            for twin in members[1:]:
                _save_state(cursor, twin, classify(twin["age_seconds"], stale_s), "superseded", None, None,
                            f"Superseded by host agent id {lead['id']} on the same resource.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    for item in after_commit:
        try:
            if item[0] == "resolved":
                from app.ws.publisher import publish_alert_resolved
                publish_alert_resolved(alert_id=item[1], account_id=item[2])
            else:
                kind, alert_id, host, target, severity, age, threshold, detail = item
                name = ""
                c2 = get_connection()
                try:
                    cur2 = c2.cursor(dictionary=True)
                    name = _account_name(cur2, host["aws_account_id"])
                finally:
                    c2.close()
                _notify(kind, alert_id, host, target, severity, age, threshold, detail, name)
        except Exception:
            logger.warning("[host-agent-watchdog] post-commit step failed", exc_info=True)

    if stats["opened"] or stats["resolved"] or stats["departed"]:
        logger.info(f"[host-agent-watchdog] {stats}")
    return stats
