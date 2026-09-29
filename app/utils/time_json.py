# app/utils/time_json.py
"""
2026-09-29: single source of truth for turning a UTC-intended Python
datetime into a JSON-safe ISO 8601 string that a browser's Date parser
will correctly treat as UTC.

Every datetime this app reads back from MySQL is NAIVE -- MySQL DATETIME
columns carry no timezone info at all -- even though every value this
codebase ever WRITES into one is a UTC wall-clock reading (see
datetime.now(datetime.UTC)/datetime.utcnow() throughout app/collector/,
app/aws/). Calling bare .isoformat() on a naive datetime produces a
string with no trailing 'Z' or '+00:00' offset, e.g. "2026-09-29T11:02:00".
Per the ECMAScript Date-parsing spec, an ISO date-TIME string with no
timezone designator is parsed as the BROWSER'S LOCAL time, not UTC (only
a bare DATE string like "2026-09-29" defaults to UTC -- one of the
best-known footguns in Date parsing). That silently reinterprets a
genuinely-UTC instant as if it were already in the viewer's zone,
shifting every displayed time by the viewer's UTC offset.

Confirmed live on prod 2026-09-29 (CloudOps, U4RAD-JUMP instance
detail): the CPUUtilization chart's topbar correctly showed "IST" and
the real local time was 16:45, but the chart's own x-axis showed its
newest point as "11:02" -- the metric's raw UTC clock reading (11:02
UTC), silently misread by the browser as if it were already 11:02 IST.
The frontend's timezone-conversion code (AccountDetail.jsx's
toLocaleTimeString(..., {timeZone: ianaName})) was working correctly;
the bug was entirely upstream, in the backend sending an unmarked
string in the first place.

The fix this file exists to make impossible to skip: one function,
used everywhere a naive/UTC-intended datetime becomes JSON, instead of
each caller re-deciding (or forgetting) whether to append 'Z'.
app/api/status_page.py's started_at/resolved_at/generated_at fields
already did this correctly by hand (str(dt) + "Z") before this module
existed -- proof the pattern is right, just never centralized or
propagated to the ~13 other call sites that were missing it.
"""
import datetime


def to_utc_iso(dt):
    """dt: datetime.datetime | datetime.date | None -> str | None.

    A NAIVE datetime is assumed to already be a UTC wall-clock reading
    (true for every datetime this app writes to MySQL) and gets an
    explicit 'Z' appended -- never silently left ambiguous.

    A datetime that is ALREADY timezone-aware (e.g. anything boto3
    returns for a CloudWatch/EC2/etc. API timestamp -- those come back
    genuinely tz-aware, already correct, and must NOT be reinterpreted
    as naive-UTC) is converted to UTC and given the same 'Z' form,
    rather than trusted to keep whatever offset it happened to carry --
    JSON consumers here only ever need to compare against other 'Z'
    timestamps, not preserve an arbitrary source offset.

    A bare date.date() has no time-of-day component and therefore no
    timezone ambiguity -- passed through unchanged.
    """
    if dt is None:
        return None
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo is None:
            return dt.isoformat() + "Z"
        return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    return dt.isoformat()
