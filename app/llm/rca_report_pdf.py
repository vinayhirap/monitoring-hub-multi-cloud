# app/llm/rca_report_pdf.py
"""
Renders app/llm/rca_report.py's markdown RCA report as a downloadable, branded PDF, using fpdf2 (pure Python, no compiled
system dependency).

Deliberately not a full markdown engine. This app's report markdown only ever uses:
  '#' / '##' headers, '-' bullets, '**bold**' inline, a fixed run of "- **Label:** value" metadata lines right after the
  title, "**Lead-in.** text" paragraphs in the summary, "- **time** - event" timeline bullets, and "- [Title](url)"
  reference links. Each is handled explicitly below.

Layout (redesigned after an audit of the first real report):
  header band -> title + severity/status chips -> key-figures strip (reading, limit, over by, open for)
  -> details table (wraps long values) -> notice banner -> summary paragraphs with bold lead-ins -> bullets
  -> timeline with connected dots -> references -> footer (confidential, alert reference, page x/y).

Branding matches the app shell (Layout.jsx logo tile, --accent): navy #0b1220 band, teal #2bb3ac accent. Keep the palette
below in sync if the theme changes.
"""
import re
from datetime import datetime, timezone

from fpdf import FPDF
from fpdf.fonts import FontFace

_MARGIN = 15

_NAVY = (11, 18, 32)          # #0b1220
_TEAL = (43, 179, 172)        # #2bb3ac
_TEAL_DARK = (28, 128, 123)
_TEAL_TINT = (230, 245, 244)
_WHITE = (255, 255, 255)
_INK = (30, 38, 54)
_MUTED = (110, 124, 150)
_ROW_ALT = (244, 247, 250)
_BORDER = (222, 228, 236)

_SEVERITY_COLORS = {
    "CRITICAL": (214, 62, 62),
    "WARNING": (196, 130, 30),
    "INFO": (60, 130, 200),
}
_STATUS_COLORS = {"ACTIVE": (60, 130, 200), "RESOLVED": (46, 160, 100), "ACKNOWLEDGED": (120, 100, 190)}

_UNICODE_REPLACEMENTS = {
    "\u2022": "-",     # bullet
    "\u2014": " - ",   # em dash
    "\u2013": "-",     # en dash
    "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"',
    "\u2026": "...",
    "\u2192": "->",
}

# Rows the PDF leaves out of its details table because the title chips, key-figures strip and footnote already show them
# (the Markdown download keeps every row).
_PDF_SKIP_ROWS = {"alert", "severity", "status", "reading vs limit", "report generated"}

_METADATA_LINE = re.compile(r"^- \*\*(?P<label>[^*]+):\*\* (?P<value>.*)$")
_MD_LINK_BULLET = re.compile(r"^\[(?P<title>[^\]]+)\]\((?P<url>https?://[^)]+)\)$")
_LEAD_IN = re.compile(r"^\*\*(?P<lead>[^*]+?)\.\*\*\s+(?P<rest>.+)$")
_TIMELINE_BULLET = re.compile(r"^- \*\*(?P<time>[^*]+)\*\* \u2014 (?P<event>.+)$")


def _latin1_safe(text: str) -> str:
    """fpdf2's core Helvetica supports latin-1 only. Map the common typographic characters to ASCII, then hard-fall back
    (replace, never raise) so an odd character in a resource name cannot turn a download into a 500."""
    for uni, ascii_equiv in _UNICODE_REPLACEMENTS.items():
        text = text.replace(uni, ascii_equiv)
    text = text.replace(" -- ", " - ")
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _strip_bold_markers(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text)


class _RCAReportPDF(FPDF):
    subtitle = "Root Cause Analysis Report"
    alert_ref = ""

    def header(self):
        self.set_fill_color(*_NAVY)
        self.rect(0, 0, self.w, 22, "F")
        self.set_xy(_MARGIN, 5)
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(*_WHITE)
        self.cell(0, 7, "AURIONPRO", new_x="LMARGIN", new_y="NEXT", align="L")
        self.set_xy(_MARGIN, 12)
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*_TEAL)
        self.cell(0, 5, "CloudOps  |  " + self.subtitle)
        if self.alert_ref:
            self.set_xy(self.w - _MARGIN - 60, 9)
            self.set_font("Helvetica", "B", 10)
            self.set_text_color(*_WHITE)
            self.cell(60, 6, _latin1_safe(self.alert_ref), align="R")
        self.set_text_color(*_INK)
        self.set_y(30)

    def footer(self):
        self.set_y(-15)
        self.set_draw_color(*_BORDER)
        self.line(_MARGIN, self.get_y(), self.w - _MARGIN, self.get_y())
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*_MUTED)
        left = "CONFIDENTIAL  |  AurionPro CloudOps" + (f"  |  {self.alert_ref}" if self.alert_ref else "")
        self.cell(0, 8, _latin1_safe(left), align="L")
        self.set_xy(-40, -12)
        self.cell(25, 8, f"Page {self.page_no()}/{{nb}}", align="R")
        self.set_text_color(*_INK)


# ── building blocks ──────────────────────────────────────────────────────────

def _chip(pdf, x, y, text, color, w):
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(*color)
    pdf.set_text_color(*_WHITE)
    pdf.set_xy(x, y)
    pdf.cell(w, 6.5, text, align="C", fill=True)
    pdf.set_text_color(*_INK)


def _draw_title_block(pdf, title, severity, status, subtitle):
    y_start = pdf.get_y()
    sev = (severity or "").upper() or "N/A"
    st = (status or "").upper()
    pdf.set_font("Helvetica", "B", 8)
    sev_w = 8 + pdf.get_string_width(sev)
    st_w = (8 + pdf.get_string_width(st)) if st else 0
    chips_w = sev_w + (st_w + 3 if st else 0)
    pdf.set_font("Helvetica", "B", 17)
    pdf.set_text_color(*_INK)
    avail_w = pdf.w - 2 * _MARGIN - chips_w - 5
    pdf.multi_cell(avail_w, 8, _latin1_safe(title), new_x="LMARGIN", new_y="NEXT", align="L")
    if subtitle:
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(*_MUTED)
        pdf.multi_cell(avail_w, 5, _latin1_safe(subtitle), new_x="LMARGIN", new_y="NEXT", align="L")
    bottom = pdf.get_y()

    x = pdf.w - _MARGIN
    if st:
        x -= st_w
        _chip(pdf, x, y_start + 1, st, _STATUS_COLORS.get(st, _MUTED), st_w)
        x -= 3
    x -= sev_w
    _chip(pdf, x, y_start + 1, sev, _SEVERITY_COLORS.get(sev, _MUTED), sev_w)

    pdf.set_xy(_MARGIN, bottom + 2)
    pdf.set_draw_color(*_TEAL)
    pdf.set_line_width(0.8)
    pdf.line(_MARGIN, pdf.get_y(), pdf.w - _MARGIN, pdf.get_y())
    pdf.set_line_width(0.2)
    pdf.set_draw_color(*_BORDER)
    pdf.ln(6)


def _draw_kpis(pdf, kpis, severity):
    if not kpis:
        return
    gap = 3
    n = len(kpis)
    card_w = (pdf.w - 2 * _MARGIN - gap * (n - 1)) / n
    card_h = 21
    y = pdf.get_y()
    sev_color = _SEVERITY_COLORS.get((severity or "").upper(), _INK)
    for i, k in enumerate(kpis):
        x = _MARGIN + i * (card_w + gap)
        pdf.set_fill_color(*_ROW_ALT)
        pdf.set_draw_color(*_BORDER)
        pdf.rect(x, y, card_w, card_h, "DF", round_corners=True, corner_radius=1.5)
        pdf.set_xy(x + 3, y + 2.5)
        pdf.set_font("Helvetica", "B", 6.8)
        pdf.set_text_color(*_MUTED)
        pdf.cell(card_w - 6, 3.5, _latin1_safe(k["label"]))
        size = 15
        value = _latin1_safe(str(k["value"]))
        pdf.set_font("Helvetica", "B", size)
        while size > 9 and pdf.get_string_width(value) > card_w - 6:
            size -= 1
            pdf.set_font("Helvetica", "B", size)
        pdf.set_text_color(*(sev_color if k.get("tone") == "severity" else _INK))
        pdf.set_xy(x + 3, y + 7)
        pdf.cell(card_w - 6, 7, value)
        pdf.set_xy(x + 3, y + 15.2)
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(*_MUTED)
        pdf.cell(card_w - 6, 3.5, _latin1_safe(str(k.get("note") or ""))[:42])
    pdf.set_text_color(*_INK)
    pdf.set_xy(_MARGIN, y + card_h + 6)


def _draw_metadata_table(pdf, rows):
    pdf.set_font("Helvetica", "B", 8)
    label_w = max(34, min(58, max(pdf.get_string_width(label.upper()) for label, _ in rows) + 8))
    pdf.set_font("Helvetica", "", 9.5)          # table default: regular. Labels are made bold explicitly below.
    pdf.set_draw_color(*_BORDER)
    with pdf.table(
        col_widths=(label_w, pdf.w - 2 * _MARGIN - label_w), width=pdf.w - 2 * _MARGIN, first_row_as_headings=False,
        borders_layout="HORIZONTAL_LINES", line_height=5, padding=(1.1, 2.5),
        text_align=("LEFT", "LEFT"),
    ) as table:
        label_style = FontFace(emphasis="BOLD", size_pt=8, color=_MUTED)
        value_style = FontFace(emphasis="", size_pt=9.5, color=_INK)
        for label, value in rows:
            row = table.row()
            row.cell(_latin1_safe(label.upper()), style=label_style)
            row.cell(_latin1_safe(_strip_bold_markers(value)), style=value_style)
    pdf.ln(5)


def _ensure_room(pdf, needed_mm):
    if pdf.get_y() + needed_mm > pdf.h - 22:
        pdf.add_page()


def _draw_section_header(pdf, text):
    _ensure_room(pdf, 30)               # never leave a header stranded at the bottom of a page
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 12.5)
    pdf.set_text_color(*_INK)
    pdf.multi_cell(0, 7, _strip_bold_markers(text), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.set_draw_color(*_TEAL)
    pdf.set_line_width(0.6)
    y = pdf.get_y() + 0.5
    pdf.line(_MARGIN, y, _MARGIN + 24, y)
    pdf.set_line_width(0.2)
    pdf.set_draw_color(*_BORDER)
    pdf.set_xy(_MARGIN, y + 3.5)


def _draw_banner(pdf, text):
    pdf.set_font("Helvetica", "", 9)
    inner_w = pdf.w - 2 * _MARGIN - 8
    lines = pdf.multi_cell(inner_w, 4.8, text, dry_run=True, output="LINES")
    h = len(lines) * 4.8 + 5
    _ensure_room(pdf, h + 4)
    y = pdf.get_y()
    pdf.set_fill_color(*_TEAL_TINT)
    pdf.rect(_MARGIN, y, pdf.w - 2 * _MARGIN, h, "F")
    pdf.set_fill_color(*_TEAL)
    pdf.rect(_MARGIN, y, 1.4, h, "F")
    pdf.set_xy(_MARGIN + 5, y + 2.5)
    pdf.set_text_color(*_INK)
    pdf.multi_cell(inner_w, 4.8, text, new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.set_xy(_MARGIN, y + h + 4)


def _bullet_dot(pdf, y_mid):
    pdf.set_fill_color(*_TEAL)
    pdf.ellipse(_MARGIN + 1.2, y_mid - 0.9, 1.8, 1.8, "F")


def _draw_bullet(pdf, text):
    _ensure_room(pdf, 12)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*_INK)
    y = pdf.get_y()
    _bullet_dot(pdf, y + 3)
    pdf.set_x(_MARGIN + 6)
    pdf.multi_cell(pdf.w - 2 * _MARGIN - 6, 5.8, _strip_bold_markers(text), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.ln(1.2)


def _draw_paragraph(pdf, line):
    """'**Lead-in.** text' -> bold lead-in then normal text on the same wrapped paragraph; anything else plain."""
    pdf.set_text_color(*_INK)
    m = _LEAD_IN.match(line)
    if m:
        _ensure_room(pdf, 16)
        pdf.set_x(_MARGIN)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(*_TEAL_DARK)
        pdf.write(5.8, m.group("lead").upper() + "   ")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(*_INK)
        pdf.write(5.8, _strip_bold_markers(m.group("rest")))
        pdf.ln(7.6)
    else:
        pdf.set_x(_MARGIN)
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 5.8, _strip_bold_markers(line), new_x="LMARGIN", new_y="NEXT", align="L")
        pdf.ln(1.5)


def _draw_timeline_event(pdf, when, event, prev_dot_y):
    _ensure_room(pdf, 12)
    y = pdf.get_y()
    time_w = 52
    if prev_dot_y is not None and prev_dot_y < y:          # connector from the previous event's dot
        pdf.set_draw_color(*_BORDER)
        pdf.set_line_width(0.4)
        pdf.line(_MARGIN + 2.1, prev_dot_y, _MARGIN + 2.1, y + 3.3)
        pdf.set_line_width(0.2)
    # Dots are drawn AFTER the connector so the line never crosses them: the previous dot is redrawn over the line's
    # start, then this event's dot over its end.
    pdf.set_fill_color(*_TEAL)
    pdf.set_draw_color(*_WHITE)
    if prev_dot_y is not None and prev_dot_y < y:
        pdf.ellipse(_MARGIN + 0.6, prev_dot_y - 1.5, 3, 3, "DF")
    pdf.ellipse(_MARGIN + 0.6, y + 1.8, 3, 3, "DF")
    pdf.set_xy(_MARGIN + 7, y)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*_INK)
    pdf.cell(time_w, 5.8, _latin1_safe(when))
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(pdf.w - 2 * _MARGIN - 7 - time_w, 5.8, _latin1_safe(event), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.ln(1.8)
    return y + 3.3


def render_pdf(markdown_text: str, title: str, severity: str = None, *, subtitle: str = None, status: str = None,
               kpis: list = None, alert_ref: str = None) -> bytes:
    pdf = _RCAReportPDF(format="A4")
    pdf.alert_ref = alert_ref or ""
    pdf.alias_nb_pages()
    pdf.set_margins(_MARGIN, _MARGIN, _MARGIN)
    pdf.set_auto_page_break(auto=True, margin=20)
    now = datetime.now(timezone.utc)
    pdf.set_title(_latin1_safe(f"RCA Report - {title}"))
    pdf.set_author("AurionPro CloudOps")
    pdf.set_creator("AurionPro CloudOps")
    pdf.set_subject(_latin1_safe("Root cause analysis" + (f" - {alert_ref}" if alert_ref else "")))
    pdf.set_keywords("RCA, root cause analysis, alert, CloudOps")
    pdf.set_creation_date(now)
    pdf.add_page()

    lines = markdown_text.split("\n")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    _draw_title_block(pdf, title, severity, status, subtitle)
    _draw_kpis(pdf, kpis, severity)

    metadata_rows = []
    while lines and lines[0].strip() == "":
        lines.pop(0)
    while lines:
        m = _METADATA_LINE.match(lines[0].strip())
        if not m:
            break
        metadata_rows.append((m.group("label"), m.group("value")))
        lines.pop(0)
    generated_stamp = next((v for k, v in metadata_rows if k.lower() == "report generated"), None)
    shown_rows = [(k, v) for k, v in metadata_rows if k.lower() not in _PDF_SKIP_ROWS]
    if shown_rows:
        _draw_metadata_table(pdf, shown_rows)

    generated_footnote_seen = False
    section = ""
    after_header = False
    prev_dot_y = None
    for raw_line in lines:
        line = raw_line.rstrip()
        if not line:
            if not after_header:
                pdf.ln(1.5)
            continue
        if line.startswith("# ") or line.startswith("## "):
            section = line.lstrip("#").strip()
            prev_dot_y = None
            _draw_section_header(pdf, _latin1_safe(section))
            after_header = True
            continue
        after_header = False
        if line.startswith("Note: "):
            _draw_banner(pdf, _latin1_safe(line))
        elif line.startswith("- "):
            tl = _TIMELINE_BULLET.match(line)
            link_match = _MD_LINK_BULLET.match(line[2:])
            if tl and section.lower() == "timeline":
                prev_dot_y = _draw_timeline_event(pdf, tl.group("time"), tl.group("event"), prev_dot_y)
            elif link_match:
                _ensure_room(pdf, 14)
                y = pdf.get_y()
                _bullet_dot(pdf, y + 3)
                pdf.set_xy(_MARGIN + 6, y)
                pdf.set_font("Helvetica", "U", 10)
                pdf.set_text_color(*_TEAL_DARK)
                pdf.multi_cell(0, 6, _latin1_safe(link_match.group("title")), new_x="LMARGIN", new_y="NEXT",
                               align="L", link=link_match.group("url"))
                pdf.set_font("Helvetica", "", 8)
                pdf.set_text_color(*_MUTED)
                pdf.set_x(_MARGIN + 6)
                pdf.multi_cell(0, 4, _latin1_safe(link_match.group("url")), new_x="LMARGIN", new_y="NEXT", align="L")
                pdf.set_text_color(*_INK)
                pdf.ln(1.5)
            else:
                _draw_bullet(pdf, _latin1_safe(line[2:]))
        elif line.startswith("*Generated automatically"):
            generated_footnote_seen = True
            pdf.ln(3)
            pdf.set_draw_color(*_BORDER)
            pdf.line(_MARGIN, pdf.get_y(), pdf.w - _MARGIN, pdf.get_y())
            pdf.ln(2.5)
            pdf.set_x(_MARGIN)
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(*_MUTED)
            note = line.strip("*")
            if generated_stamp:
                note += f" Generated {generated_stamp}."
            pdf.multi_cell(0, 4.6, _strip_bold_markers(_latin1_safe(note)), new_x="LMARGIN", new_y="NEXT", align="L")
            pdf.set_text_color(*_INK)
        else:
            _draw_paragraph(pdf, _latin1_safe(line))

    if not generated_footnote_seen:
        pdf.ln(3)
        pdf.set_x(_MARGIN)
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(*_MUTED)
        generated_at = now.strftime("%d %b %Y, %H:%M UTC")
        pdf.multi_cell(0, 4.6, f"Generated automatically by AurionPro CloudOps on {generated_at}. "
                               f"Verify before external distribution.", align="L")

    return bytes(pdf.output())
