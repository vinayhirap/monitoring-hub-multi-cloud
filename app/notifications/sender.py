# app/notifications/sender.py
"""
Alert notification delivery (audit C9).

  notify_alert_event(event, alert)   -> fan out to every matching enabled channel; never raises
  send_test(channel)                 -> one synthetic message to one channel (the "Test" button)
  mask_target(type, target)          -> what the API may show: host only for URLs, addresses for email

Safety properties (each covered by tests):
  * Webhook-style targets go through the same SSRF guard as synthetic checks (app.collector.synthetic):
    the IP actually dialled is validated on every hop, so a channel pointed at 169.254.169.254, localhost or a
    private range is refused, and redirects are not followed into one.
  * A failing or slow channel never affects the others, and never blocks or breaks alert evaluation: the
    caller runs this after commit inside try/except, and each send has a short timeout.
  * Silenced (maintenance-window) alerts are not notified.
  * Every attempt is recorded in notification_log (sent / failed / skipped) without the secret URL.
  * The message names account, resource, metric, value and threshold, and links to the app, nothing else.
"""
import json
import logging
import re
from urllib.parse import urlsplit

from app.db import get_connection

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"WARNING": 1, "CRITICAL": 2}
CHANNEL_TYPES = ("email", "slack", "teams", "webhook")
VALID_EVENTS = ("opened", "escalated")
SEND_TIMEOUT_SECONDS = 6
_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


# ── validation (used by the API) ─────────────────────────────────────

def parse_addresses(target: str) -> list:
    return [a.strip() for a in (target or "").split(",") if a.strip()]


def validate_channel(ctype: str, target: str) -> str:
    """Returns the normalised target or raises ValueError with a user-safe message."""
    if ctype not in CHANNEL_TYPES:
        raise ValueError(f"type must be one of {list(CHANNEL_TYPES)}")
    target = (target or "").strip()
    if not target:
        raise ValueError("target is required")
    if len(target) > 1000:
        raise ValueError("target is too long")
    if ctype == "email":
        addrs = parse_addresses(target)
        if not addrs or len(addrs) > 20:
            raise ValueError("give 1 to 20 comma-separated email addresses")
        for a in addrs:
            if "\r" in a or "\n" in a or not _EMAIL_RE.match(a):
                raise ValueError(f"not a valid email address: {a[:60]}")
        return ", ".join(addrs)
    parts = urlsplit(target)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("webhook targets must be a full https:// URL")
    if parts.username or parts.password:
        raise ValueError("do not put credentials in the URL")
    from app.collector.synthetic import validate_target
    validate_target("http", target)         # SSRF: rejects loopback / private / metadata addresses
    return target


def mask_target(ctype: str, target: str) -> str:
    if ctype == "email":
        return target
    try:
        host = urlsplit(target).hostname or ""
    except ValueError:
        host = ""
    return f"https://{host}/…" if host else "(hidden)"


# ── message construction ─────────────────────────────────────────────

def _app_url() -> str:
    try:
        from app.email import mailer
        return mailer.get_public_app_url()
    except Exception:
        return ""


def _fmt(v):
    try:
        f = float(v)
        return f"{f:.2f}".rstrip("0").rstrip(".") if abs(f) < 1e6 else f"{f:.3g}"
    except (TypeError, ValueError):
        return str(v)


def build_message(event: str, a: dict) -> dict:
    title = {"opened": "Alert opened", "escalated": "Alert escalated"}.get(event, "Alert")
    sev = a.get("severity", "")
    bits = [f"{sev}: {a.get('metric_name', '')}", f"on {a.get('resource_id', '')}"]
    if a.get("account_name"):
        bits.append(f"({a['account_name']})")
    headline = f"[CloudOps] {title} - " + " ".join(bits)
    lines = [headline]
    if a.get("value") is not None and a.get("threshold") is not None:
        lines.append(f"Value {_fmt(a['value'])} against threshold {_fmt(a['threshold'])}")
    if event == "escalated" and a.get("group_name"):
        lines.append(f"Escalated to group: {a['group_name']}")
    url = _app_url()
    if url:
        lines.append(f"Open: {url}/alerts")
    return {"headline": headline, "text": "\n".join(lines), "alert": {
        "id": a.get("id"), "severity": sev, "metric": a.get("metric_name"),
        "resource_id": a.get("resource_id"), "account": a.get("account_name"),
        "value": a.get("value"), "threshold": a.get("threshold"), "event": event,
    }}


# ── transport ────────────────────────────────────────────────────────

def _post_json(url: str, payload: dict) -> None:
    from app.collector.synthetic import _guarded_session
    session = _guarded_session()
    try:
        resp = session.post(url, data=json.dumps(payload), headers={"Content-Type": "application/json"},
                            timeout=SEND_TIMEOUT_SECONDS, allow_redirects=False)
    finally:
        session.close()
    if resp.status_code >= 300:       # a redirect is treated as failure: never follow it somewhere else
        raise RuntimeError(f"endpoint answered HTTP {resp.status_code}")


def deliver(channel: dict, message: dict) -> None:
    """Raises on failure. Transport only; logging/recording is the caller's job."""
    ctype, target = channel["type"], channel["target"]
    if ctype == "email":
        from app.email import mailer
        if not mailer.is_configured():
            raise RuntimeError("SMTP is not configured (SMTP_HOST unset)")
        ok_count = 0
        for addr in parse_addresses(target):
            if mailer.send_email(addr, message["headline"], message["text"]):
                ok_count += 1
        if not ok_count:
            raise RuntimeError("no email could be sent - check the server log")
    elif ctype == "slack":
        _post_json(target, {"text": message["text"]})
    elif ctype == "teams":
        _post_json(target, {"@type": "MessageCard", "@context": "https://schema.org/extensions",
                            "summary": message["headline"], "text": message["text"].replace("\n", "<br>")})
    elif ctype == "webhook":
        _post_json(target, {"source": "cloudops", **message["alert"], "message": message["text"]})
    else:
        raise RuntimeError(f"unknown channel type {ctype!r}")


def _short_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return re.sub(r"https?://\S+", "<url>", text)[:300]      # never persist a (secret) URL in the log


def _record(cursor, channel, alert_id, event, status, detail=None):
    cursor.execute(
        "INSERT INTO notification_log (channel_id, channel_name, alert_id, event, status, detail) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (channel.get("id"), channel.get("name"), alert_id, event, status, (detail or None)),
    )


# ── selection + fan-out ──────────────────────────────────────────────

def channel_matches(channel: dict, event: str, severity: str, account_id) -> bool:
    if not channel.get("enabled", 1):
        return False
    if event not in (channel.get("events") or "").split(","):
        return False
    if SEVERITY_RANK.get(severity, 0) < SEVERITY_RANK.get(channel.get("min_severity"), 2):
        return False
    scoped = channel.get("aws_account_id")
    return scoped is None or account_id is None or int(scoped) == int(account_id)


def notify_alert_event(event: str, alert: dict) -> int:
    """Delivers to every matching channel. Returns the number of successful deliveries. Never raises."""
    try:
        if alert.get("silenced"):
            return 0
        conn = get_connection()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT * FROM notification_channels WHERE enabled = 1")
            channels = [c for c in cur.fetchall()
                        if channel_matches(c, event, alert.get("severity"), alert.get("aws_account_id"))]
            if not channels:
                return 0
            message = build_message(event, alert)
            sent = 0
            for ch in channels:
                try:
                    deliver(ch, message)
                    _record(cur, ch, alert.get("id"), event, "sent")
                    sent += 1
                except Exception as exc:
                    logger.warning(f"[notify] channel {ch.get('name')!r} failed: {_short_error(exc)}")
                    try:
                        _record(cur, ch, alert.get("id"), event, "failed", _short_error(exc))
                    except Exception:
                        pass
            conn.commit()
            return sent
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[notify] notification fan-out failed: {_short_error(exc)}")
        return 0


def send_test(channel: dict) -> None:
    """Raises on failure (the API turns that into a clear message)."""
    msg = build_message("opened", {"id": 0, "severity": "WARNING", "metric_name": "TestNotification",
                                   "resource_id": "channel-test", "account_name": "CloudOps",
                                   "value": 1, "threshold": 0})
    msg["headline"] = "[CloudOps] Test notification"
    msg["text"] = "This is a test message from CloudOps. If you can read it, this channel works."
    deliver(channel, msg)
