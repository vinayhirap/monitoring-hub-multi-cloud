# app/email/schedules.py
"""
Validation of report-schedule definitions (pure: no database, no clock). The scheduler that runs them is patch 0002.

A schedule says: send THIS report (type, scope) to THIS recipient list at THIS time. The report type is also the
frequency: WEEKLY runs on a weekday, MONTHLY and QUARTERLY on a day of the month (1-28, so every month has it), YEARLY
on a month and day. Times are wall-clock in the schedule's own timezone (default Asia/Kolkata).
"""
from datetime import date, datetime, time, timedelta, timezone
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


# ── calendar maths (pure: the clock is always passed in) ─────────────
#
# Reports cover the LAST FULL CALENDAR period before the run, in the schedule's own timezone:
#   WEEKLY     the Monday-to-Sunday week before the week of the run
#   MONTHLY    the previous calendar month
#   QUARTERLY  the previous calendar quarter (runs in January, April, July, October)
#   YEARLY     the 12 full months before the run month: run_month = 1 gives the calendar year, run_month = 4 gives April
#              to March (the Indian financial year)
# A run that was missed (service down) is still sent while it is less than CATCHUP_HOURS late, never later: a monthly
# report that arrives three weeks late is worse than none. A schedule never sends for an occurrence before it was created.

CATCHUP_HOURS = 36
_QUARTER_MONTHS = (1, 4, 7, 10)


def _tz(row):
    return ZoneInfo(row.get("timezone") or DEFAULT_TZ)


def _at(tz, d, row):
    return datetime.combine(d, time(int(row["run_hour"]), int(row["run_minute"])), tzinfo=tz)


def _add_months(y, m, delta):
    idx = y * 12 + (m - 1) + delta
    return idx // 12, idx % 12 + 1


def latest_occurrence(row, now_utc):
    """The most recent scheduled run time at or before now, as an aware datetime in the schedule's timezone."""
    tz = _tz(row)
    now = now_utc.astimezone(tz)
    rt = row["report_type"]
    if rt == "WEEKLY":
        d = now.date() - timedelta(days=now.weekday()) + timedelta(days=int(row["run_weekday"]))
        occ = _at(tz, d, row)
        return occ if occ <= now else _at(tz, d - timedelta(days=7), row)
    if rt == "MONTHLY":
        occ = _at(tz, date(now.year, now.month, int(row["run_day"])), row)
        if occ <= now:
            return occ
        y, m = _add_months(now.year, now.month, -1)
        return _at(tz, date(y, m, int(row["run_day"])), row)
    if rt == "QUARTERLY":
        for k in range(0, 13):
            y, m = _add_months(now.year, now.month, -k)
            if m in _QUARTER_MONTHS:
                occ = _at(tz, date(y, m, int(row["run_day"])), row)
                if occ <= now:
                    return occ
        raise ValueError("no quarterly occurrence found")                      # unreachable: a quarter start is <= 3 months back
    occ = _at(tz, date(now.year, int(row["run_month"]), int(row["run_day"])), row)
    return occ if occ <= now else _at(tz, date(now.year - 1, int(row["run_month"]), int(row["run_day"])), row)


def next_occurrence(row, now_utc):
    """The first scheduled run time strictly after now (aware, schedule timezone)."""
    tz, last = _tz(row), latest_occurrence(row, now_utc)
    d, rt = last.date(), row["report_type"]
    if rt == "WEEKLY":
        return _at(tz, d + timedelta(days=7), row)
    if rt == "MONTHLY":
        y, m = _add_months(d.year, d.month, 1)
    elif rt == "QUARTERLY":
        y, m = _add_months(d.year, d.month, 3)
    else:
        y, m = d.year + 1, d.month
    return _at(tz, date(y, m, d.day), row)


def period_for(row, occurrence):
    """Reporting window for a run at `occurrence` -> (start, end) as naive UTC datetimes, ready for report_jobs."""
    tz, rt = occurrence.tzinfo, row["report_type"]
    o = occurrence.date()
    if rt == "WEEKLY":
        end_d = o - timedelta(days=o.weekday())
        start_d = end_d - timedelta(days=7)
    elif rt == "MONTHLY":
        end_d = date(o.year, o.month, 1)
        start_d = date(*_add_months(o.year, o.month, -1), 1)
    elif rt == "QUARTERLY":
        end_d = date(o.year, 3 * ((o.month - 1) // 3) + 1, 1)
        start_d = date(*_add_months(end_d.year, end_d.month, -3), 1)
    else:
        end_d = date(o.year, o.month, 1)
        start_d = date(o.year - 1, o.month, 1)
    to_utc = lambda d: datetime.combine(d, time(0, 0), tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    return to_utc(start_d), to_utc(end_d)


def due(row, now_utc, created_at=None, last_period_end=None, catchup_hours=CATCHUP_HOURS):
    """None, or {"occurrence", "period_start", "period_end"} when this schedule should run now. created_at and
    last_period_end are naive UTC datetimes as stored. A period is only ever sent once (last_period_end)."""
    if not row.get("enabled"):
        return None
    occ = latest_occurrence(row, now_utc)
    occ_utc = occ.astimezone(timezone.utc).replace(tzinfo=None)
    now_naive = now_utc.astimezone(timezone.utc).replace(tzinfo=None)
    if now_naive - occ_utc > timedelta(hours=catchup_hours):
        return None
    if created_at is not None and occ_utc < created_at:
        return None
    start, end = period_for(row, occ)
    if last_period_end is not None and end <= last_period_end:
        return None
    return {"occurrence": occ, "period_start": start, "period_end": end}
