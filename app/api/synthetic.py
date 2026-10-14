# app/api/synthetic.py
"""
CRUD + results for synthetic_checks (db/migrations/031_synthetic_monitoring.sql).
Read gated on synthetic.view, write gated on synthetic.manage -- same
read/write permission split as topology.py (see
024_topology_manage_permission.sql's reasoning). Every endpoint is
scoped to the caller's accessible accounts via
get_accessible_account_ids(), same pattern as app/api/alerts.py.
"""
import logging
import re
from fastapi import APIRouter, Body, HTTPException, Depends, Query
from app.db import get_connection
from app.auth.permissions import require_permission
from app.auth.authorization import get_accessible_account_ids
from app.collector.synthetic import (
    validate_target, validate_https_redirect_option,
    MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, CERT_ALERT_METRIC,
    CERT_WARN_DAYS, CERT_CRIT_DAYS,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/synthetic-checks", tags=["Synthetic Monitoring"])

_VALID_CHECK_TYPES = ("http", "https", "tcp", "dns")
_MAX_INTERVAL_SECONDS = 86400


def _int_field(payload: dict, key: str, default, lo: int, hi: int):
    """Parses an optional int field with bounds -> HTTP 400 on bad input
    (previously int() raised straight through as a 500)."""
    raw = payload.get(key, default)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{key} must be an integer")
    if not (lo <= value <= hi):
        raise HTTPException(status_code=400, detail=f"{key} must be between {lo} and {hi}")
    return value


def _validate_common(payload: dict) -> dict:
    """Validates/normalises every bounded field present in payload."""
    out = {}
    if "timeout_seconds" in payload:
        out["timeout_seconds"] = _int_field(payload, "timeout_seconds", 10, MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)
    if "interval_seconds" in payload:
        # Below 60 the 2-min critical-tier scheduler cadence can't
        # honor the interval anyway -- reject rather than silently
        # running slower than configured.
        out["interval_seconds"] = _int_field(payload, "interval_seconds", 300, 60, _MAX_INTERVAL_SECONDS)
    if "consecutive_failure_threshold" in payload:
        out["consecutive_failure_threshold"] = _int_field(payload, "consecutive_failure_threshold", 2, 1, 100)
    if "expected_status_code" in payload:
        out["expected_status_code"] = _int_field(payload, "expected_status_code", None, 100, 599)
    if "expected_keyword" in payload:
        kw = payload.get("expected_keyword")
        if kw is not None and (not isinstance(kw, str) or len(kw) > 255):
            raise HTTPException(status_code=400, detail="expected_keyword must be a string of at most 255 characters")
        out["expected_keyword"] = kw or None
    if "environment" in payload:
        env = payload.get("environment")
        if not isinstance(env, str) or not env.strip() or len(env) > 50:
            raise HTTPException(status_code=400, detail="environment must be a non-empty string of at most 50 characters")
        out["environment"] = env.strip()
    if "name" in payload:
        name = (payload.get("name") or "").strip() if isinstance(payload.get("name"), str) else ""
        if not name or len(name) > 255:
            raise HTTPException(status_code=400, detail="name is required (max 255 characters)")
        out["name"] = name
    if "enabled" in payload:
        out["enabled"] = bool(payload.get("enabled"))
    if "expect_https_redirect" in payload:
        flag = payload.get("expect_https_redirect")
        if flag not in (True, False, 0, 1, None):
            raise HTTPException(status_code=400, detail="expect_https_redirect must be true or false")
        out["expect_https_redirect"] = 1 if flag else 0
    return out


def effective_cert_thresholds(check: dict):
    """(warn, crit) a check's certificate alert really uses: its own sane pair, else the global defaults."""
    try:
        warn, crit = int(check.get("cert_warn_days")), int(check.get("cert_crit_days"))
    except (TypeError, ValueError):
        return CERT_WARN_DAYS, CERT_CRIT_DAYS
    return (warn, crit) if 1 <= crit < warn else (CERT_WARN_DAYS, CERT_CRIT_DAYS)


def _cert_pair(payload: dict, check_type: str, current: dict = None):
    """Per-check certificate thresholds. -> {} when the payload does not mention them, else
    {"cert_warn_days": n|None, "cert_crit_days": n|None}. Both or neither; crit < warn; https checks only.
    On PATCH a single value is merged with the stored pair so crit < warn is judged on the result."""
    if "cert_warn_days" not in payload and "cert_crit_days" not in payload:
        return {}
    cur = current or {}
    warn = payload["cert_warn_days"] if "cert_warn_days" in payload else cur.get("cert_warn_days")
    crit = payload["cert_crit_days"] if "cert_crit_days" in payload else cur.get("cert_crit_days")
    if warn in (None, "") and crit in (None, ""):
        return {"cert_warn_days": None, "cert_crit_days": None}          # back to the global defaults
    if check_type != "https":
        raise HTTPException(status_code=400, detail="certificate thresholds only apply to https checks")
    if warn in (None, "") or crit in (None, ""):
        raise HTTPException(status_code=400, detail="set both the warning and the critical days, or leave both empty for the defaults")
    try:
        warn, crit = int(warn), int(crit)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="certificate thresholds must be whole numbers of days")
    if not (2 <= warn <= 365) or not (1 <= crit <= 364):
        raise HTTPException(status_code=400, detail="warning days must be 2 to 365 and critical days 1 to 364")
    if crit >= warn:
        raise HTTPException(status_code=400, detail="critical days must be smaller than warning days")
    return {"cert_warn_days": warn, "cert_crit_days": crit}


_CONTACT_EMAIL_RE = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")
MAX_CONTACTS = 20


def _clean_contacts(raw) -> list:
    """Validated, de-duplicated contact list or HTTP 400. Each: {email, label, notify_outage, notify_cert}."""
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="contacts must be a list")
    if len(raw) > MAX_CONTACTS:
        raise HTTPException(status_code=400, detail=f"at most {MAX_CONTACTS} contacts per check")
    out, seen = [], set()
    for i, c in enumerate(raw, 1):
        if not isinstance(c, dict):
            raise HTTPException(status_code=400, detail=f"contact {i} must be an object")
        email = (c.get("email") or "").strip() if isinstance(c.get("email"), str) else ""
        if not email or len(email) > 254 or not _CONTACT_EMAIL_RE.match(email):
            raise HTTPException(status_code=400, detail=f"contact {i}: not a valid email address")
        if email.lower() in seen:
            raise HTTPException(status_code=400, detail=f"{email} is listed twice")
        seen.add(email.lower())
        label = c.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > 100):
            raise HTTPException(status_code=400, detail=f"contact {i}: label must be text of at most 100 characters")
        label = re.sub(r"[\x00-\x1f]", " ", label).strip() if isinstance(label, str) else None
        outage, cert = bool(c.get("notify_outage", True)), bool(c.get("notify_cert", True))
        if not outage and not cert:
            raise HTTPException(status_code=400, detail=f"contact {i} ({email}) receives nothing: turn on outages or certificates")
        out.append({"email": email, "label": label or None, "notify_outage": outage, "notify_cert": cert})
    return out


def _mask_email(addr: str) -> str:
    local, _, domain = (addr or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "(hidden)"


def _require_account_access(account_id: int, current_user: dict) -> None:
    accessible = get_accessible_account_ids(current_user)
    if accessible is not None and account_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")


def _get_check_account_id(check_id: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT aws_account_id FROM synthetic_checks WHERE id = %s", (check_id,))
        row = cur.fetchone()
        return row["aws_account_id"] if row else None
    finally:
        cur.close(); conn.close()


def _get_check_type(check_id: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT check_type FROM synthetic_checks WHERE id = %s", (check_id,))
        row = cur.fetchone()
        return row["check_type"] if row else None
    finally:
        cur.close(); conn.close()


def _get_check_row(check_id: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT check_type, target, expect_https_redirect, cert_warn_days, cert_crit_days FROM synthetic_checks WHERE id = %s",
            (check_id,))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


@router.get("")
def list_checks(current_user: dict = Depends(require_permission("synthetic.view"))):
    """
    Includes a lightweight uptime_pct_24h computed inline from
    synthetic_check_results -- avoids a second round trip from the
    frontend's list view for the single number people actually look at
    first (Pingdom/UptimeRobot's own list views lead with the same
    number for the same reason).

    For 'https' checks also returns the latest TLS facts (cert_days_left,
    cert_not_after, cert_subject, cert_issuer, tls_version, tls_cipher,
    handshake_ms, cert_valid, tls_checked_at) taken from the most recent result
    that reached a TLS session. For every check it also returns the latest probe's
    `last_error` text, `last_ok` (1/0) and `last_cert_valid`: last_ok = 0 means the
    text is a failure; last_ok = 1 with text means a non-failing "Notice: ..." (e.g.
    http:// answers without redirecting to https).

    https rows also carry cert_warn_days / cert_crit_days: the thresholds the cert-expiry
    alert is actually using (SYNTHETIC_CERT_WARN_DAYS / _CRIT_DAYS, defaults 30 / 7), so the
    UI colours and chart lines always agree with the alert.
    """
    accessible = get_accessible_account_ids(current_user)
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT c.*, acc.account_name,
                   (SELECT COUNT(*) FROM synthetic_check_contacts k WHERE k.check_id = c.id) AS contact_count,
                   (SELECT COUNT(*) FROM synthetic_check_contacts k WHERE k.check_id = c.id AND k.notify_cert = 1) AS cert_contact_count,
                   (SELECT COUNT(*) FROM synthetic_check_contacts k WHERE k.check_id = c.id AND k.notify_outage = 1) AS outage_contact_count,
                   (SELECT ROUND(100 * AVG(r.success), 1)
                    FROM synthetic_check_results r
                    WHERE r.check_id = c.id
                      AND r.checked_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                      AND (c.target_changed_at IS NULL OR r.checked_at >= c.target_changed_at)) AS uptime_pct_24h,
                   lr.error_message AS last_error, lr.success AS last_ok,
                   lr.cert_valid AS last_cert_valid,
                   lt.checked_at AS tls_checked_at, lt.cert_days_left, lt.cert_not_after,
                   lt.cert_subject, lt.cert_issuer, lt.tls_version, lt.tls_cipher,
                   lt.handshake_ms, lt.cert_valid
            FROM synthetic_checks c
            JOIN aws_accounts acc ON acc.id = c.aws_account_id
            LEFT JOIN synthetic_check_results lr
                   ON lr.id = (SELECT r3.id FROM synthetic_check_results r3
                               WHERE r3.check_id = c.id
                                 AND (c.target_changed_at IS NULL OR r3.checked_at >= c.target_changed_at)
                               ORDER BY r3.checked_at DESC, r3.id DESC LIMIT 1)
            LEFT JOIN synthetic_check_results lt
                   ON c.check_type = 'https'
                  AND lt.id = (SELECT r2.id FROM synthetic_check_results r2
                               WHERE r2.check_id = c.id AND r2.tls_version IS NOT NULL
                                 AND (c.target_changed_at IS NULL OR r2.checked_at >= c.target_changed_at)
                               ORDER BY r2.checked_at DESC, r2.id DESC LIMIT 1)
            ORDER BY c.name
        """)
        rows = cur.fetchall()
        if accessible is not None:
            rows = [r for r in rows if r["aws_account_id"] in accessible]
        for r in rows:
            if r.get("check_type") == "https":
                # this check's own pair when it has one, else the global defaults: what the alert really uses
                r["cert_thresholds_custom"] = (r.get("cert_warn_days"), r.get("cert_crit_days")) != (None, None) \
                    and effective_cert_thresholds(r) != (CERT_WARN_DAYS, CERT_CRIT_DAYS)
                r["cert_warn_days"], r["cert_crit_days"] = effective_cert_thresholds(r)
        return rows
    finally:
        cur.close(); conn.close()


# rows returned per results request: a 30-day window at a 60 s interval is ~43k rows
_RESULTS_DEFAULT_LIMIT = 20000
_RESULTS_MAX_LIMIT = 50000


@router.get("/{check_id}/results")
def get_check_results(
    check_id: int,
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(_RESULTS_DEFAULT_LIMIT, ge=1, le=_RESULTS_MAX_LIMIT),
    current_user: dict = Depends(require_permission("synthetic.view")),
):
    account_id = _get_check_account_id(check_id)
    if account_id is None:
        raise HTTPException(status_code=404, detail="Check not found")
    _require_account_access(account_id, current_user)

    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        scope = """
            FROM synthetic_check_results r
            JOIN synthetic_checks c ON c.id = r.check_id
            WHERE r.check_id = %s AND r.checked_at >= DATE_SUB(NOW(), INTERVAL %s HOUR)
              AND (c.target_changed_at IS NULL OR r.checked_at >= c.target_changed_at)
        """
        # uptime is computed over the WHOLE window in SQL, so capping the rows returned never skews it
        cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(r.success), 0) AS ok " + scope, (check_id, hours))
        agg = cur.fetchone()
        total, ok = int(agg["n"] or 0), int(agg["ok"] or 0)
        # newest `limit` rows (a long window at a short interval can be tens of thousands), returned oldest-first
        cur.execute("""
            SELECT r.checked_at, r.success, r.response_time_ms, r.status_code, r.error_message,
                   r.cert_days_left, r.cert_not_after, r.cert_subject, r.cert_issuer,
                   r.tls_version, r.tls_cipher, r.handshake_ms, r.cert_valid
            """ + scope + """
            ORDER BY r.checked_at DESC, r.id DESC LIMIT %s
        """, (check_id, hours, limit))
        results = list(reversed(cur.fetchall()))
        uptime_pct = round(100 * ok / total, 2) if total else None
        # only reported when the target change cut this window short, so the UI can say why older probes are missing
        cur.execute("""
            SELECT target_changed_at FROM synthetic_checks
            WHERE id = %s AND target_changed_at >= DATE_SUB(NOW(), INTERVAL %s HOUR)
        """, (check_id, hours))
        tc = cur.fetchone()
        return {"check_id": check_id, "window_hours": hours, "uptime_pct": uptime_pct, "results": results,
                "total_results": total, "truncated": total > len(results),
                "target_changed_at": tc["target_changed_at"] if tc else None}
    finally:
        cur.close(); conn.close()


@router.post("")
def create_check(payload: dict = Body(...), current_user: dict = Depends(require_permission("synthetic.manage"))):
    account_id = payload.get("aws_account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="aws_account_id is required")
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="aws_account_id must be an integer")
    _require_account_access(account_id, current_user)

    check_type = payload.get("check_type", "http")
    if check_type not in _VALID_CHECK_TYPES:
        raise HTTPException(status_code=400, detail=f"check_type must be one of {_VALID_CHECK_TYPES}")

    fields = _validate_common({
        "name": payload.get("name"),
        "timeout_seconds": payload.get("timeout_seconds", 10),
        "interval_seconds": payload.get("interval_seconds", 300),
        "consecutive_failure_threshold": payload.get("consecutive_failure_threshold", 2),
        "expected_status_code": payload.get("expected_status_code"),
        "expected_keyword": payload.get("expected_keyword"),
        "environment": payload.get("environment", "prod"),
        "enabled": payload.get("enabled", True),
        "expect_https_redirect": payload.get("expect_https_redirect", False),
    })

    certs = _cert_pair(payload, check_type)
    contacts = _clean_contacts(payload.get("contacts", [])) if "contacts" in payload else []

    target = (payload.get("target") or "").strip() if isinstance(payload.get("target"), str) else ""
    try:
        # strict_scheme: a NEW https:// URL must use the 'https' type (existing
        # 'http' checks with https:// URLs keep running; see validate_target)
        validate_target(check_type, target, strict_scheme=True)
        if fields["expect_https_redirect"]:
            validate_https_redirect_option(check_type, target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO synthetic_checks
                (aws_account_id, name, check_type, target, expected_status_code,
                 expected_keyword, expect_https_redirect, timeout_seconds, interval_seconds,
                 consecutive_failure_threshold, environment, enabled, created_by, cert_warn_days, cert_crit_days)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            int(account_id), fields["name"], check_type, target,
            fields["expected_status_code"], fields["expected_keyword"],
            fields["expect_https_redirect"],
            fields["timeout_seconds"], fields["interval_seconds"],
            fields["consecutive_failure_threshold"],
            fields["environment"], fields["enabled"],
            int(current_user["id"]), certs.get("cert_warn_days"), certs.get("cert_crit_days"),
        ))
        new_id = cur.lastrowid
        for c in contacts:
            cur.execute("""INSERT INTO synthetic_check_contacts (check_id, email, label, notify_outage, notify_cert, created_by)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (new_id, c["email"], c["label"], c["notify_outage"], c["notify_cert"], current_user.get("username")))
        conn.commit()
        return {"status": "created", "id": new_id}
    except Exception:
        conn.rollback()
        logger.exception("create synthetic check failed")
        raise HTTPException(status_code=400, detail="Could not create synthetic check")
    finally:
        cur.close(); conn.close()


# PATCH fields that change what a probe does; editing one restarts the failure streak (see update_check)
_PROBE_AFFECTING_FIELDS = ("target", "expected_status_code", "expected_keyword", "timeout_seconds", "expect_https_redirect")


@router.patch("/{check_id}")
def update_check(check_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("synthetic.manage"))):
    account_id = _get_check_account_id(check_id)
    if account_id is None:
        raise HTTPException(status_code=404, detail="Check not found")
    _require_account_access(account_id, current_user)

    editable_fields = (
        "name", "target", "expected_status_code", "expected_keyword",
        "timeout_seconds", "interval_seconds", "consecutive_failure_threshold",
        "environment", "enabled", "expect_https_redirect", "cert_warn_days", "cert_crit_days",
    )
    raw = {k: v for k, v in payload.items() if k in editable_fields}
    if not raw:
        raise HTTPException(status_code=400, detail="No editable fields provided")
    cert_raw = {k: raw.pop(k) for k in ("cert_warn_days", "cert_crit_days") if k in raw}
    updates = _validate_common(raw)
    if cert_raw:
        stored = _get_check_row(check_id)
        updates.update(_cert_pair(cert_raw, stored["check_type"], stored))

    target_changed = False
    if "target" in raw or "expect_https_redirect" in raw:
        row = _get_check_row(check_id)
        check_type = row["check_type"]
        if "target" in raw:
            # SSRF: a PATCH must pass the same target validation as create.
            target = raw["target"].strip() if isinstance(raw["target"], str) else ""
            try:
                validate_target(check_type, target)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            updates["target"] = target
            target_changed = target != (row["target"] or "")
        # the redirect flag must stay valid for whatever the target ends up being
        if updates.get("expect_https_redirect", row["expect_https_redirect"]):
            try:
                validate_https_redirect_option(check_type, updates.get("target", row["target"]))
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

    set_parts = [f"{k} = %s" for k in updates]
    params = list(updates.values())
    # An edit that changes WHAT is probed must not leave the old target's failure streak / schedule behind: the
    # streak restarts and the next collector tick probes with the new settings. (current_status and the open
    # alert are left alone -- the next probe moves them through the normal path.) A new interval pulls an
    # already-later next_check_at in, so a shorter interval takes effect without waiting out the old one.
    if target_changed:
        # a different host: results from before this moment describe the OLD target and are no longer shown
        # in the list's TLS / last-error / uptime figures or in the detail drawer (rows are kept)
        set_parts.append("target_changed_at = NOW()")
    if any(k in updates for k in _PROBE_AFFECTING_FIELDS):
        set_parts += ["consecutive_failures = 0", "next_check_at = NOW()"]
    elif "interval_seconds" in updates:
        set_parts.append("next_check_at = LEAST(COALESCE(next_check_at, NOW()), DATE_ADD(NOW(), INTERVAL %s SECOND))")
        params.append(updates["interval_seconds"])
    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute(
            f"UPDATE synthetic_checks SET {', '.join(set_parts)} WHERE id = %s",
            (*params, check_id),
        )
        conn.commit()
        return {"status": "updated"}
    finally:
        cur.close(); conn.close()


@router.delete("/{check_id}")
def delete_check(check_id: int, current_user: dict = Depends(require_permission("synthetic.manage"))):
    account_id = _get_check_account_id(check_id)
    if account_id is None:
        raise HTTPException(status_code=404, detail="Check not found")
    _require_account_access(account_id, current_user)

    conn = get_connection(); cur = conn.cursor()
    try:
        # Resolve any still-active alert for this check's synthetic
        # resource first -- deleting the check shouldn't leave a
        # permanently-open alert with no configuration behind it.
        cur.execute("""
            UPDATE alerts SET status = 'resolved', resolved_at = UTC_TIMESTAMP(), last_seen_at = UTC_TIMESTAMP(),
                              resolution_reason = 'check_deleted', resolved_by = %s
            WHERE aws_account_id = %s AND resource_id = %s
              AND metric_name IN ('synthetic_uptime', %s)
              AND status IN ('active', 'acknowledged')
        """, (current_user["username"], account_id, f"synthetic-{check_id}", CERT_ALERT_METRIC))
        cur.execute("DELETE FROM synthetic_checks WHERE id = %s", (check_id,))
        # F25: drop the auto-created resources row too (collector's
        # _ensure_resource_row), otherwise every deleted check left an
        # orphan "synthetic-N" resource in inventory/topology forever.
        cur.execute("""
            DELETE FROM resources
            WHERE aws_account_id = %s AND resource_type = 'synthetic_check' AND resource_id = %s
        """, (account_id, f"synthetic-{check_id}"))
        conn.commit()
        return {"status": "deleted"}
    finally:
        cur.close(); conn.close()


# ── Alert contacts (who is told about THIS check) ─────────────────────
# Email addresses are personal data: anyone with synthetic.view sees how many people are told and a masked address,
# only synthetic.manage sees (and can change) the full list. Account scope is enforced like every other route here.

def _is_manager(current_user: dict) -> bool:
    from app.auth.permissions import has_permission
    return has_permission(current_user, "synthetic.manage")


@router.get("/{check_id}/contacts")
def get_contacts(check_id: int, current_user: dict = Depends(require_permission("synthetic.view"))):
    account_id = _get_check_account_id(check_id)
    if account_id is None:
        raise HTTPException(status_code=404, detail="Check not found")
    _require_account_access(account_id, current_user)
    manager = _is_manager(current_user)
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT email, label, notify_outage, notify_cert FROM synthetic_check_contacts "
                    "WHERE check_id = %s ORDER BY id", (check_id,))
        rows = cur.fetchall()
        cur.execute("SELECT cert_warn_days, cert_crit_days, check_type FROM synthetic_checks WHERE id = %s", (check_id,))
        chk = cur.fetchone() or {}
    finally:
        cur.close(); conn.close()
    warn, crit = effective_cert_thresholds(chk)
    contacts = [{"email": r["email"] if manager else _mask_email(r["email"]), "label": r["label"],
                 "notify_outage": bool(r["notify_outage"]), "notify_cert": bool(r["notify_cert"])} for r in rows]
    return {"check_id": check_id, "contacts": contacts, "editable": manager, "max_contacts": MAX_CONTACTS,
            "cert_warn_days": warn, "cert_crit_days": crit,
            "cert_thresholds_custom": (chk.get("cert_warn_days"), chk.get("cert_crit_days")) != (None, None),
            "default_warn_days": CERT_WARN_DAYS, "default_crit_days": CERT_CRIT_DAYS}


@router.put("/{check_id}/contacts")
def put_contacts(check_id: int, payload: dict = Body(...),
                 current_user: dict = Depends(require_permission("synthetic.manage"))):
    """Replaces the check's whole contact list (the UI edits the list as a unit)."""
    account_id = _get_check_account_id(check_id)
    if account_id is None:
        raise HTTPException(status_code=404, detail="Check not found")
    _require_account_access(account_id, current_user)
    contacts = _clean_contacts(payload.get("contacts"))
    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute("DELETE FROM synthetic_check_contacts WHERE check_id = %s", (check_id,))
        for c in contacts:
            cur.execute("""INSERT INTO synthetic_check_contacts (check_id, email, label, notify_outage, notify_cert, created_by)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (check_id, c["email"], c["label"], c["notify_outage"], c["notify_cert"], current_user.get("username")))
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("saving synthetic contacts failed")
        raise HTTPException(status_code=400, detail="Could not save the contacts")
    finally:
        cur.close(); conn.close()
    try:
        from app.audit import write_audit
        write_audit(current_user["username"], "Synthetic check contacts changed",
                    f"check_id={check_id} contacts={len(contacts)}", role=current_user.get("role"))
    except Exception:
        logger.warning("audit write failed for synthetic contacts", exc_info=True)
    return {"status": "saved", "count": len(contacts)}


@router.post("/{check_id}/contacts/test")
def test_contacts(check_id: int, current_user: dict = Depends(require_permission("synthetic.manage"))):
    """Sends one clearly-labelled test email to every contact of the check, so people know it will reach them."""
    from app.email import mailer
    account_id = _get_check_account_id(check_id)
    if account_id is None:
        raise HTTPException(status_code=404, detail="Check not found")
    _require_account_access(account_id, current_user)
    if not mailer.is_configured():
        raise HTTPException(status_code=501, detail="Email is not set up on this server yet (SMTP_HOST). "
                                                    "An administrator must configure SMTP before alerts can be emailed.")
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT c.name, c.target FROM synthetic_checks c WHERE c.id = %s", (check_id,))
        chk = cur.fetchone()
        cur.execute("SELECT email FROM synthetic_check_contacts WHERE check_id = %s ORDER BY id", (check_id,))
        addrs = [r["email"] for r in cur.fetchall()]
    finally:
        cur.close(); conn.close()
    if not addrs:
        raise HTTPException(status_code=400, detail="This check has no contacts to test")
    subject = f"[CloudOps] Test: alert contacts for {chk['name']}"
    body = (f"This is a test message from CloudOps.\n\nYou are listed as an alert contact for the synthetic check "
            f"'{chk['name']}' ({chk['target']}). You will be emailed if it goes down or its certificate is about to "
            f"expire. If you can read this, delivery works.\n\nSent by {current_user.get('username')}.")
    sent = sum(1 for a in addrs if mailer.send_email(a, subject, body))
    try:
        from app.audit import write_audit
        write_audit(current_user["username"], "Synthetic contacts tested",
                    f"check_id={check_id} sent={sent}/{len(addrs)}", role=current_user.get("role"))
    except Exception:
        logger.warning("audit write failed for synthetic contact test", exc_info=True)
    return {"status": "sent" if sent == len(addrs) else ("partial" if sent else "failed"), "sent": sent, "total": len(addrs)}
