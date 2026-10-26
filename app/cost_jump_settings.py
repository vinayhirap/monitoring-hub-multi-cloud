# app/cost_jump_settings.py
"""
Settings for the cost-jump alert (app/collector/cost_jump_alert.py): on/off, percentage above the 7-day average, and the
minimum rise in USD per day. Editable from the Cloud cost page (usage.manage), for all accounts and per account.

Stored in cost_jump_settings (migration 132): scope_key 0 = all accounts, scope_key N = account N. NULL = inherit.
Resolution per field: the account's own row -> the all-accounts row -> the server environment
(COST_JUMP_ALERT_ENABLED, COST_JUMP_THRESHOLD_PCT, COST_JUMP_MIN_INCREASE_USD) -> the built-in default.

RECIPIENTS (migration 133): notify_user_ids (CloudOps users, by id) and notify_emails (typed addresses) say who is emailed
when the alert opens, on top of the notification channels already configured. They are ADDITIVE, not inherited: an
account is notified at its own recipients plus the all-accounts recipients. NULL / empty = nobody extra at that scope.

Dependency-free on purpose (no FastAPI): the collector and the API both import it. Takes a cursor, never opens one.
"""
import json
import math
import os
import re

GLOBAL_KEY = 0
FIELDS = ("enabled", "threshold_pct", "min_increase_usd")
BUILTIN = {"enabled": False, "threshold_pct": 30.0, "min_increase_usd": 0.10}
LIMITS = {"threshold_pct": (1.0, 1000.0), "min_increase_usd": (0.0, 100000.0)}
_ENV = {"enabled": "COST_JUMP_ALERT_ENABLED", "threshold_pct": "COST_JUMP_THRESHOLD_PCT",
        "min_increase_usd": "COST_JUMP_MIN_INCREASE_USD"}
RECIPIENT_FIELDS = ("notify_user_ids", "notify_emails")
MAX_RECIPIENT_USERS = 50
MAX_RECIPIENT_EMAILS = 20
_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")       # same rule as notification channels
_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


def env_defaults():
    """{field: (value, source)} from the server environment, 'environment' only when the variable is set and valid."""
    out = {}
    for f in FIELDS:
        raw = os.getenv(_ENV[f])
        value, source = BUILTIN[f], "default"
        if raw is not None and raw.strip() != "":
            try:
                if f == "enabled":
                    low = raw.strip().lower()
                    if low in _TRUE or low in _FALSE:
                        value, source = low in _TRUE, "environment"
                else:
                    v = float(raw)
                    lo, hi = LIMITS[f]
                    if math.isfinite(v) and lo <= v <= hi:
                        value, source = v, "environment"
            except ValueError:
                pass
        out[f] = (value, source)
    return out


def validate(raw):
    """Request body -> {field: value | None} for the fields present (None = inherit). Raises ValueError with a plain message."""
    if not isinstance(raw, dict):
        raise ValueError("Send an object with enabled, threshold_pct and/or min_increase_usd.")
    clean = {}
    for f in FIELDS:
        if f not in raw:
            continue
        v = raw[f]
        if v is None:
            clean[f] = None
        elif f == "enabled":
            if not isinstance(v, bool):
                raise ValueError("enabled must be true, false or null (inherit).")
            clean[f] = v
        else:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"{f} must be a number or null (inherit).")
            lo, hi = LIMITS[f]
            if not lo <= v <= hi:
                raise ValueError(f"{f} must be between {lo:g} and {hi:g}.")
            clean[f] = round(float(v), 2 if f == "threshold_pct" else 4)
    if "notify_user_ids" in raw:
        clean["notify_user_ids"] = _clean_user_ids(raw["notify_user_ids"])
    if "notify_emails" in raw:
        clean["notify_emails"] = _clean_emails(raw["notify_emails"])
    if not clean:
        raise ValueError("Nothing to change: send at least one of enabled, threshold_pct, min_increase_usd, "
                         "notify_user_ids, notify_emails.")
    return clean


def _clean_user_ids(v):
    if v is None or v == []:
        return None
    if not isinstance(v, list) or any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in v):
        raise ValueError("notify_user_ids must be a list of user ids, or null.")
    ids = sorted(set(v))
    if len(ids) > MAX_RECIPIENT_USERS:
        raise ValueError(f"At most {MAX_RECIPIENT_USERS} users per scope.")
    return ids


def _clean_emails(v):
    if v is None or v == [] or v == "":
        return None
    if isinstance(v, str):
        v = re.split(r"[,;\s]+", v.strip())
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise ValueError("notify_emails must be a list of email addresses (or a comma-separated string), or null.")
    out = []
    for raw in v:
        a = raw.strip().lower()
        if not a:
            continue
        if "\r" in a or "\n" in a or len(a) > 254 or not _EMAIL_RE.match(a):
            raise ValueError(f"Not a valid email address: {a[:60]}")
        if a not in out:
            out.append(a)
    if len(out) > MAX_RECIPIENT_EMAILS:
        raise ValueError(f"At most {MAX_RECIPIENT_EMAILS} email addresses per scope.")
    return out or None


def resolve(settings, account_pk=None):
    """{field: (value, source)}; source = 'account' | 'all accounts' | 'environment' | 'default'."""
    base = env_defaults()
    glob = (settings or {}).get("global") or {}
    acct = ((settings or {}).get("accounts") or {}).get(account_pk) or {} if account_pk is not None else {}
    out = {}
    for f in FIELDS:
        if acct.get(f) is not None:
            out[f] = (acct[f], "account")
        elif glob.get(f) is not None:
            out[f] = (glob[f], "all accounts")
        else:
            out[f] = base[f]
    return out


def effective(settings, account_pk=None):
    return {f: v for f, (v, _s) in resolve(settings, account_pk).items()}


def _json_ids(v):
    try:
        out = json.loads(v) if isinstance(v, (str, bytes)) and v else (v or [])
        return sorted({int(x) for x in out}) if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


def _csv(v):
    return [x.strip() for x in str(v).split(",") if x.strip()] if v else []


def _row(r):
    return {"enabled": None if r.get("enabled") is None else bool(r["enabled"]),
            "threshold_pct": None if r.get("threshold_pct") is None else float(r["threshold_pct"]),
            "min_increase_usd": None if r.get("min_increase_usd") is None else float(r["min_increase_usd"]),
            "notify_user_ids": _json_ids(r.get("notify_user_ids")), "notify_emails": _csv(r.get("notify_emails")),
            "updated_by": r.get("updated_by"), "updated_at": r.get("updated_at")}


def load(cursor):
    """{'global': {...}, 'accounts': {pk: {...}}, 'table_missing': bool, 'recipients_missing': bool}.
    A missing table (migration 132 pending) is not an error: the environment and built-in defaults then apply.
    Missing recipient columns (migration 133 pending) only mean nobody extra is notified yet."""
    recipients_missing = False
    try:
        cursor.execute("SELECT scope_key, enabled, threshold_pct, min_increase_usd, notify_user_ids, notify_emails, "
                       "updated_by, updated_at FROM cost_jump_settings")
        rows = cursor.fetchall()
    except Exception as e:
        if "1054" in str(e) or "Unknown column" in str(e):
            recipients_missing = True
            cursor.execute("SELECT scope_key, enabled, threshold_pct, min_increase_usd, updated_by, updated_at FROM cost_jump_settings")
            rows = cursor.fetchall()
        elif "doesn't exist" in str(e) or "1146" in str(e):
            return {"global": {}, "accounts": {}, "table_missing": True, "recipients_missing": True}
        else:
            raise
    out = {"global": {}, "accounts": {}, "table_missing": False, "recipients_missing": recipients_missing}
    for r in rows:
        key = int(r["scope_key"])
        if key == GLOBAL_KEY:
            out["global"] = _row(r)
        else:
            out["accounts"][key] = _row(r)
    return out


def recipients(settings, account_pk):
    """(user_ids, emails) to tell when this account's alert opens: its own plus the all-accounts ones, de-duplicated."""
    glob = (settings or {}).get("global") or {}
    own = ((settings or {}).get("accounts") or {}).get(account_pk) or {}
    users = sorted(set(glob.get("notify_user_ids") or []) | set(own.get("notify_user_ids") or []))
    emails = []
    for a in (glob.get("notify_emails") or []) + (own.get("notify_emails") or []):
        if a.lower() not in emails:
            emails.append(a.lower())
    return users, emails


def _store(field, value):
    if value is None:
        return None
    return json.dumps(value) if field == "notify_user_ids" else (", ".join(value) if field == "notify_emails" else value)


def save(cursor, scope_key, values, actor, recipients_supported=True):
    """Upsert only the fields in `values` (validated; None = back to inherit / nobody). An account row left with every field
    empty is removed. recipients_supported=False (migration 133 not applied): the recipient columns are neither written nor
    tested, and asking to write them is an error the caller must have refused already."""
    every = FIELDS + (RECIPIENT_FIELDS if recipients_supported else ())
    cols = [f for f in every if f in values]
    if any(f in values for f in RECIPIENT_FIELDS) and not recipients_supported:
        raise ValueError("Recipients cannot be saved until migration 133 is applied.")
    if not cols:
        return
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    updates = ", ".join(f"{c} = VALUES({c})" for c in cols)
    cursor.execute(f"INSERT INTO cost_jump_settings (scope_key, {names}, updated_by) VALUES (%s, {marks}, %s) "
                   f"ON DUPLICATE KEY UPDATE {updates}, updated_by = VALUES(updated_by)",
                   (scope_key, *[_store(c, values[c]) for c in cols], str(actor or "")[:100]))
    if scope_key != GLOBAL_KEY:
        empty = " AND ".join(f"{c} IS NULL" for c in every)
        cursor.execute(f"DELETE FROM cost_jump_settings WHERE scope_key = %s AND {empty}", (scope_key,))
