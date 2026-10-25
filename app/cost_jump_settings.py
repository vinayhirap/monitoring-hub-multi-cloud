# app/cost_jump_settings.py
"""
Settings for the cost-jump alert (app/collector/cost_jump_alert.py): on/off, percentage above the 7-day average, and the
minimum rise in USD per day. Editable from the Cloud cost page (usage.manage), for all accounts and per account.

Stored in cost_jump_settings (migration 132): scope_key 0 = all accounts, scope_key N = account N. NULL = inherit.
Resolution per field: the account's own row -> the all-accounts row -> the server environment
(COST_JUMP_ALERT_ENABLED, COST_JUMP_THRESHOLD_PCT, COST_JUMP_MIN_INCREASE_USD) -> the built-in default.

Dependency-free on purpose (no FastAPI): the collector and the API both import it. Takes a cursor, never opens one.
"""
import math
import os

GLOBAL_KEY = 0
FIELDS = ("enabled", "threshold_pct", "min_increase_usd")
BUILTIN = {"enabled": False, "threshold_pct": 30.0, "min_increase_usd": 0.10}
LIMITS = {"threshold_pct": (1.0, 1000.0), "min_increase_usd": (0.0, 100000.0)}
_ENV = {"enabled": "COST_JUMP_ALERT_ENABLED", "threshold_pct": "COST_JUMP_THRESHOLD_PCT",
        "min_increase_usd": "COST_JUMP_MIN_INCREASE_USD"}
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
    if not clean:
        raise ValueError("Nothing to change: send at least one of enabled, threshold_pct, min_increase_usd.")
    return clean


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


def _row(r):
    return {"enabled": None if r.get("enabled") is None else bool(r["enabled"]),
            "threshold_pct": None if r.get("threshold_pct") is None else float(r["threshold_pct"]),
            "min_increase_usd": None if r.get("min_increase_usd") is None else float(r["min_increase_usd"]),
            "updated_by": r.get("updated_by"), "updated_at": r.get("updated_at")}


def load(cursor):
    """{'global': {...}, 'accounts': {pk: {...}}, 'table_missing': bool}. A missing table (migration 132 pending) is not an error:
    the environment and built-in defaults then apply, exactly as before this feature."""
    try:
        cursor.execute("SELECT scope_key, enabled, threshold_pct, min_increase_usd, updated_by, updated_at FROM cost_jump_settings")
        rows = cursor.fetchall()
    except Exception as e:
        if "doesn't exist" in str(e) or "1146" in str(e):
            return {"global": {}, "accounts": {}, "table_missing": True}
        raise
    out = {"global": {}, "accounts": {}, "table_missing": False}
    for r in rows:
        key = int(r["scope_key"])
        if key == GLOBAL_KEY:
            out["global"] = _row(r)
        else:
            out["accounts"][key] = _row(r)
    return out


def save(cursor, scope_key, values, actor):
    """Upsert only the fields in `values` (validated; None = back to inherit). An account row left with every field NULL is removed."""
    cols = [f for f in FIELDS if f in values]
    if not cols:
        return
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    updates = ", ".join(f"{c} = VALUES({c})" for c in cols)
    cursor.execute(f"INSERT INTO cost_jump_settings (scope_key, {names}, updated_by) VALUES (%s, {marks}, %s) "
                   f"ON DUPLICATE KEY UPDATE {updates}, updated_by = VALUES(updated_by)",
                   (scope_key, *[values[c] for c in cols], str(actor or "")[:100]))
    if scope_key != GLOBAL_KEY:
        cursor.execute("DELETE FROM cost_jump_settings WHERE scope_key = %s AND enabled IS NULL AND threshold_pct IS NULL "
                       "AND min_increase_usd IS NULL", (scope_key,))
