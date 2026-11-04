# app/email/alert_rules.py
"""
Alert and incident email rules (pure: no database, no clock of its own, no mail).

A rule says: when one of THESE events happens at or above THIS severity (for THIS account, or every account), mail THIS
recipient list. This module validates a rule, decides whether a rule matches an event, and decides whether a mail to one
person may go out now (throttle and hourly cap). Reading and writing rows is the API's job; sending is patch 0005.

Knobs (.env, clamped to a safe range; a bad value falls back to the default):
  EMAIL_RULE_THROTTLE_SECONDS         1800   one mail per recipient, per rule, per resource, per window
  EMAIL_RULE_MAX_PER_RECIPIENT_HOUR   20     hard cap per rule and recipient, however many resources
  EMAIL_RULE_SWEEP_MINUTES            30     how far back the resolved / incident sweep looks
"""
import hashlib
import os
from datetime import timedelta

from app.email import recipients

EVENTS = ("alert_opened", "alert_escalated", "alert_resolved", "incident_opened", "incident_resolved")
EVENT_LABELS = {"alert_opened": "Alert opened", "alert_escalated": "Alert escalated", "alert_resolved": "Alert resolved",
                "incident_opened": "Incident opened", "incident_resolved": "Incident resolved"}
SEVERITIES = ("WARNING", "CRITICAL")
_RANK = {"WARNING": 1, "CRITICAL": 2}

_KNOBS = {  # name: (default, lowest, highest)
    "EMAIL_RULE_THROTTLE_SECONDS": (1800, 60, 86400),
    "EMAIL_RULE_MAX_PER_RECIPIENT_HOUR": (20, 1, 200),
    "EMAIL_RULE_SWEEP_MINUTES": (30, 5, 1440),
}


def _knob(name):
    default, lo, hi = _KNOBS[name]
    try:
        n = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return n if lo <= n <= hi else default


def throttle_seconds():
    return _knob("EMAIL_RULE_THROTTLE_SECONDS")


def max_per_hour():
    return _knob("EMAIL_RULE_MAX_PER_RECIPIENT_HOUR")


def sweep_minutes():
    return _knob("EMAIL_RULE_SWEEP_MINUTES")


def recipient_key(address):
    return hashlib.sha1(str(address).strip().lower().encode("utf-8")).hexdigest()


def resource_key(account_id, resource_id):
    """One key per resource; resource ids repeat across accounts, so the account is part of it."""
    return hashlib.sha1(f"{account_id}|{resource_id}".encode("utf-8")).hexdigest()


def parse_events(value):
    """CSV text or list -> list of known events, in EVENTS order. Raises ValueError for an unknown one or an empty set."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    chosen = {str(v).strip().lower() for v in items if str(v).strip()}
    bad = sorted(chosen - set(EVENTS))
    if bad:
        raise ValueError(f"unknown event: {bad[0][:40]} (choose from {list(EVENTS)})")
    if not chosen:
        raise ValueError("choose at least one event")
    return [e for e in EVENTS if e in chosen]


def normalize(payload, current=None):
    """API payload (+ the stored row on update) -> the columns to write. Raises ValueError with a user-safe message."""
    cur = current or {}
    get = lambda k: payload[k] if k in payload else cur.get(k)

    name = get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not name or len(name) > 120:
        raise ValueError("name is required (max 120 characters)")

    events = parse_events(get("events"))

    severity = str(get("min_severity") or "WARNING").strip().upper()
    if severity not in SEVERITIES:
        raise ValueError(f"min_severity must be one of {list(SEVERITIES)}")

    account_id = get("account_id")
    if account_id in (None, ""):
        account_id = None
    else:
        if isinstance(account_id, bool):
            raise ValueError("account_id must be a number")
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            raise ValueError("account_id must be a number")
        if account_id <= 0:
            raise ValueError("account_id must be a positive number")

    enabled = get("enabled")
    if enabled is None:
        enabled = True
    if not isinstance(enabled, (bool, int)) or (isinstance(enabled, int) and enabled not in (0, 1)):
        raise ValueError("enabled must be true or false")

    list_id = get("recipient_list_id")
    if list_id in (None, ""):
        list_id = None
    else:
        if isinstance(list_id, bool):
            raise ValueError("recipient_list_id must be a number")
        try:
            list_id = int(list_id)
        except (TypeError, ValueError):
            raise ValueError("recipient_list_id must be a number")
        if list_id <= 0:
            raise ValueError("recipient_list_id must be a positive number")

    extra = recipients.parse_emails(get("extra_emails"))
    if list_id is None and not extra:
        raise ValueError("a rule needs a recipient list or at least one extra email address")

    return {"name": name, "enabled": 1 if enabled else 0, "events": ",".join(events), "min_severity": severity,
            "account_id": account_id, "recipient_list_id": list_id, "extra_emails": ", ".join(extra) if extra else None}


def matches(rule, event, severity, account_id):
    """Does this stored rule want mail for this event? Disabled rules never match."""
    if not rule.get("enabled"):
        return False
    if event not in [e.strip() for e in str(rule.get("events") or "").split(",")]:
        return False
    if _RANK.get(str(severity or "").upper(), 0) < _RANK.get(str(rule.get("min_severity") or "WARNING").upper(), 1):
        return False
    rule_account = rule.get("account_id")
    return rule_account is None or (account_id is not None and int(rule_account) == int(account_id))


def decide(now, recent, res_key, throttle=None, cap=None, already_sent=False):
    """May ONE mail go to ONE person for ONE rule right now? -> (ok, reason).
    `recent` = this rule's sends to this person in the last hour: [{"sent_at": datetime, "resource_key": str|None}].
    `already_sent` = this person already got this event for this alert / incident (the once-ever rule)."""
    throttle = throttle_seconds() if throttle is None else throttle
    cap = max_per_hour() if cap is None else cap
    if already_sent:
        return False, "already mailed for this event"
    hour_ago = now - timedelta(hours=1)
    last_hour = [r for r in recent if r["sent_at"] > hour_ago]
    if len(last_hour) >= cap:
        return False, "hourly cap reached"
    window = now - timedelta(seconds=throttle)
    if res_key and any(r.get("resource_key") == res_key and r["sent_at"] > window for r in recent):
        return False, "throttled (same resource, inside the window)"
    return True, ""


def describe(rule):
    names = [EVENT_LABELS.get(e.strip(), e.strip()) for e in str(rule.get("events") or "").split(",") if e.strip()]
    return f"{', '.join(names)} at {str(rule.get('min_severity') or 'WARNING').title()} or higher"
