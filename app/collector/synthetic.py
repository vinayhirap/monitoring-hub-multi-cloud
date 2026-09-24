# app/collector/synthetic.py
"""
Synthetic/uptime (blackbox) monitoring -- active HTTP/TCP/DNS probes
run FROM this app AGAINST a configured target, on a schedule. See
db/migrations/031_synthetic_monitoring.sql's module docstring for why
this exists (this app is otherwise 100% passive) and the integration
design (a failing check becomes a normal `alerts` row against an
auto-created `resources` row, so correlate.py/health_score.py/
escalation.py/rca.py/the LLM summarizer all pick it up with zero
changes to any of them).

Runs in scheduler.py's "critical" tier (2-min cadence, see run_loop's
docstring) but only ever probes checks that are actually DUE
(next_check_at <= NOW()) -- a check configured for a 5-minute interval
genuinely only runs every ~5 minutes, not every 2-minute scheduler
tick; the tick is just how often this module LOOKS for due work.
"""
import ipaddress
import json
import logging
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from app.db import get_connection

logger = logging.getLogger(__name__)

# How many checks to probe in a single scheduler tick -- bounds
# worst-case tick duration if many checks come due at once (e.g. right
# after a bulk import). Remaining due checks are simply picked up on
# the next 2-min tick, a few minutes later than ideal but never
# blocking the rest of this tier's other work.
MAX_CHECKS_PER_CYCLE = 100

# How long synthetic_check_results history is kept -- same 30-day
# horizon as metric_history (see baseline.py's LOOKBACK_DAYS), enough
# for a meaningful uptime-% trend without unbounded table growth.
RESULT_RETENTION_DAYS = 30

_HTTP_USER_AGENT = "monitoring-hub-synthetic-check/1.0"

# Hard bounds on user-configurable probe parameters (audit b20). A
# probe runs inline in the leader's critical-tier thread, so an
# unbounded timeout lets one check stall the whole tier.
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 60
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 1_000_000  # keyword search reads at most this much

# ── SSRF guard (audit b20) ───────────────────────────────────────────
# Probes are created by any synthetic.manage holder and run from inside
# the app's VPC with the instance role attached, so without this a
# check could target 169.254.169.254 (IMDS credentials), 127.0.0.1
# (MySQL/Redis/VictoriaMetrics/uvicorn) or any VPC-internal host, and
# read the result back through status_code / expected_keyword /
# error_message. Enforced at CONNECT time on every hop (redirects
# included) against the IP actually dialled, so DNS rebinding between
# validation and connect cannot bypass it.
#
# Always blocked: loopback, link-local (cloud metadata), multicast,
# unspecified, reserved. Other non-global ranges (RFC1918, CGNAT, ULA)
# are blocked unless listed in SYNTHETIC_ALLOWED_PRIVATE_CIDRS
# (comma-separated), for deployments that deliberately probe internal
# endpoints.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def _load_allowed_private_nets():
    nets = []
    for raw in (os.getenv("SYNTHETIC_ALLOWED_PRIVATE_CIDRS") or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            logger.warning(f"[synthetic] ignoring invalid SYNTHETIC_ALLOWED_PRIVATE_CIDRS entry {raw!r}")
    return nets


_ALLOWED_PRIVATE_NETS = _load_allowed_private_nets()


class UnsafeTargetError(Exception):
    """Target resolves to an address synthetic probes may not reach."""


def _is_blocked_ip(ip) -> bool:
    if isinstance(ip, str):
        ip = ipaddress.ip_address(ip.split("%", 1)[0])
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64_PREFIX:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return True
    if ip.version == 4 and ip in ipaddress.ip_network("0.0.0.0/8"):
        return True
    if not ip.is_global:
        return not any(ip.version == n.version and ip in n for n in _ALLOWED_PRIVATE_NETS)
    return False


def _resolve_safe_ip(host: str, port: int) -> str:
    """Resolves host and returns one address to dial. Raises
    UnsafeTargetError if ANY resolved address is blocked (so a
    round-robin record mixing public and internal IPs is rejected
    outright), socket.gaierror if it doesn't resolve."""
    host = (host or "").strip().strip("[]").rstrip(".")
    if not host:
        raise UnsafeTargetError("empty host")
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addrs = [info[4][0] for info in infos]
    if not addrs:
        raise socket.gaierror(f"no addresses for {host}")
    for a in addrs:
        if _is_blocked_ip(a):
            raise UnsafeTargetError(
                f"target {host} resolves to a blocked (internal/metadata) address"
            )
    return addrs[0]


class _GuardedHTTPConnection(HTTPConnection):
    def _new_conn(self):
        ip = _resolve_safe_ip(self._dns_host, self.port)
        original = self._dns_host
        self._dns_host = ip
        try:
            return super()._new_conn()
        finally:
            self._dns_host = original


class _GuardedHTTPSConnection(HTTPSConnection):
    # TLS SNI + certificate verification still use self.host (the
    # hostname), only the TCP dial goes to the pre-validated IP.
    def _new_conn(self):
        ip = _resolve_safe_ip(self._dns_host, self.port)
        original = self._dns_host
        self._dns_host = ip
        try:
            return super()._new_conn()
        finally:
            self._dns_host = original


class _GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


class _GuardedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _GuardedHTTPPool, "https": _GuardedHTTPSPool,
        }


def _guarded_session() -> requests.Session:
    session = requests.Session()
    # Never route probes through an env-configured proxy: the guard
    # would validate the proxy's address instead of the real target.
    session.trust_env = False
    session.max_redirects = MAX_REDIRECTS
    adapter = _GuardedAdapter(max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _split_tcp_target(target: str):
    """'host:port' or '[v6]:port' -> (host, port). Raises ValueError."""
    target = (target or "").strip()
    if target.startswith("["):
        host, sep, rest = target[1:].partition("]")
        if not sep or not rest.startswith(":"):
            raise ValueError("tcp target must be host:port")
        port_str = rest[1:]
    else:
        host, _, port_str = target.rpartition(":")
    port = int(port_str)
    if not host or not (1 <= port <= 65535):
        raise ValueError("tcp target must be host:port with port 1-65535")
    return host, port


def validate_target(check_type: str, target: str) -> None:
    """API-side validation (create/update). Raises ValueError with a
    user-safe message. A hostname that doesn't resolve yet is accepted
    (the connect-time guard still applies on every probe); one that
    resolves to a blocked address is rejected."""
    target = (target or "").strip()
    if not target:
        raise ValueError("target is required")
    if len(target) > 500:
        raise ValueError("target must be at most 500 characters")
    if check_type == "http":
        parts = urlsplit(target)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("http checks need a full URL (http:// or https://)")
        try:
            host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            raise ValueError("http target has an invalid port")
    elif check_type == "tcp":
        host, port = _split_tcp_target(target)
    elif check_type == "dns":
        if any(c in target for c in "/: "):
            raise ValueError("dns checks take a bare hostname")
        return
    else:
        raise ValueError("unknown check_type")
    try:
        _resolve_safe_ip(host, port)
    except UnsafeTargetError as e:
        raise ValueError(str(e))
    except (socket.gaierror, UnicodeError, OSError):
        pass


def _clamp_timeout(timeout) -> int:
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = 10
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, timeout))


# Bounded pool for DNS probes -- replaces socket.setdefaulttimeout(),
# which mutated a process-wide default under 2 workers + collector
# threads and didn't bound getaddrinfo anyway.
_DNS_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="synthetic-dns")


def _probe_http(target: str, timeout: int, expected_status: int, expected_keyword: str):
    timeout = _clamp_timeout(timeout)
    start = time.monotonic()
    session = _guarded_session()
    try:
        resp = session.get(
            target, timeout=timeout, allow_redirects=True, stream=True,
            headers={"User-Agent": _HTTP_USER_AGENT},
        )
        try:
            status_ok = resp.status_code == (expected_status or 200)
            keyword_ok = True
            if expected_keyword:
                body = bytearray()
                for chunk in resp.iter_content(chunk_size=16384):
                    body.extend(chunk)
                    if len(body) >= MAX_BODY_BYTES or time.monotonic() - start > timeout:
                        break
                text = bytes(body[:MAX_BODY_BYTES]).decode(resp.encoding or "utf-8", errors="replace")
                keyword_ok = expected_keyword in text
        finally:
            resp.close()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        success = status_ok and keyword_ok
        error = None
        if not success:
            reasons = []
            if not status_ok:
                reasons.append(f"expected status {expected_status or 200}, got {resp.status_code}")
            if not keyword_ok:
                reasons.append(f"expected keyword '{expected_keyword}' not found in response")
            error = "; ".join(reasons)[:490]
        return success, elapsed_ms, resp.status_code, error
    except UnsafeTargetError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"blocked: {e}"[:490]
    except requests.exceptions.Timeout:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"timed out after {timeout}s"
    except requests.exceptions.RequestException as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, str(e)[:490]
    finally:
        session.close()


def _probe_tcp(target: str, timeout: int):
    timeout = _clamp_timeout(timeout)
    start = time.monotonic()
    try:
        host, port = _split_tcp_target(target)
        ip = _resolve_safe_ip(host, port)
        with socket.create_connection((ip, port), timeout=timeout):
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return True, elapsed_ms, None, None
    except UnsafeTargetError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"blocked: {e}"[:490]
    except (socket.timeout, TimeoutError):
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"connection timed out after {timeout}s"
    except (socket.gaierror, ConnectionRefusedError, OSError, ValueError) as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, str(e)[:490]


def _probe_dns(target: str, timeout: int):
    timeout = _clamp_timeout(timeout)
    start = time.monotonic()
    future = _DNS_EXECUTOR.submit(socket.gethostbyname, target)
    try:
        future.result(timeout=timeout)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return True, elapsed_ms, None, None
    except FutureTimeout:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"DNS resolution timed out after {timeout}s"
    except (socket.gaierror, UnicodeError, OSError) as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"DNS resolution failed: {e}"[:490]


def _run_probe(check: dict):
    if check["check_type"] == "http":
        return _probe_http(check["target"], check["timeout_seconds"],
                            check["expected_status_code"], check["expected_keyword"])
    elif check["check_type"] == "tcp":
        return _probe_tcp(check["target"], check["timeout_seconds"])
    elif check["check_type"] == "dns":
        return _probe_dns(check["target"], check["timeout_seconds"])
    return False, None, None, f"unknown check_type '{check['check_type']}'"


def _synthetic_resource_id(check_id: int) -> str:
    return f"synthetic-{check_id}"


def _ensure_resource_row(cursor, check: dict) -> str:
    """Auto-creates/upserts the `resources` row a synthetic check's
    alerts key off of -- see migration 031's docstring for why this is
    the integration point that gets correlate.py/health_score.py/
    escalation.py/rca.py for free. Returns the resource_id string used
    as `resources.resource_id` / `alerts.resource_id`."""
    resource_id = _synthetic_resource_id(check["id"])
    cursor.execute("""
        INSERT INTO resources (aws_account_id, resource_type, resource_id, name, tags)
        VALUES (%s, 'synthetic_check', %s, %s, %s)
        ON DUPLICATE KEY UPDATE name = VALUES(name), tags = VALUES(tags)
    """, (
        check["aws_account_id"], resource_id, check["name"],
        json.dumps({"environment": check["environment"]}),
    ))
    return resource_id


def _write_or_update_alert(cursor, resource_id: str, check: dict, error_message: str):
    # aws_account_id is REQUIRED on every alert row (migration 048): all
    # readers join on it, so the old INSERT (which omitted it) made a synthetic
    # "site is DOWN" alert invisible on every screen.
    account_id = check["aws_account_id"]
    group_key = f"{account_id}:synthetic_check:synthetic_uptime"
    cursor.execute("""
        SELECT id FROM alerts
        WHERE aws_account_id = %s AND resource_id = %s AND metric_name = 'synthetic_uptime'
          AND status IN ('active', 'acknowledged')
        LIMIT 1
    """, (account_id, resource_id))
    existing = cursor.fetchone()
    if existing:
        cursor.execute("""
            UPDATE alerts
            SET current_value = %s, last_seen_at = UTC_TIMESTAMP()
            WHERE id = %s
        """, (check["consecutive_failures"], existing["id"]))
        return

    cursor.execute("""
        INSERT INTO alerts
            (aws_account_id, resource_id, metric_name, severity, environment, group_key, status,
             triggered_at, last_seen_at, healthy_streak, current_value, threshold)
        VALUES (%s, %s, 'synthetic_uptime', 'CRITICAL', %s, %s, 'active',
                UTC_TIMESTAMP(), UTC_TIMESTAMP(), 0, %s, %s)
    """, (
        account_id, resource_id, check["environment"], group_key,
        check["consecutive_failures"], check["consecutive_failure_threshold"],
    ))
    logger.warning(
        f"[synthetic] check '{check['name']}' (id={check['id']}) is DOWN after "
        f"{check['consecutive_failures']} consecutive failures: {error_message}"
    )


def _resolve_alert(cursor, resource_id: str, account_id=None):
    scope = "AND aws_account_id = %s" if account_id is not None else ""
    params = (resource_id,) + ((account_id,) if account_id is not None else ())
    cursor.execute(f"""
        UPDATE alerts SET status = 'resolved', resolved_at = UTC_TIMESTAMP(), last_seen_at = UTC_TIMESTAMP(),
                          resolution_reason = 'recovered', resolved_by = 'system'
        WHERE resource_id = %s {scope} AND metric_name = 'synthetic_uptime'
          AND status IN ('active', 'acknowledged')
    """, params)
    if cursor.rowcount:
        logger.info(f"[synthetic] {resource_id} recovered -- resolved its active alert")


def run_due_checks() -> int:
    """Probes every enabled check whose next_check_at has passed (or
    was never set -- brand new checks run on the very next tick).
    Returns the number of checks probed this cycle. Non-fatal per
    check -- one check's probe raising an unexpected exception never
    blocks the rest of the batch."""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    probed = 0
    try:
        cursor.execute("""
            SELECT id, aws_account_id, name, check_type, target,
                   expected_status_code, expected_keyword, timeout_seconds,
                   interval_seconds, consecutive_failure_threshold, environment,
                   consecutive_failures, current_status
            FROM synthetic_checks
            WHERE enabled = 1 AND (next_check_at IS NULL OR next_check_at <= NOW())
            ORDER BY next_check_at IS NULL DESC, next_check_at ASC
            LIMIT %s
        """, (MAX_CHECKS_PER_CYCLE,))
        due = cursor.fetchall()

        for check in due:
            try:
                success, elapsed_ms, status_code, error = _run_probe(check)

                cursor.execute("""
                    INSERT INTO synthetic_check_results
                        (check_id, checked_at, success, response_time_ms, status_code, error_message)
                    VALUES (%s, NOW(), %s, %s, %s, %s)
                """, (check["id"], success, elapsed_ms, status_code, error))

                if success:
                    new_consecutive_failures = 0
                    new_status = "up"
                else:
                    new_consecutive_failures = check["consecutive_failures"] + 1
                    new_status = "down" if new_consecutive_failures >= check["consecutive_failure_threshold"] else check["current_status"]

                cursor.execute("""
                    UPDATE synthetic_checks
                    SET last_checked_at = NOW(),
                        next_check_at = DATE_ADD(NOW(), INTERVAL interval_seconds SECOND),
                        consecutive_failures = %s,
                        current_status = %s
                    WHERE id = %s
                """, (new_consecutive_failures, new_status, check["id"]))

                check["consecutive_failures"] = new_consecutive_failures

                resource_id = _ensure_resource_row(cursor, check)
                if new_status == "down" and check["current_status"] != "down":
                    # Just crossed the threshold this cycle -- fire the alert.
                    _write_or_update_alert(cursor, resource_id, check, error)
                elif new_status == "down":
                    # Still down -- keep the existing active alert's
                    # current_value/last_seen_at fresh.
                    _write_or_update_alert(cursor, resource_id, check, error)
                elif new_status == "up" and check["current_status"] == "down":
                    _resolve_alert(cursor, resource_id, check["aws_account_id"])

                # Commit per check: keeps row locks on alerts/
                # synthetic_checks short (probes are slow network I/O)
                # and stops one failing check's partial writes from
                # being committed alongside everyone else's.
                conn.commit()
                probed += 1

            except Exception:
                conn.rollback()
                logger.exception(
                    f"[synthetic] probe failed unexpectedly for check id={check['id']} "
                    f"('{check.get('name')}') -- skipping this cycle, will retry next tick"
                )
                continue

        conn.commit()
        if probed:
            logger.info(f"[synthetic] probed {probed} due check(s)")
        return probed
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def prune_synthetic_results(retain_days: int = RESULT_RETENTION_DAYS) -> int:
    """Deletes synthetic_check_results older than retain_days. Called
    from scheduler.py's low tier, same pattern as
    metrics_writer.prune_metric_history()."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM synthetic_check_results WHERE checked_at < DATE_SUB(NOW(), INTERVAL %s DAY)",
            (retain_days,),
        )
        deleted = cursor.rowcount
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
