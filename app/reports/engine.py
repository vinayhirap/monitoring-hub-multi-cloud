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
  - metrics                   : last-value snapshot for a quick current-state table
  - metric_history             : trend lines (avg/min/max per day in range)

Incident-timeline / RCA narrative (app/collector/correlate.py,
app/collector/rca.py) is intentionally NOT re-implemented here; a
follow-up can feed correlate.py's output into this same
`_gather_incident_timeline` seam once that data has a stable
account-independent lookup. For now, alerts ARE the incident record --
every alert row already carries severity/status/start-time/resource,
which covers this phase's report content requirement.

PDF design: branded to match this app's own design tokens
(frontend/src/index.css's :root palette -- --accent #2bb3ac,
--accent-red/-yellow/-green, --bg-base navy) rather than an unthemed
default, and uses the existing aslops_logo.png (repo root) as the
cover-page / header mark. All charts are drawn as native PDF vector
primitives (fpdf2 rect/line calls) -- no matplotlib/kaleido dependency
added, matching this app's stated "avoid heavy/compiled dependencies
where a light one does the job" convention (see requirements.txt's
statsmodels/fpdf2 comments). Pillow (for the logo PNG) is already a
transitive dependency of fpdf2 -- nothing new to install.
"""
import logging
import os
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone

from fpdf import FPDF

from app.db import get_db_cursor

logger = logging.getLogger(__name__)

# ── Brand palette (mirrors frontend/src/index.css :root tokens) ───────
_NAVY        = (6, 11, 20)      # --bg-base
_NAVY_CARD   = (14, 24, 41)     # --bg-card
_TEAL        = (43, 179, 172)   # --accent
_TEAL_DIM    = (223, 242, 241)  # light tint of --accent for row banding
_RED         = (239, 68, 68)    # --accent-red
_YELLOW      = (245, 158, 11)   # --accent-yellow
_GREEN       = (34, 197, 94)    # --accent-green
_GRAY_TEXT   = (74, 95, 128)    # --text-muted
_GRAY_LINE   = (222, 227, 235)
_WHITE       = (255, 255, 255)
_INK         = (20, 26, 38)     # near-black body text, better print contrast than pure black

_LOGO_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "aslops_logo.png")

_UNICODE_REPLACEMENTS = {
    "\u2022": "-", "\u2014": "--", "\u2013": "-",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2026": "...",
}


def _safe(text) -> str:
    text = "" if text is None else str(text)
    for uni, ascii_equiv in _UNICODE_REPLACEMENTS.items():
        text = text.replace(uni, ascii_equiv)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _severity_color(sev: str):
    sev = (sev or "").upper()
    if sev == "CRITICAL":
        return _RED
    if sev == "WARNING":
        return _YELLOW
    return _GRAY_TEXT


def _status_color(status: str):
    s = (status or "").lower()
    if s in ("resolved", "closed"):
        return _GREEN
    return _RED


# ── Data gathering ────────────────────────────────────────────────────

def gather_report_data(scope_type: str, scope_id: str, account_id: int | None,
                        period_start: datetime, period_end: datetime) -> dict:
    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        account = None
        if account_id:
            cur.execute(
                "SELECT id, account_id, name, region_default FROM aws_accounts WHERE id=%s",
                (account_id,),
            )
            account = cur.fetchone()

        params = [period_start, period_end]
        where = ["a.created_at BETWEEN %s AND %s"]

        if scope_type == "RESOURCE":
            where.append("r.resource_id = %s")
            params.append(scope_id)
        elif scope_type == "INCIDENT":
            # This app's alert id doubles as the correlated-incident
            # anchor for report purposes; scope_id is that alert id.
            where.append("a.id = %s")
            params.append(scope_id)
        if account_id:
            where.append("r.aws_account_id = %s")
            params.append(account_id)

        sql = f"""
            SELECT a.id, a.metric_name, a.value, a.severity, a.status, a.created_at,
                   r.resource_type, r.resource_id, r.name AS resource_name
            FROM alerts a
            JOIN resources r ON r.id = a.resource_id
            WHERE {' AND '.join(where)}
            ORDER BY a.created_at ASC
        """
        cur.execute(sql, params)
        alerts = cur.fetchall()

        affected_resources = {}
        for a in alerts:
            affected_resources.setdefault(a["resource_id"], {
                "resource_id": a["resource_id"],
                "resource_type": a["resource_type"],
                "name": a["resource_name"],
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
        d = a["created_at"].date()
        if d in daily_counts:
            daily_counts[d] += 1

    return {
        "account": account,
        "alerts": alerts,
        "affected_resources": list(affected_resources.values()),
        "severity_counts": severity_counts,
        "open_count": open_count,
        "total_count": len(alerts),
        "daily_counts": daily_counts,
    }


# ── PDF rendering ─────────────────────────────────────────────────────

class ReportPDF(FPDF):
    """Branded report shell: every page after the cover gets a slim
    navy header band (logo + report title) and a footer (page number,
    confidentiality line, generation timestamp) -- fpdf2 calls
    header()/footer() automatically on every add_page()."""

    def __init__(self, meta: dict, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._meta = meta
        self.set_auto_page_break(auto=True, margin=22)

    def header(self):
        if self.page_no() == 1:
            return  # cover page draws its own full-bleed design
        self.set_fill_color(*_NAVY)
        self.rect(0, 0, self.w, 16, style="F")
        if os.path.exists(_LOGO_PATH):
            try:
                self.image(_LOGO_PATH, x=10, y=3, h=10)
            except Exception:
                pass
        self.set_xy(0, 5)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*_WHITE)
        self.cell(0, 6, _safe(self._meta["title"]), align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_y(20)
        self.set_text_color(*_INK)

    def footer(self):
        if self.page_no() == 1:
            return
        self.set_y(-16)
        self.set_draw_color(*_GRAY_LINE)
        self.line(10, self.get_y(), self.w - 10, self.get_y())
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*_GRAY_TEXT)
        self.set_y(-13)
        self.cell(0, 8, _safe("CONFIDENTIAL -- prepared by CloudOps Monitoring for the named client/account only"))
        self.set_y(-13)
        self.cell(0, 8, _safe(f"Page {self.page_no()}"), align="R")

    # ── layout helpers ──────────────────────────────────────────────
    def section_title(self, text: str):
        self.ln(3)
        self.set_fill_color(*_TEAL)
        self.set_text_color(*_WHITE)
        self.set_font("Helvetica", "B", 12)
        self.cell(0, 9, "  " + _safe(text), fill=True, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(*_INK)
        self.ln(3)

    def stat_card(self, x, y, w, h, label, value, color):
        self.set_draw_color(*_GRAY_LINE)
        self.set_fill_color(*_WHITE)
        self.rect(x, y, w, h, style="DF")
        self.set_fill_color(*color)
        self.rect(x, y, w, 2.2, style="F")
        self.set_xy(x, y + 5)
        self.set_font("Helvetica", "B", 18)
        self.set_text_color(*color)
        self.cell(w, 10, _safe(str(value)), align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_x(x)
        self.set_font("Helvetica", "", 8.5)
        self.set_text_color(*_GRAY_TEXT)
        self.cell(w, 5, _safe(label), align="C")
        self.set_text_color(*_INK)

    def pill(self, x, y, text, color, w=20, h=5.5):
        self.set_fill_color(*color)
        self.set_text_color(*_WHITE)
        self.set_font("Helvetica", "B", 7.5)
        self.set_xy(x, y)
        self.cell(w, h, _safe(text), align="C", fill=True)
        self.set_text_color(*_INK)


def _draw_cover(pdf: ReportPDF, *, title: str, subtitle: str, meta_lines: list[str]):
    pdf.add_page()
    pdf.set_fill_color(*_NAVY)
    pdf.rect(0, 0, pdf.w, pdf.h, style="F")
    pdf.set_fill_color(*_TEAL)
    pdf.rect(0, 0, pdf.w, 4, style="F")

    if os.path.exists(_LOGO_PATH):
        try:
            pdf.image(_LOGO_PATH, x=(pdf.w - 55) / 2, y=32, w=55)
        except Exception:
            pass

    pdf.set_y(95)
    pdf.set_font("Helvetica", "B", 26)
    pdf.set_text_color(*_WHITE)
    pdf.multi_cell(0, 12, _safe(title), align="C")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 14)
    pdf.set_text_color(*_TEAL)
    pdf.multi_cell(0, 8, _safe(subtitle), align="C")

    pdf.ln(14)
    pdf.set_font("Helvetica", "", 10.5)
    pdf.set_text_color(*_WHITE)
    for line in meta_lines:
        pdf.cell(0, 6.5, _safe(line), align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.set_y(-30)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(180, 190, 205)
    pdf.cell(0, 6, _safe("Confidential -- for the intended recipient only"), align="C")
    # header()/footer() key off page_no()==1 for "is this the cover",
    # so no flag needs resetting here.


def _draw_trend_chart(pdf: ReportPDF, daily_counts: "OrderedDict"):
    """Native vector bar chart (no image dependency): events/day across
    the report period, teal bars against a light gridded panel."""
    x0, y0 = pdf.get_x(), pdf.get_y()
    w, h = pdf.epw, 42
    pdf.set_draw_color(*_GRAY_LINE)
    pdf.set_fill_color(250, 251, 252)
    pdf.rect(x0, y0, w, h, style="DF")

    values = list(daily_counts.values())
    n = len(values)
    max_v = max(values) if values and max(values) > 0 else 1
    pad = 4
    plot_w = w - 2 * pad
    plot_h = h - 2 * pad
    bar_gap = 1.2
    bar_w = max((plot_w / n) - bar_gap, 0.8) if n else plot_w

    # gridlines (0/50%/100% of max)
    pdf.set_draw_color(235, 238, 242)
    for frac in (0.0, 0.5, 1.0):
        gy = y0 + pad + plot_h * (1 - frac)
        pdf.line(x0 + pad, gy, x0 + w - pad, gy)

    for i, v in enumerate(values):
        bar_h = (v / max_v) * plot_h
        bx = x0 + pad + i * (bar_w + bar_gap)
        by = y0 + pad + (plot_h - bar_h)
        pdf.set_fill_color(*(_TEAL if v == 0 else _RED if v >= max_v * 0.66 else _YELLOW if v > 0 else _TEAL))
        pdf.set_fill_color(*(_TEAL_DIM if v == 0 else _TEAL))
        pdf.rect(bx, by, bar_w, max(bar_h, 0.6), style="F")

    pdf.set_xy(x0, y0 + h + 1)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*_GRAY_TEXT)
    dates = list(daily_counts.keys())
    if dates:
        pdf.cell(w / 2, 4, _safe(dates[0].strftime("%d %b")))
        pdf.set_xy(x0 + w / 2, y0 + h + 1)
        pdf.cell(w / 2, 4, _safe(dates[-1].strftime("%d %b")), align="R")
    pdf.set_text_color(*_INK)
    pdf.set_xy(x0, y0 + h + 6)


def render_report_pdf(*, report_type: str, scope_type: str, scope_id: str,
                       scope_label: str, period_start: datetime, period_end: datetime,
                       data: dict, generated_by: str) -> bytes:
    title = "CloudOps Monitoring Report"
    subtitle = f"{report_type.title()} Report -- {scope_type.title()}: {scope_label or scope_id}"
    now = datetime.now(timezone.utc)
    meta_lines = [
        f"Period: {period_start:%d %b %Y %H:%M} - {period_end:%d %b %Y %H:%M} UTC",
        f"Generated: {now:%d %b %Y %H:%M} UTC by {generated_by}",
    ]

    pdf = ReportPDF({"title": title}, format="A4")
    _draw_cover(pdf, title=title, subtitle=subtitle, meta_lines=meta_lines)

    pdf.add_page()
    account = data.get("account")

    pdf.section_title("Account / Scope")
    pdf.set_font("Helvetica", "", 10)
    if account:
        pdf.multi_cell(0, 6, _safe(
            f"Account: {account['name']} ({account['account_id']})   "
            f"Default region: {account.get('region_default') or 'n/a'}"
        ))
    else:
        pdf.multi_cell(0, 6, _safe(f"Scope: {scope_type} = {scope_id}"))

    pdf.section_title("Incident Summary")
    sc = data["severity_counts"]
    card_w = pdf.epw / 4 - 3
    y = pdf.get_y()
    pdf.stat_card(pdf.l_margin, y, card_w, 22, "TOTAL EVENTS", data["total_count"], _TEAL)
    pdf.stat_card(pdf.l_margin + card_w + 4, y, card_w, 22, "CRITICAL", sc["CRITICAL"], _RED)
    pdf.stat_card(pdf.l_margin + 2 * (card_w + 4), y, card_w, 22, "WARNING", sc["WARNING"], _YELLOW)
    pdf.stat_card(pdf.l_margin + 3 * (card_w + 4), y, card_w, 22, "OPEN NOW", data["open_count"], _GREEN if data["open_count"] == 0 else _RED)
    pdf.set_y(y + 28)

    pdf.section_title("Event Trend Over Period")
    _draw_trend_chart(pdf, data["daily_counts"])

    pdf.section_title("Affected Resources")
    pdf.set_font("Helvetica", "", 10)
    if data["affected_resources"]:
        for i, r in enumerate(data["affected_resources"]):
            fill = i % 2 == 0
            pdf.set_fill_color(*_TEAL_DIM) if fill else None
            pdf.cell(0, 6.5, _safe(f"  [{r['resource_type']}]  {r['name'] or r['resource_id']}  ({r['resource_id']})"),
                     fill=fill, new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.multi_cell(0, 6, _safe("No resources with events in this period."))

    pdf.section_title("Incident Timeline / Alerts & Events")
    col_w = [30, 20, 20, 45, 32, 43]
    headers = ["Time (UTC)", "Severity", "Status", "Resource", "Metric", "Value"]
    pdf.set_fill_color(*_NAVY_CARD)
    pdf.set_text_color(*_WHITE)
    pdf.set_font("Helvetica", "B", 8.5)
    for w_, h_txt in zip(col_w, headers):
        pdf.cell(w_, 7, _safe(h_txt), fill=True)
    pdf.ln()
    pdf.set_text_color(*_INK)
    pdf.set_font("Helvetica", "", 8.5)
    for i, a in enumerate(data["alerts"]):
        row_y = pdf.get_y()
        if i % 2 == 0:
            pdf.set_fill_color(248, 249, 251)
            pdf.rect(pdf.l_margin, row_y, sum(col_w), 6.5, style="F")
        pdf.set_xy(pdf.l_margin, row_y)
        pdf.cell(col_w[0], 6.5, _safe(a["created_at"].strftime("%Y-%m-%d %H:%M")))
        pdf.pill(pdf.get_x(), row_y + 0.4, a.get("severity") or "-", _severity_color(a.get("severity")), w=col_w[1] - 2)
        pdf.set_xy(pdf.get_x() + col_w[1], row_y)
        pdf.pill(pdf.get_x(), row_y + 0.4, a.get("status") or "-", _status_color(a.get("status")), w=col_w[2] - 2)
        pdf.set_xy(pdf.get_x() + col_w[2], row_y)
        pdf.cell(col_w[3], 6.5, _safe((a.get("resource_name") or a["resource_id"])[:30]))
        pdf.cell(col_w[4], 6.5, _safe(a.get("metric_name") or "-"))
        pdf.cell(col_w[5], 6.5, _safe(a.get("value")), new_x="LMARGIN", new_y="NEXT")
    if not data["alerts"]:
        pdf.multi_cell(0, 6, _safe("No alerts/events recorded in this period -- clean run."))

    pdf.section_title("Resolution / Current Status")
    pdf.set_font("Helvetica", "", 10)
    resolved = data["total_count"] - data["open_count"]
    pdf.multi_cell(0, 6, _safe(
        f"{resolved} of {data['total_count']} events resolved as of report generation. "
        f"{data['open_count']} remain open and are being actively tracked in CloudOps."
    ))

    return bytes(pdf.output(dest="S"))
