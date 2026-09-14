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
import logging
import socket
import time
from datetime import datetime, timedelta

import requests

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


def _probe_http(target: str, timeout: int, expected_status: int, expected_keyword: str):
    start = time.monotonic()
    try:
        resp = requests.get(
            target, timeout=timeout, allow_redirects=True,
            headers={"User-Agent": _HTTP_USER_AGENT},
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        status_ok = resp.status_code == (expected_status or 200)
        keyword_ok = True
        if expected_keyword:
            keyword_ok = expected_keyword in resp.text
        success = status_ok and keyword_ok
        error = None
        if not success:
            reasons = []
            if not status_ok:
                reasons.append(f"expected status {expected_status or 200}, got {resp.status_code}")
            if not keyword_ok:
                reasons.append(f"expected keyword '{expected_keyword}' not found in response")
            error = "; ".join(reasons)
        return success, elapsed_ms, resp.status_code, error
    except requests.exceptions.Timeout:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"timed out after {timeout}s"
    except requests.exceptions.RequestException as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, str(e)[:490]


def _probe_tcp(target: str, timeout: int):
    start = time.monotonic()
    try:
        host, _, port_str = target.rpartition(":")
        port = int(port_str)
        with socket.create_connection((host, port), timeout=timeout):
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return True, elapsed_ms, None, None
    except (socket.timeout, TimeoutError):
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"connection timed out after {timeout}s"
    except (socket.gaierror, ConnectionRefusedError, OSError, ValueError) as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, str(e)[:490]


def _probe_dns(target: str, timeout: int):
    start = time.monotonic()
    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        socket.gethostbyname(target)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return True, elapsed_ms, None, None
    except socket.gaierror as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"DNS resolution failed: {e}"
    finally:
        socket.setdefaulttimeout(old_timeout)


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
        f'{{"environment": "{check["environment"]}"}}',
    ))
    return resource_id


def _write_or_update_alert(cursor, resource_id: str, check: dict, error_message: str):
    group_key = f"{check['aws_account_id']}:synthetic_check:synthetic_uptime"
    cursor.execute("""
        SELECT id FROM alerts
        WHERE resource_id = %s AND metric_name = 'synthetic_uptime' AND status = 'active'
    """, (resource_id,))
    existing = cursor.fetchone()
    if existing:
        cursor.execute("""
            UPDATE alerts
            SET current_value = %s, last_seen_at = NOW()
            WHERE id = %s
        """, (check["consecutive_failures"], existing["id"]))
        return

    cursor.execute("""
        INSERT INTO alerts
            (resource_id, metric_name, severity, environment, group_key, status,
             triggered_at, last_seen_at, healthy_streak, current_value, threshold)
        VALUES (%s, 'synthetic_uptime', 'CRITICAL', %s, %s, 'active',
                NOW(), NOW(), 0, %s, %s)
    """, (
        resource_id, check["environment"], group_key,
        check["consecutive_failures"], check["consecutive_failure_threshold"],
    ))
    logger.warning(
        f"[synthetic] check '{check['name']}' (id={check['id']}) is DOWN after "
        f"{check['consecutive_failures']} consecutive failures: {error_message}"
    )


def _resolve_alert(cursor, resource_id: str):
    cursor.execute("""
        UPDATE alerts SET status = 'resolved', resolved_at = NOW(), last_seen_at = NOW()
        WHERE resource_id = %s AND metric_name = 'synthetic_uptime' AND status = 'active'
    """, (resource_id,))
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
                    _resolve_alert(cursor, resource_id)

                probed += 1

            except Exception:
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
