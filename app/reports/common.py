# app/reports/common.py
"""
Helpers shared by the section reports added after the synthetic report (cost, host agents, environments, security
findings, SLOs). They follow the synthetic report's conventions exactly: the same pdf_kit design, whole UTC days, and a
`scope_id` that is "<account id>" or "<account id>:<item id>".

Nothing here touches the database.
"""
import re
from datetime import datetime, timezone

from app import pdf_kit as kit

_SCOPE_RE = re.compile(r"^(\d{1,18})(?::(\d{1,18}))?$")


def parse_account_scope(scope_id):
    """'12' -> (12, None); '12:34' -> (12, 34); anything else -> ValueError."""
    m = _SCOPE_RE.match(str(scope_id or ""))
    if not m:
        raise ValueError("scope must be '<account id>' or '<account id>:<item id>'")
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)


def naive(dt):
    return dt.replace(tzinfo=None) if isinstance(dt, datetime) and dt.tzinfo else dt


def fmt_stamp(dt):
    return naive(dt).strftime("%d %b %Y %H:%M") if isinstance(dt, datetime) else str(dt or "-")


def fmt_day(dt):
    return naive(dt).strftime("%d %b %Y") if hasattr(dt, "strftime") else str(dt or "-")


def duration(minutes):
    if minutes is None:
        return "-"
    m = int(minutes)
    if m < 60:
        return f"{m} min"
    h, rem = divmod(m, 60)
    if h < 48:
        return f"{h} h {rem:02d} min"
    return f"{h // 24} d {h % 24} h"


def plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


def usd(v, places=2):
    """'$1,234.50'. Small amounts keep four places so a few cents of CloudWatch cost is not shown as $0.00."""
    if v is None:
        return "-"
    v = float(v)
    if 0 < abs(v) < 0.01:
        places = 4
    return f"${v:,.{places}f}"


def pct_text(v, places=1):
    return "-" if v is None else f"{float(v):.{places}f}%"


def age_text(seconds):
    """Seconds -> '5 min', '3 h', '2 d'. For 'last seen' style columns."""
    if seconds is None:
        return "never"
    s = int(seconds)
    if s < 90:
        return f"{max(s, 0)} s"
    if s < 5400:
        return f"{round(s / 60)} min"
    if s < 172800:
        return f"{round(s / 3600)} h"
    return f"{round(s / 86400)} d"


def chip(label, color):
    return ("chip", label, color)


def open_report(*, title, label, report_type, period_start, period_end, generated_by, keywords,
                status=None, kpis=(), detail_rows=(), as_of_only=False):
    """Cover page, title block with chips, key-figures strip and details table: the same opening every CloudOps report
    has. `status` is (text, rgb) for the second chip. `as_of_only` words the period line for reports that describe the
    state at generation time (their period is stored but does not change the figures)."""
    now = datetime.now(timezone.utc)
    pdf = kit.new_document(
        band_subtitle=title, band_right=label, footer_ref=label, title=f"{title} - {label}",
        subject=f"{title}, {fmt_day(period_start)} to {fmt_day(period_end)}", keywords=keywords, cover=True)
    period_line = (f"As of {fmt_stamp(now)} (UTC)" if as_of_only
                   else f"Period: {fmt_stamp(period_start)} to {fmt_stamp(period_end)} (UTC)")
    kit.cover_page(pdf, title=title, subtitle=label,
                   meta_lines=[period_line, f"Generated: {fmt_stamp(now)} by {generated_by}"])
    chips = [((report_type or "REPORT").upper(), kit.TEAL_DARK)]
    if status:
        chips.append(status)
    sub = (f"{label}  ·  as of {fmt_day(now)} (UTC)" if as_of_only
           else f"{label}  ·  {fmt_day(period_start)} to {fmt_day(period_end)} (UTC)")
    kit.title_block(pdf, title, chips, sub)
    if kpis:
        kit.kpi_strip(pdf, list(kpis))
    rows = list(detail_rows)
    rows.append(("As of" if as_of_only else "Period",
                 fmt_stamp(now) if as_of_only else f"{fmt_stamp(period_start)}  to  {fmt_stamp(period_end)}"))
    rows.append(("Generated", f"{fmt_stamp(now)} by {generated_by}"))
    kit.details_table(pdf, rows)
    return pdf, now


def finish(pdf, label, now, generated_by):
    kit.footnote(pdf, f"Generated automatically by AurionPro CloudOps for {label} on {fmt_stamp(now)} (requested by "
                      f"{generated_by}). Verify before external distribution.")
    return bytes(pdf.output())


def note(pdf, text):
    kit.plain_paragraph(pdf, text, size=8.5, color=kit.MUTED, italic=True)
