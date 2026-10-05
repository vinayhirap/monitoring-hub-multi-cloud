# app/reports/engine.py
"""
Reusable report engine: same code path serves WEEKLY/MONTHLY/QUARTERLY/
CUSTOM -- those are just (period_start, period_end) computed differently
by the API layer (see app/api/reports.py's _resolve_period). Adding a
new report type later means adding a period-resolution function and a
scope_type branch here, not a new engine.

Data sources (all already-existing tables -- no new collector needed):
  - aws_accounts / resources : account, cloud, region, resource identity
  - alerts                    : severity, status, start/created_at, metric+value
  - incidents / incident_alerts : the correlated incident narrative

Layout: the report is drawn with app/pdf_kit.py, the SAME design kit as the RCA report (header band, key figures,
chips, tables, footer), so every PDF CloudOps produces looks like one product. All charts are native PDF vector
primitives -- no matplotlib/kaleido dependency, matching this app's "avoid heavy/compiled dependencies" convention.
"""
import logging
import re
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from app import pdf_kit as kit
from app.db import get_db_cursor
from app.metric_labels import metric_label, format_metric_value

logger = logging.getLogger(__name__)

# AUDIT FIX (b21/082, MEDIUM): safety-valve row cap for gather_report_data's
# alerts/incidents queries -- see the comment at its alerts query for why.
_MAX_QUERY_ROWS = 50000
_MAX_QUERY_INCIDENTS = 5000


# ── Data gathering ────────────────────────────────────────────────────
#
# Column-name correction (found while adding the incident narrative
# below): the ORIGINAL version of this function queried
# `a.value`/`a.created_at` and joined `resources r ON r.id = a.resource_id`
# -- both wrong against the live schema. db/schema.sql's baseline
# (`value`, `created_at`, resource_id as an int FK) was superseded long
# ago: alerts.current_value / alerts.triggered_at / alerts.resolved_at
# are the real columns (see app/collector/alert_evaluator.py's INSERT,
# app/api/incidents.py's own query), and alerts.resource_id stores the
# STRING cloud resource id (e.g. "i-0abc...").
#
# Cross-account scoping correction (found after
# db/migrations/048_add_account_scoping_to_alerts.sql landed on main,
# same day): a resource_id is only unique WITHIN one account, not
# globally -- two accounts sharing a resource_id could otherwise leak
# one account's alerts into another account's report. This function
# now filters/joins on alerts.aws_account_id directly (added by 048),
# never resources.aws_account_id alone, matching the same fix already
# applied to app/collector/correlate.py and health_score.py in that
# commit. RESOURCE and INCIDENT scoped reports now REQUIRE account_id
# for the same reason (enforced in app/api/reports.py) -- a bare
# resource_id or incident id is not a safe lookup key on its own.

def gather_report_data(scope_type: str, scope_id: str, account_id: int | None,
                        period_start: datetime, period_end: datetime) -> dict:
    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        account = None
        if account_id:
            cur.execute(
                "SELECT id, account_id, account_name, default_region, provider FROM aws_accounts WHERE id=%s",
                (account_id,),
            )
            account = cur.fetchone()

        params = [period_start, period_end]
        where = ["a.triggered_at BETWEEN %s AND %s"]

        if scope_type == "RESOURCE":
            where.append("a.resource_id = %s")
            params.append(scope_id)
        if account_id:
            where.append("a.aws_account_id = %s")
            params.append(account_id)

        # AUDIT FIX (b21/082, MEDIUM): safety-valve cap. Nothing here
        # bounded how many rows this can pull into memory in one go --
        # a scope_type=CLIENT report (no account filter) or a wide
        # CUSTOM period (up to ~400 days, see _resolve_period) has no
        # upper bound otherwise. render_report_pdf already caps what it
        # DRAWS (_MAX_TIMELINE_ROWS etc) but only after this fetches and
        # processes the full result set. _MAX_QUERY_ROWS is set far
        # above any realistic report's true row count deliberately --
        # this is a backstop against a pathological/adversarial case,
        # not a change to normal report content or the render layer's
        # own significance-based truncation (which still operates on
        # whatever this returns, most-severe-first).
        sql = f"""
            SELECT a.id, a.metric_name, a.current_value AS value, a.threshold,
                   a.severity, a.status, a.triggered_at, a.resolved_at,
                   r.resource_type, r.resource_id, r.name AS resource_name, r.region
            FROM alerts a
            JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
            WHERE {' AND '.join(where)}
            ORDER BY a.triggered_at ASC
            LIMIT {_MAX_QUERY_ROWS}
        """
        cur.execute(sql, params)
        alerts = cur.fetchall()

        # Real correlated incidents (app/collector/correlate.py +
        # app/api/incidents.py) overlapping the period -- these give
        # the actual "incident" narrative: title, probable cause,
        # start/end, member alerts/resources. A scope_type=INCIDENT
        # request always has account_id set (enforced in
        # app/api/reports.py) since incidents are looked up
        # (id, account_id) just like the Incidents page does.
        incidents = []
        if scope_type != "RESOURCE":  # a single-resource report has no incident grouping to show
            inc_where = ["i.started_at <= %s", "(i.resolved_at IS NULL OR i.resolved_at >= %s)"]
            inc_params = [period_end, period_start]
            if scope_type == "INCIDENT":
                inc_where.append("i.id = %s")
                inc_params.append(scope_id)
            if account_id:
                inc_where.append("i.aws_account_id = %s")
                inc_params.append(account_id)
            cur.execute(
                f"""SELECT i.id, i.title, i.severity, i.status, i.primary_resource_id,
                           i.probable_cause, i.started_at, i.resolved_at, i.last_seen_at
                    FROM incidents i WHERE {' AND '.join(inc_where)}
                    ORDER BY i.started_at ASC
                    LIMIT {_MAX_QUERY_INCIDENTS}""",
                inc_params,
            )
            incidents = cur.fetchall()
            # AUDIT FIX (b21/082, MEDIUM): this used to run one
            # incident_alerts query PER incident (N+1) -- for a
            # scope_type=CLIENT report spanning every account, or any
            # account with a large incident history over a long custom
            # period, that's one query per incident even though the PDF
            # only ever displays _MAX_INCIDENT_CARDS of them (the
            # truncation happens later, at render time, in
            # render_report_pdf). One batched IN(...) query + grouping
            # in Python instead.
            if incidents:
                inc_ids = [inc["id"] for inc in incidents]
                placeholders = ",".join(["%s"] * len(inc_ids))
                cur.execute(
                    f"""SELECT ia.incident_id, a.id, a.resource_id, a.metric_name, a.current_value AS value,
                               a.severity, a.status, a.triggered_at, a.resolved_at,
                               r.resource_type, r.name AS resource_name, r.region
                        FROM incident_alerts ia
                        JOIN alerts a ON a.id = ia.alert_id
                        LEFT JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
                        WHERE ia.incident_id IN ({placeholders})
                        ORDER BY ia.incident_id, a.triggered_at ASC""",
                    inc_ids,
                )
                member_alerts_by_incident = {}
                for row in cur.fetchall():
                    member_alerts_by_incident.setdefault(row["incident_id"], []).append(row)
                for inc in incidents:
                    inc["member_alerts"] = member_alerts_by_incident.get(inc["id"], [])

        affected_resources = {}
        for a in alerts:
            affected_resources.setdefault(a["resource_id"], {
                "resource_id": a["resource_id"],
                "resource_type": a["resource_type"],
                "name": a["resource_name"],
                "region": a.get("region"),
            })

    severity_counts = {"CRITICAL": 0, "WARNING": 0, "OTHER": 0}
    for a in alerts:
        sev = (a.get("severity") or "OTHER").upper()
        severity_counts[sev if sev in ("CRITICAL", "WARNING") else "OTHER"] += 1

    open_count = sum(1 for a in alerts if (a.get("status") or "").lower() not in ("resolved", "closed"))

    # Daily event trend, zero-filled across the whole period so the
    # chart shows genuinely quiet days rather than skipping them.
    daily_counts = OrderedDict()
    cursor_day = period_start.date()
    while cursor_day <= period_end.date():
        daily_counts[cursor_day] = 0
        cursor_day += timedelta(days=1)
    for a in alerts:
        d = a["triggered_at"].date()
        if d in daily_counts:
            daily_counts[d] += 1

    return {
        "account": account,
        "alerts": alerts,
        "incidents": incidents,
        "affected_resources": list(affected_resources.values()),
        "severity_counts": severity_counts,
        "open_count": open_count,
        "total_count": len(alerts),
        "daily_counts": daily_counts,
    }


# ── Content builders (pure: no DB, no PDF) ────────────────────────────
#
# Everything the report SAYS is decided here, so it can be unit-tested. The first real weekly report (U4RAD, 26 Sep to
# 03 Oct) had: raw metric keys ("httpcode_target_4xx_count"), full ARNs inside sentences, "resource(s)", "--" dashes,
# 2,429 rows of alert log, 40 near-identical "affected resource" lines, and justified text that stretched across the page
# around those long ARNs. A reader got no answer to "what happened and what do I need to look at?"

_TYPE_TITLES = {
    "WEEKLY": "Weekly Operations Report", "MONTHLY": "Monthly Operations Review",
    "QUARTERLY": "Quarterly Operations Review", "CUSTOM": "Operations Report",
}
_MAX_INCIDENT_CARDS = 10
_MAX_TIMELINE_ROWS = 60
_MAX_ROWS_PER_SOURCE = 5          # one noisy source (60 near-identical "Target 4xx Errors" rows) must not fill the whole log
_TOP_N = 8

_ARN_RE = re.compile(r"arn:aws[\w-]*:[^\s,)\]]+")
_BREACH_RE = re.compile(
    r"Earliest breach in this incident: (?P<metric>\S+) on (?P<res>\S+) at "
    r"(?P<d>\d{4}-\d{2}-\d{2}) (?P<t>\d{2}:\d{2})(?::\d{2})?\.?")
_DEPENDS_RE = re.compile(r"(\d+) other resource\(s\) depend on it in the topology graph\.?")


def report_title(report_type: str, scope_type: str) -> str:
    st = (scope_type or "").upper()
    if st == "INCIDENT":
        return "Incident Report"
    if st == "RESOURCE":
        return "Resource Report"
    base = _TYPE_TITLES.get((report_type or "").upper(), "Operations Report")
    return base.replace("Operations Report", "Client Report").replace("Operations Review", "Client Review") \
        if st == "CLIENT" else base


def short_resource(text) -> str:
    """'arn:aws:elasticloadbalancing:ap-south-1:1234:loadbalancer/app/u4rad-alb/7825df' -> 'u4rad-alb'. Plain ids and
    names pass through unchanged."""
    s = "" if text is None else str(text)
    if not s.startswith("arn:"):
        return s
    tail = s.split(":", 5)[-1] if s.count(":") >= 5 else s
    parts = [p for p in re.split(r"[/:]", tail) if p]          # ARN resource parts use '/' OR ':' ("function:Name")
    if "loadbalancer" in parts:                      # loadbalancer/<app|net|gwy>/<name>/<hash>
        i = parts.index("loadbalancer")
        return parts[i + 2] if len(parts) > i + 2 else parts[-1]
    return parts[-1] if parts else s


def _fmt_day(dt) -> str:
    return dt.strftime("%d %b %Y") if dt else "-"


def _fmt_stamp(dt) -> str:
    return dt.strftime("%d %b %Y, %H:%M UTC") if dt else "-"


def _fmt_short(dt) -> str:
    return dt.strftime("%d %b, %H:%M") if dt else "-"


def _duration_text(delta) -> str:
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{max(1, round(hours * 60))} min"
    if hours < 48:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


def _naive(dt):
    return dt.replace(tzinfo=None) if dt is not None and getattr(dt, "tzinfo", None) else dt


def _is_open(status) -> bool:
    return (status or "").lower() not in ("resolved", "closed")


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


def _name_of(a: dict) -> str:
    return a.get("resource_name") or short_resource(a.get("resource_id"))


def summarize(data: dict) -> dict:
    """Rankings and headline facts used by the key figures, the summary text and the tables."""
    alerts = data.get("alerts") or []
    total = len(alerts)
    by_res, by_metric = {}, {}
    for a in alerts:
        sev = (a.get("severity") or "").upper()
        r = by_res.setdefault(a["resource_id"], {"name": _name_of(a), "type": a.get("resource_type") or "",
                                                 "total": 0, "critical": 0, "warning": 0, "open": 0})
        m = by_metric.setdefault((a.get("metric_name") or "").lower(),
                                 {"label": metric_label(a.get("metric_name")), "total": 0, "critical": 0, "warning": 0, "open": 0})
        for bucket in (r, m):
            bucket["total"] += 1
            bucket["critical"] += sev == "CRITICAL"
            bucket["warning"] += sev == "WARNING"
            bucket["open"] += _is_open(a.get("status"))
    # "Most affected" means most alerts. (It used to sort still-open first, so the table read 27, 21, 117, 143, 204 and the
    # summary named the 27- and 21-alert resources "most affected" ahead of one with 204.) Open ones have their own column
    # and their own "Still open" line.
    top_resources = sorted(by_res.values(), key=lambda x: (-x["total"], -x["critical"], -x["open"], x["name"]))[:_TOP_N]
    top_metrics = sorted(by_metric.values(), key=lambda x: (-x["total"], -x["critical"]))[:_TOP_N]
    daily = data.get("daily_counts") or {}
    busiest = max(daily.items(), key=lambda kv: kv[1]) if daily and max(daily.values()) > 0 else None
    dominant = None
    if total >= 20 and top_metrics and top_metrics[0]["total"] / total >= 0.5:
        dominant = {"label": top_metrics[0]["label"], "share": round(100 * top_metrics[0]["total"] / total)}
    open_alerts = sorted((a for a in alerts if _is_open(a.get("status"))),
                         key=lambda a: ({"CRITICAL": 0, "WARNING": 1}.get((a.get("severity") or "").upper(), 2),
                                        a["triggered_at"]))
    return {"total": total, "resources": len(by_res), "top_resources": top_resources, "top_metrics": top_metrics,
            "busiest": busiest, "dominant": dominant, "open_alerts": open_alerts}


def select_significant(alerts, limit=None, per_source=None):
    """The alerts worth showing, most significant first (open, then severity, then newest), taking at most `per_source` per
    (resource, metric) on the first pass so the log shows the BREADTH of what happened; remaining slots are then filled from
    what was skipped. Returned in time order."""
    limit = limit or _MAX_TIMELINE_ROWS
    per_source = per_source or _MAX_ROWS_PER_SOURCE
    ranked = sorted(alerts, key=lambda a: (0 if _is_open(a.get("status")) else 1,
                                           {"CRITICAL": 0, "WARNING": 1}.get((a.get("severity") or "").upper(), 2),
                                           -a["triggered_at"].timestamp()))
    # Round-robin in waves: the first `per_source` alerts of EVERY source, then the next `per_source` of every source, and so on,
    # keeping significance order inside a wave. (Topping up with "whatever was skipped" let the newest source fill the log again.)
    counts, waves = {}, []
    for idx, a in enumerate(ranked):
        key = (a.get("resource_id"), (a.get("metric_name") or "").lower())
        n = counts.get(key, 0)
        counts[key] = n + 1
        waves.append((n // per_source, idx, a))
    waves.sort(key=lambda t: (t[0], t[1]))
    chosen = [a for _, _, a in waves[:limit]]
    return sorted(chosen, key=lambda a: a["triggered_at"])


def summary_paragraphs(data: dict, summ: dict, period_start, period_end) -> list:
    """[(lead, text)] for the Executive Summary. Every sentence is built from counted facts; nothing is inferred."""
    sc = data["severity_counts"]
    n_inc = len(data.get("incidents") or [])
    if summ["total"] == 0:
        return [("Overview", f"No alerts were raised between {_fmt_day(period_start)} and {_fmt_day(period_end)}.")]
    out = [("Overview",
            f"{summ['total']:,} {_plural(summ['total'], 'alert')} {_plural(summ['total'], 'was', 'were')} raised on "
            f"{summ['resources']:,} {_plural(summ['resources'], 'resource')} between {_fmt_day(period_start)} and "
            f"{_fmt_day(period_end)}, grouped into {n_inc:,} {_plural(n_inc, 'incident')}. "
            f"{sc['CRITICAL']:,} {_plural(sc['CRITICAL'], 'was', 'were')} critical and {sc['WARNING']:,} "
            f"{_plural(sc['WARNING'], 'was', 'were')} warnings. "
            + (f"{data['open_count']:,} {_plural(data['open_count'], 'remains', 'remain')} open."
               if data["open_count"] else "All have since been resolved."))]
    if summ["busiest"]:
        day, count = summ["busiest"]
        out.append(("Busiest day", f"{day.strftime('%d %b')} had the most alerts ({count:,})."))
    if summ["top_resources"]:
        shown = summ["top_resources"][:3]
        out.append(("Most affected", ", ".join(f"{r['name']} ({r['total']:,})" for r in shown) + "."))
    if summ["dominant"]:
        out.append(("Alert sources",
                    f"{summ['dominant']['share']}% of all alerts came from one metric, {summ['dominant']['label']}. "
                    f"If that level is normal for the workload, raise its limit or let auto-tuning learn it; "
                    f"otherwise find what is driving it."))
    if summ["open_alerts"]:
        names = [f"{_name_of(a)} ({metric_label(a.get('metric_name'))})" for a in summ["open_alerts"][:3]]
        more = len(summ["open_alerts"]) - len(names)
        out.append(("Still open", ", ".join(names) + (f" and {more} more." if more > 0 else ".")))
    return out


def _names_by_id(inc: dict) -> dict:
    out = {}
    for m in inc.get("member_alerts") or []:
        rid = m.get("resource_id")
        if rid:
            out[rid] = m.get("resource_name") or short_resource(rid)
    return out


def humanize_incident_title(title, inc: dict) -> str:
    """'Correlated breach on vol-0952... and related resource(s)' -> a title with the resource's name and no '(s)'."""
    names = _names_by_id(inc)
    t = (title or "Untitled incident").replace("resource(s)", "resources")
    t = _ARN_RE.sub(lambda m: short_resource(m.group(0)), t)
    for rid, name in sorted(names.items(), key=lambda kv: -len(kv[0])):
        t = t.replace(rid, name)
    return t


def humanize_cause(text, inc: dict) -> str:
    """Rewrites the stored probable-cause sentence for a reader: metric labels, resource names, short times."""
    if not text:
        return ""
    names = _names_by_id(inc)

    def breach(m):
        res = m.group("res")
        name = names.get(res) or short_resource(res)
        when = datetime.strptime(f"{m.group('d')} {m.group('t')}", "%Y-%m-%d %H:%M")
        return f"Started with {metric_label(m.group('metric'))} on {name} at {_fmt_stamp(when)}."
    out = _BREACH_RE.sub(breach, str(text))
    out = _DEPENDS_RE.sub(lambda m: f"{m.group(1)} other {'resource depends' if m.group(1) == '1' else 'resources depend'} on it.", out)
    out = _ARN_RE.sub(lambda m: short_resource(m.group(0)), out)
    return out.replace("resource(s)", "resources")


# ── Layout ────────────────────────────────────────────────────────────

def _incident_card(pdf, inc: dict):
    """One incident: severity bar, wrapped title, chips, a metadata line, then Impact and Status in plain left-aligned text."""
    started, resolved = _naive(inc["started_at"]), _naive(inc.get("resolved_at"))
    is_resolved = not _is_open(inc.get("status"))
    duration = (resolved or datetime.now(timezone.utc).replace(tzinfo=None)) - started
    dur = _duration_text(duration)
    sev = (inc.get("severity") or "").upper()
    members = inc.get("member_alerts") or []
    names = sorted({m.get("resource_name") or short_resource(m.get("resource_id")) for m in members})
    regions = sorted({m.get("region") for m in members if m.get("region")})
    title = f"Incident #{inc['id']}: {humanize_incident_title(inc.get('title'), inc)}"
    cause = humanize_cause(inc.get("probable_cause"), inc)
    when = f"Started {_fmt_stamp(started)}"
    when += f"  |  Resolved {_fmt_stamp(resolved)}" if (is_resolved and resolved) else ("" if is_resolved else "  |  Still open")
    when += f"  |  Duration {dur}"
    res_line = f"Resources ({len(names)}): " + (", ".join(names) or "n/a") + (f"  |  {', '.join(regions)}" if regions else "")
    last = inc.get("last_seen_at")
    if is_resolved:
        status = f"Resolved after {dur}; all correlated metrics returned within their limits." + \
                 (f" Last confirmed healthy {_fmt_stamp(_naive(last))}." if last else "")
    else:
        status = f"Still active after {dur}, tracked live in CloudOps." + (f" Last activity {_fmt_stamp(_naive(last))}." if last else "")

    inner_w = pdf.w - 2 * kit.MARGIN - 9
    chips_w = 52
    pdf.set_font("Helvetica", "B", 10)
    t_lines = pdf.multi_cell(inner_w - chips_w, 5.4, kit.latin1_safe(title), dry_run=True, output="LINES")
    pdf.set_font("Helvetica", "", 8.5)
    w_lines = len(pdf.multi_cell(inner_w, 4.8, kit.latin1_safe(when), dry_run=True, output="LINES"))
    r_lines = len(pdf.multi_cell(inner_w, 4.8, kit.latin1_safe(res_line), dry_run=True, output="LINES"))
    pdf.set_font("Helvetica", "", 9)
    c_lines = len(pdf.multi_cell(inner_w, 5, kit.latin1_safe("Impact: " + cause), dry_run=True, output="LINES")) if cause else 0
    s_lines = len(pdf.multi_cell(inner_w, 5, kit.latin1_safe("Status: " + status), dry_run=True, output="LINES"))
    h = 3 + len(t_lines) * 5.4 + 1.5 + (w_lines + r_lines) * 4.8 + 1.5 + (c_lines + s_lines) * 5 + 3
    kit.ensure_room(pdf, h + 4)
    x, y = kit.MARGIN, pdf.get_y()
    pdf.set_fill_color(252, 252, 254)
    pdf.set_draw_color(*kit.BORDER)
    pdf.rect(x, y, pdf.w - 2 * kit.MARGIN, h, "DF")
    pdf.set_fill_color(*kit.SEVERITY_COLORS.get(sev, kit.MUTED))
    pdf.rect(x, y, 1.6, h, "F")

    right = pdf.w - kit.MARGIN - 3
    st_text = "RESOLVED" if is_resolved else "ACTIVE"
    pdf.set_font("Helvetica", "B", 7.5)
    st_w, sv_w = 8 + pdf.get_string_width(st_text), 8 + pdf.get_string_width(sev or "-")
    kit.chip(pdf, right - st_w, y + 2.6, st_text, kit.STATUS_COLORS["RESOLVED" if is_resolved else "OPEN"], st_w)
    kit.chip(pdf, right - st_w - 3 - sv_w, y + 2.6, sev or "-", kit.SEVERITY_COLORS.get(sev, kit.MUTED), sv_w)

    pdf.set_xy(x + 5, y + 3)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*kit.INK)
    pdf.multi_cell(inner_w - chips_w, 5.4, kit.latin1_safe(title), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*kit.MUTED)
    pdf.set_xy(x + 5, pdf.get_y() + 1.5)
    pdf.multi_cell(inner_w, 4.8, kit.latin1_safe(when), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.set_x(x + 5)
    pdf.multi_cell(inner_w, 4.8, kit.latin1_safe(res_line), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.set_text_color(*kit.INK)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_xy(x + 5, pdf.get_y() + 1.5)
    if cause:
        pdf.multi_cell(inner_w, 5, kit.latin1_safe("Impact: " + cause), new_x="LMARGIN", new_y="NEXT", align="L")
        pdf.set_x(x + 5)
    pdf.multi_cell(inner_w, 5, kit.latin1_safe("Status: " + status), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.set_xy(kit.MARGIN, y + h + 3)


def _incident_sort_key(inc: dict):
    """Highest priority first: still-open beats resolved, CRITICAL beats WARNING, then most recent."""
    sev_rank = {"CRITICAL": 2, "WARNING": 1}.get((inc.get("severity") or "").upper(), 0)
    started = inc.get("started_at")
    return (1 if _is_open(inc.get("status")) else 0, sev_rank, started.timestamp() if started else 0)


def render_report_pdf(*, report_type: str, scope_type: str, scope_id: str,
                       scope_label: str, period_start: datetime, period_end: datetime,
                       data: dict, generated_by: str) -> bytes:
    label = scope_label or scope_id
    title = report_title(report_type, scope_type)
    now = datetime.now(timezone.utc)
    summ = summarize(data)
    sc = data["severity_counts"]
    incidents = data.get("incidents") or []
    pdf = kit.new_document(
        band_subtitle=title, band_right=label, footer_ref=label,
        title=f"{title} - {label}", subject=f"{title}, {_fmt_day(period_start)} to {_fmt_day(period_end)}",
        keywords="CloudOps, monitoring report", cover=True)
    kit.cover_page(
        pdf, title=title, subtitle=f"{(scope_type or '').title()}: {label}",
        meta_lines=[f"Period: {_fmt_stamp(period_start)} to {_fmt_stamp(period_end)}",
                    f"Generated: {_fmt_stamp(now)} by {generated_by}"])

    open_n = data["open_count"]
    chips = [((report_type or "REPORT").upper(), kit.TEAL_DARK),
             ((f"{open_n:,} OPEN" if open_n else "ALL RESOLVED"), kit.STATUS_COLORS["OPEN" if open_n else "RESOLVED"])]
    kit.title_block(pdf, title, chips, f"{label}  \u00b7  {_fmt_day(period_start)} to {_fmt_day(period_end)} (UTC)")
    kit.kpi_strip(pdf, [
        {"label": "ALERTS RAISED", "value": f"{summ['total']:,}", "note": f"on {summ['resources']:,} resources", "tone": "ink"},
        {"label": "CRITICAL", "value": f"{sc['CRITICAL']:,}", "note": "highest severity", "tone": "crit"},
        {"label": "WARNING", "value": f"{sc['WARNING']:,}", "note": "needs attention", "tone": "warn"},
        {"label": "INCIDENTS", "value": f"{len(incidents):,}", "note": "correlated groups", "tone": "ink"},
        {"label": "OPEN NOW", "value": f"{open_n:,}", "note": "still firing" if open_n else "none", "tone": "crit" if open_n else "ok"},
    ])

    account = data.get("account")
    rows = []
    if account:
        rows += [("Account", f"{account['account_name']} ({account['account_id']})"),
                 ("Provider", (account.get("provider") or "aws").upper()),
                 ("Default region", account.get("default_region") or "n/a")]
    else:
        rows.append(("Scope", f"{(scope_type or '').title()}: {label}"))
    rows += [("Period", f"{_fmt_stamp(period_start)}  to  {_fmt_stamp(period_end)}"),
             ("Generated", f"{_fmt_stamp(now)} by {generated_by}")]
    kit.details_table(pdf, rows)

    kit.section_header(pdf, "Executive Summary")
    for lead, text in summary_paragraphs(data, summ, period_start, period_end):
        kit.paragraph(pdf, lead, text)

    if summ["total"]:
        kit.section_header(pdf, "Alerts per Day")
        daily = data["daily_counts"]
        kit.bar_chart(pdf, [d.strftime("%d %b") for d in daily], list(daily.values()),
                      caption="Number of alerts that started on each day (UTC).")

        kit.section_header(pdf, "Most Affected Resources")
        kit.data_table(pdf, [("Resource", 64, "L"), ("Type", 30, "L"), ("Alerts", 22, "R"), ("Critical", 22, "R"),
                             ("Warning", 22, "R"), ("Open", 20, "R")],
                       [(r["name"], r["type"].upper() or "-", f"{r['total']:,}", f"{r['critical']:,}",
                         f"{r['warning']:,}", f"{r['open']:,}") for r in summ["top_resources"]])
        if summ["resources"] > len(summ["top_resources"]):
            kit.plain_paragraph(pdf, f"{summ['resources'] - len(summ['top_resources']):,} more resources had alerts in this "
                                     f"period; the full list is in CloudOps.", size=8.5, color=kit.MUTED, italic=True)

        kit.section_header(pdf, "Top Alert Sources")
        kit.data_table(pdf, [("Metric", 74, "L"), ("Alerts", 24, "R"), ("Share", 22, "R"), ("Critical", 20, "R"),
                             ("Warning", 20, "R"), ("Open", 20, "R")],
                       [(m["label"], f"{m['total']:,}", f"{round(100 * m['total'] / summ['total'])}%", f"{m['critical']:,}",
                         f"{m['warning']:,}", f"{m['open']:,}") for m in summ["top_metrics"]])

    kit.section_header(pdf, f"Incidents ({len(incidents):,} in this period)")
    if incidents:
        kit.plain_paragraph(pdf, "Each incident groups the correlated alerts CloudOps identified as one connected event.",
                            size=9.5, color=kit.MUTED)
        shown = sorted(incidents, key=_incident_sort_key, reverse=True)[:_MAX_INCIDENT_CARDS]
        for inc in shown:
            _incident_card(pdf, inc)
        if len(incidents) > len(shown):
            kit.plain_paragraph(pdf, f"{len(incidents) - len(shown):,} more {_plural(len(incidents) - len(shown), 'incident')} "
                                     f"occurred in this period. Showing the {len(shown)} most significant: still-open first, "
                                     f"then highest severity. The full history is in CloudOps (Reports, Incident scope).",
                                size=8.5, color=kit.MUTED, italic=True)
    else:
        kit.plain_paragraph(pdf, "No correlated incidents were identified in this period.")

    alerts = data.get("alerts") or []
    kit.section_header(pdf, "Most Significant Alerts" if len(alerts) > _MAX_TIMELINE_ROWS else "Alert Log")
    if alerts:
        pool = select_significant(alerts) if len(alerts) > _MAX_TIMELINE_ROWS else sorted(alerts, key=lambda a: a["triggered_at"])
        kit.data_table(pdf, [("Time (UTC)", 26, "L"), ("Severity", 22, "C"), ("Status", 22, "C"), ("Resource", 54, "L"),
                             ("Metric", 34, "L"), ("Value", 22, "R")],
                       [(_fmt_short(_naive(a["triggered_at"])),
                         ("chip", (a.get("severity") or "-").upper(), kit.SEVERITY_COLORS.get((a.get("severity") or "").upper(), kit.MUTED)),
                         ("chip", "OPEN" if _is_open(a.get("status")) else "RESOLVED",
                          kit.STATUS_COLORS["OPEN" if _is_open(a.get("status")) else "RESOLVED"]),
                         _name_of(a), metric_label(a.get("metric_name")),
                         format_metric_value(a.get("metric_name"), a.get("value"), grouped=True)) for a in pool])
        if len(pool) < len(alerts):
            kit.plain_paragraph(pdf, f"Showing {len(pool)} of {len(alerts):,} alerts: still-open and highest severity first, at most "
                                     f"{_MAX_ROWS_PER_SOURCE} per resource and metric so one noisy source does not crowd out the rest. "
                                     f"The full alert history is in CloudOps or available through the API.",
                                size=8.5, color=kit.MUTED, italic=True)
    else:
        kit.plain_paragraph(pdf, "No alerts were recorded in this period. A clean run.")

    kit.section_header(pdf, "Resolution and Current Status")
    resolved_n = data["total_count"] - open_n
    pct = ""
    if data["total_count"]:
        # Floor while anything is still open: 2,423 of 2,429 is not "100%".
        share = 100 if not open_n else min(99, (100 * resolved_n) // data["total_count"])
        pct = f" ({share}%)"
    kit.bullet(pdf, f"{resolved_n:,} of {data['total_count']:,} alerts were resolved by the time this report was generated{pct}.")
    if open_n:
        kit.bullet(pdf, f"{open_n:,} {_plural(open_n, 'alert remains', 'alerts remain')} open and "
                        f"{_plural(open_n, 'is', 'are')} being tracked live in CloudOps:")
        for a in summ["open_alerts"][:8]:
            kit.bullet(pdf, f"{_name_of(a)}: {metric_label(a.get('metric_name'))}, "
                            f"{format_metric_value(a.get('metric_name'), a.get('value'), grouped=True)} "
                            f"({(a.get('severity') or '').title()}, since {_fmt_short(_naive(a['triggered_at']))} UTC)")
    else:
        kit.bullet(pdf, "Nothing remains open.")

    kit.footnote(pdf, f"Generated automatically by AurionPro CloudOps for {label} on {_fmt_stamp(now)} (requested by "
                      f"{generated_by}). Verify before external distribution.")
    return bytes(pdf.output())
