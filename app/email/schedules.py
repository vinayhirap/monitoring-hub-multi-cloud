# app/email/schedules.py
"""
Validation of report-schedule definitions (pure: no database, no clock). The scheduler that runs them is patch 0002.

A schedule says: send THIS report (type, scope) to THIS recipient list at THIS time. The report type is also the
frequency: WEEKLY runs on a weekday, MONTHLY and QUARTERLY on a day of the month (1-28, so every month has it), YEARLY
on a month and day. Times are wall-clock in the schedule's own timezone (default Asia/Kolkata).
"""
from zoneinfo import ZoneInfo

from app.email import recipients

REPORT_TYPES = ("WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY")
SCOPE_TYPES = ("ACCOUNT", "RESOURCE", "CLIENT", "SYNTHETIC", "COST", "HOST", "ENVIRONMENT", "SECURITY", "SLO")
NEEDS_ACCOUNT = ("ACCOUNT", "RESOURCE", "SYNTHETIC", "HOST", "ENVIRONMENT", "SECURITY", "SLO")
DEFAULT_TZ = "Asia/Kolkata"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _int(value, label, lo, hi):
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"{label} is required")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number")
    if not lo <= n <= hi:
        raise ValueError(f"{label} must be between {lo} and {hi}")
    return n


def normalize(payload, current=None):
    """API payload (+ the stored row on update) -> the columns to write. Raises ValueError with a user-safe message.
    Fields that do not apply to the chosen report type are stored as NULL."""
    cur = current or {}
    get = lambda k: payload[k] if k in payload else cur.get(k)

    name = get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not name or len(name) > 120:
        raise ValueError("name is required (max 120 characters)")

    rtype = str(get("report_type") or "").upper()
    if rtype not in REPORT_TYPES:
        raise ValueError(f"report_type must be one of {list(REPORT_TYPES)}")
    stype = str(get("scope_type") or "").upper()
    if stype not in SCOPE_TYPES:
        raise ValueError(f"scope_type must be one of {list(SCOPE_TYPES)}")

    account_id = get("account_id")
    if stype == "COST" or stype == "CLIENT":
        account_id = None
    elif stype in NEEDS_ACCOUNT:
        account_id = _int(account_id, "account_id", 1, 2**62)
    scope_id = str(get("scope_id") if get("scope_id") is not None else "").strip()
    if stype == "ACCOUNT":
        scope_id = str(account_id)
    if not scope_id or len(scope_id) > 191:
        raise ValueError("scope_id is required (max 191 characters)")

    out = {"name": name, "report_type": rtype, "scope_type": stype, "scope_id": scope_id, "account_id": account_id,
           "enabled": 1 if get("enabled") in (None, True, 1, "1", "true", "True") else 0,
           "run_weekday": None, "run_month": None, "run_day": None}
    if rtype == "WEEKLY":
        out["run_weekday"] = _int(get("run_weekday"), "run_weekday (0 = Monday)", 0, 6)
    else:
        out["run_day"] = _int(get("run_day"), "run_day", 1, 28)
        if rtype == "YEARLY":
            out["run_month"] = _int(get("run_month"), "run_month", 1, 12)
    out["run_hour"] = _int(get("run_hour") if get("run_hour") is not None else 8, "run_hour", 0, 23)
    out["run_minute"] = _int(get("run_minute") if get("run_minute") is not None else 0, "run_minute", 0, 59)

    tz = str(get("timezone") or DEFAULT_TZ).strip()
    try:
        ZoneInfo(tz)
    except Exception:
        raise ValueError(f"unknown timezone: {tz[:60]}")
    out["timezone"] = tz

    lid = get("recipient_list_id")
    out["recipient_list_id"] = _int(lid, "recipient_list_id", 1, 2**62) if lid not in (None, "", 0) else None
    extra = recipients.parse_emails(get("extra_emails"))
    out["extra_emails"] = ", ".join(extra) if extra else None
    if out["recipient_list_id"] is None and not extra:
        raise ValueError("choose a recipient list or type at least one email address")
    return out


def describe(row):
    """One human line: 'Monthly on day 1 at 08:00 (Asia/Kolkata)'. Pure, used by the API and the delivery log."""
    t = f"{int(row['run_hour']):02d}:{int(row['run_minute']):02d} ({row.get('timezone') or DEFAULT_TZ})"
    rt = row["report_type"]
    if rt == "WEEKLY":
        return f"Weekly on {WEEKDAYS[int(row['run_weekday'])]} at {t}"
    if rt == "MONTHLY":
        return f"Monthly on day {int(row['run_day'])} at {t}"
    if rt == "QUARTERLY":
        return f"Quarterly on day {int(row['run_day'])} of the first month of each quarter at {t}"
    return f"Yearly on day {int(row['run_day'])} of month {int(row['run_month'])} at {t}"
