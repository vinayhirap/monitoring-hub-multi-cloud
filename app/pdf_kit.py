# app/pdf_kit.py
"""
One visual language for every PDF CloudOps produces (RCA reports, weekly / monthly / quarterly / custom reports).

Before this module the RCA report and the periodic reports each had their own header, colours, tables and wording, and
looked like two different products. Everything here is the single source for: palette, header band, footer, title block
with chips, key-figures strip, details table, section headers, notice banner, bullets, lead-in paragraphs, a connected
timeline, a data table with severity/status chips (wrapped and truncated safely), and a labelled bar chart.

Branding matches the app shell (Layout.jsx logo tile, --accent): navy #0b1220 band, teal #2bb3ac accent.
Pure fpdf2 (no compiled dependency). Core Helvetica is latin-1 only, so every string goes through latin1_safe().
"""
import logging
import os
import re
from datetime import datetime, timezone

from fpdf import FPDF
from fpdf.fonts import FontFace

logger = logging.getLogger(__name__)

MARGIN = 15

# The app's real brand mark (the teal double-arc used in Layout.jsx's sidebar logo), transparent so it sits directly on
# the navy band, like the app's own topbar. Replaces the boxed "AslOps" placeholder the reports used to carry.
MARK_PATH = os.path.join(os.path.dirname(__file__), "reports", "assets", "cloudops_mark.png")

NAVY = (11, 18, 32)
TEAL = (43, 179, 172)
TEAL_DARK = (28, 128, 123)
TEAL_TINT = (230, 245, 244)
WHITE = (255, 255, 255)
INK = (30, 38, 54)
MUTED = (110, 124, 150)
ROW_ALT = (244, 247, 250)
BORDER = (222, 228, 236)

SEVERITY_COLORS = {"CRITICAL": (214, 62, 62), "WARNING": (196, 130, 30), "INFO": (60, 130, 200)}
STATUS_COLORS = {"ACTIVE": (60, 130, 200), "OPEN": (214, 62, 62), "RESOLVED": (46, 160, 100),
                 "CLOSED": (46, 160, 100), "ACKNOWLEDGED": (120, 100, 190)}

_UNICODE_REPLACEMENTS = {
    "\u2022": "-", "\u2014": " - ", "\u2013": "-", "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u2192": "->",
}


def latin1_safe(text) -> str:
    """Core Helvetica supports latin-1 only. Map typographic characters to ASCII, then replace (never raise) so an odd
    character in a resource name cannot turn a download into a 500. Also turns a stray ' -- ' into ' - '."""
    text = "" if text is None else str(text)
    for uni, ascii_equiv in _UNICODE_REPLACEMENTS.items():
        text = text.replace(uni, ascii_equiv)
    text = text.replace(" -- ", " - ")
    return text.encode("latin-1", errors="replace").decode("latin-1")


def strip_bold(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text)


def fit_text(pdf, text: str, max_width: float) -> str:
    """Truncate with an ellipsis to fit max_width at the CURRENT font (call set_font first). fpdf2's cell() neither
    clips nor wraps, so an over-long value printed straight into the next column."""
    text = latin1_safe(text)
    if pdf.get_string_width(text) <= max_width:
        return text
    while text and pdf.get_string_width(text + "...") > max_width:
        text = text[:-1]
    return text.rstrip() + "..."


class BrandedPDF(FPDF):
    """Header band + footer shared by every report. Set band_subtitle / band_right / footer_ref after construction."""
    band_subtitle = "Report"
    band_right = ""
    footer_ref = ""
    has_cover = False          # periodic reports open with a full-bleed cover that draws its own page (no band, no footer)

    def header(self):
        if self.has_cover and self.page_no() == 1:
            return
        self.set_fill_color(*NAVY)
        self.rect(0, 0, self.w, 22, "F")
        text_x = MARGIN
        if os.path.exists(MARK_PATH):
            try:
                self.image(MARK_PATH, x=MARGIN, y=4.3, h=13.4)
                text_x = MARGIN + 16
            except Exception as e:                      # a bad image must never break a download
                logger.warning(f"PDF header: could not draw brand mark {MARK_PATH}: {e}")
        self.set_xy(text_x, 5)
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(*WHITE)
        self.cell(0, 7, "AURIONPRO", new_x="LMARGIN", new_y="NEXT", align="L")
        self.set_xy(text_x, 12)
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*TEAL)
        self.cell(0, 5, latin1_safe("CloudOps  |  " + self.band_subtitle))
        if self.band_right:
            self.set_xy(self.w - MARGIN - 80, 9)
            self.set_font("Helvetica", "B", 10)
            self.set_text_color(*WHITE)
            self.cell(80, 6, latin1_safe(self.band_right), align="R")
        self.set_text_color(*INK)
        self.set_y(30)

    def footer(self):
        if self.has_cover and self.page_no() == 1:
            return
        self.set_y(-15)
        self.set_draw_color(*BORDER)
        self.line(MARGIN, self.get_y(), self.w - MARGIN, self.get_y())
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*MUTED)
        left = "CONFIDENTIAL  |  AurionPro CloudOps" + (f"  |  {self.footer_ref}" if self.footer_ref else "")
        self.cell(0, 8, latin1_safe(left), align="L")
        self.set_xy(-40, -12)
        self.cell(25, 8, f"Page {self.page_no()}/{{nb}}", align="R")
        self.set_text_color(*INK)


def new_document(*, band_subtitle, band_right="", footer_ref="", title="", subject="", keywords="", cover=False):
    pdf = BrandedPDF(format="A4")
    pdf.band_subtitle, pdf.band_right, pdf.footer_ref = band_subtitle, band_right, footer_ref
    pdf.has_cover = cover
    pdf.alias_nb_pages()
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_title(latin1_safe(title))
    pdf.set_author("AurionPro CloudOps")
    pdf.set_creator("AurionPro CloudOps")
    pdf.set_subject(latin1_safe(subject))
    pdf.set_keywords(keywords or "CloudOps, report")
    pdf.set_creation_date(datetime.now(timezone.utc))
    pdf.add_page()
    return pdf


def cover_page(pdf, *, title, subtitle="", meta_lines=(), confidentiality="Confidential: for the intended recipient only"):
    """Full-bleed navy cover on page 1 (document must be created with cover=True). Brand mark, AURIONPRO / CloudOps as real
    text, the title, a teal subtitle and a few meta lines."""
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, pdf.w, pdf.h, "F")
    pdf.set_fill_color(*TEAL)
    pdf.rect(0, 0, pdf.w, 4, "F")
    if os.path.exists(MARK_PATH):
        try:
            mark_w = 34
            pdf.image(MARK_PATH, x=(pdf.w - mark_w) / 2, y=40, w=mark_w)
        except Exception as e:
            logger.warning(f"PDF cover: could not draw brand mark {MARK_PATH}: {e}")
    pdf.set_y(78)
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(180, 190, 205)
    pdf.cell(0, 6, "AURIONPRO", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(*WHITE)
    pdf.cell(0, 9, "CloudOps", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(108)
    pdf.set_font("Helvetica", "B", 26)
    pdf.set_text_color(*WHITE)
    pdf.multi_cell(0, 12, latin1_safe(title), align="C")
    if subtitle:
        pdf.ln(2)
        pdf.set_font("Helvetica", "", 14)
        pdf.set_text_color(*TEAL)
        pdf.multi_cell(0, 8, latin1_safe(subtitle), align="C")
    pdf.ln(12)
    pdf.set_font("Helvetica", "", 10.5)
    pdf.set_text_color(*WHITE)
    for line in meta_lines:
        pdf.cell(0, 6.5, latin1_safe(line), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(-30)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(180, 190, 205)
    pdf.cell(0, 6, latin1_safe(confidentiality), align="C")
    pdf.set_text_color(*INK)
    pdf.add_page()


# ── blocks ───────────────────────────────────────────────────────────────────

def chip(pdf, x, y, text, color, w):
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(*color)
    pdf.set_text_color(*WHITE)
    pdf.set_xy(x, y)
    pdf.cell(w, 6.5, latin1_safe(text), align="C", fill=True)
    pdf.set_text_color(*INK)


def title_block(pdf, title, chips=(), subtitle=None):
    """Large title with right-aligned chips [(text, rgb), ...] and a muted subtitle, then a teal rule."""
    y_start = pdf.get_y()
    pdf.set_font("Helvetica", "B", 8)
    widths = [8 + pdf.get_string_width(latin1_safe(t)) for t, _ in chips]
    chips_w = sum(widths) + 3 * max(0, len(widths) - 1)
    pdf.set_font("Helvetica", "B", 17)
    pdf.set_text_color(*INK)
    avail_w = pdf.w - 2 * MARGIN - chips_w - 5
    pdf.multi_cell(avail_w, 8, latin1_safe(title), new_x="LMARGIN", new_y="NEXT", align="L")
    if subtitle:
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(avail_w, 5, latin1_safe(subtitle), new_x="LMARGIN", new_y="NEXT", align="L")
    bottom = pdf.get_y()
    x = pdf.w - MARGIN
    for (text, color), w in reversed(list(zip(chips, widths))):
        x -= w
        chip(pdf, x, y_start + 1, text, color, w)
        x -= 3
    pdf.set_xy(MARGIN, bottom + 2)
    pdf.set_draw_color(*TEAL)
    pdf.set_line_width(0.8)
    pdf.line(MARGIN, pdf.get_y(), pdf.w - MARGIN, pdf.get_y())
    pdf.set_line_width(0.2)
    pdf.set_draw_color(*BORDER)
    pdf.ln(6)


def kpi_strip(pdf, kpis, severity=None):
    """kpis: [{label, value, note, tone}] with tone 'ink' | 'severity' | 'crit' | 'warn' | 'ok'."""
    if not kpis:
        return
    gap = 3
    n = len(kpis)
    card_w = (pdf.w - 2 * MARGIN - gap * (n - 1)) / n
    card_h = 21
    y = pdf.get_y()
    tone_colors = {"ink": INK, "crit": SEVERITY_COLORS["CRITICAL"], "warn": SEVERITY_COLORS["WARNING"],
                   "ok": STATUS_COLORS["RESOLVED"], "severity": SEVERITY_COLORS.get((severity or "").upper(), INK)}
    for i, k in enumerate(kpis):
        x = MARGIN + i * (card_w + gap)
        pdf.set_fill_color(*ROW_ALT)
        pdf.set_draw_color(*BORDER)
        pdf.rect(x, y, card_w, card_h, "DF", round_corners=True, corner_radius=1.5)
        pdf.set_xy(x + 3, y + 2.5)
        pdf.set_font("Helvetica", "B", 6.8)
        pdf.set_text_color(*MUTED)
        pdf.cell(card_w - 6, 3.5, latin1_safe(k["label"]))
        size = 15
        value = latin1_safe(str(k["value"]))
        pdf.set_font("Helvetica", "B", size)
        while size > 9 and pdf.get_string_width(value) > card_w - 6:
            size -= 1
            pdf.set_font("Helvetica", "B", size)
        pdf.set_text_color(*tone_colors.get(k.get("tone", "ink"), INK))
        pdf.set_xy(x + 3, y + 7)
        pdf.cell(card_w - 6, 7, value)
        pdf.set_xy(x + 3, y + 15.2)
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(*MUTED)
        pdf.cell(card_w - 6, 3.5, latin1_safe(str(k.get("note") or ""))[:46])
    pdf.set_text_color(*INK)
    pdf.set_xy(MARGIN, y + card_h + 6)


def details_table(pdf, rows):
    pdf.set_font("Helvetica", "B", 8)
    label_w = max(34, min(58, max(pdf.get_string_width(latin1_safe(label).upper()) for label, _ in rows) + 8))
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_draw_color(*BORDER)
    with pdf.table(
        col_widths=(label_w, pdf.w - 2 * MARGIN - label_w), width=pdf.w - 2 * MARGIN, first_row_as_headings=False,
        borders_layout="HORIZONTAL_LINES", line_height=5, padding=(1.1, 2.5), text_align=("LEFT", "LEFT"),
    ) as table:
        label_style = FontFace(emphasis="BOLD", size_pt=8, color=MUTED)
        value_style = FontFace(emphasis="", size_pt=9.5, color=INK)
        for label, value in rows:
            row = table.row()
            row.cell(latin1_safe(label.upper()), style=label_style)
            row.cell(latin1_safe(strip_bold(str(value))), style=value_style)
    pdf.ln(5)


def ensure_room(pdf, needed_mm):
    if pdf.get_y() + needed_mm > pdf.h - 22:
        pdf.add_page()


def section_header(pdf, text):
    ensure_room(pdf, 30)               # never leave a header stranded at the bottom of a page
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 12.5)
    pdf.set_text_color(*INK)
    pdf.multi_cell(0, 7, strip_bold(latin1_safe(text)), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.set_draw_color(*TEAL)
    pdf.set_line_width(0.6)
    y = pdf.get_y() + 0.5
    pdf.line(MARGIN, y, MARGIN + 24, y)
    pdf.set_line_width(0.2)
    pdf.set_draw_color(*BORDER)
    pdf.set_xy(MARGIN, y + 3.5)


def banner(pdf, text):
    text = latin1_safe(text)
    pdf.set_font("Helvetica", "", 9)
    inner_w = pdf.w - 2 * MARGIN - 8
    lines = pdf.multi_cell(inner_w, 4.8, text, dry_run=True, output="LINES")
    h = len(lines) * 4.8 + 5
    ensure_room(pdf, h + 4)
    y = pdf.get_y()
    pdf.set_fill_color(*TEAL_TINT)
    pdf.rect(MARGIN, y, pdf.w - 2 * MARGIN, h, "F")
    pdf.set_fill_color(*TEAL)
    pdf.rect(MARGIN, y, 1.4, h, "F")
    pdf.set_xy(MARGIN + 5, y + 2.5)
    pdf.set_text_color(*INK)
    pdf.multi_cell(inner_w, 4.8, text, new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.set_xy(MARGIN, y + h + 4)


def bullet_dot(pdf, y_mid):
    pdf.set_fill_color(*TEAL)
    pdf.ellipse(MARGIN + 1.2, y_mid - 0.9, 1.8, 1.8, "F")


def bullet(pdf, text):
    ensure_room(pdf, 12)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*INK)
    y = pdf.get_y()
    bullet_dot(pdf, y + 3)
    pdf.set_x(MARGIN + 6)
    pdf.multi_cell(pdf.w - 2 * MARGIN - 6, 5.8, strip_bold(latin1_safe(text)), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.ln(1.2)


def paragraph(pdf, lead, text):
    """'LEAD-IN  text' on one wrapped, LEFT-aligned paragraph (justified text made huge gaps around long ids)."""
    ensure_room(pdf, 16)
    pdf.set_x(MARGIN)
    pdf.set_text_color(*TEAL_DARK)
    pdf.set_font("Helvetica", "B", 10)
    pdf.write(5.8, latin1_safe(lead).upper() + "   ")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*INK)
    pdf.write(5.8, strip_bold(latin1_safe(text)))
    pdf.ln(7.6)


def plain_paragraph(pdf, text, size=10, color=INK, italic=False):
    pdf.set_x(MARGIN)
    pdf.set_font("Helvetica", "I" if italic else "", size)
    pdf.set_text_color(*color)
    pdf.multi_cell(0, 5.8 if size >= 10 else 5, strip_bold(latin1_safe(text)), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.set_text_color(*INK)
    pdf.ln(1.5)


def timeline_event(pdf, when, event, prev_dot_y):
    ensure_room(pdf, 12)
    y = pdf.get_y()
    time_w = 52
    if prev_dot_y is not None and prev_dot_y < y:
        pdf.set_draw_color(*BORDER)
        pdf.set_line_width(0.4)
        pdf.line(MARGIN + 2.1, prev_dot_y, MARGIN + 2.1, y + 3.3)
        pdf.set_line_width(0.2)
    pdf.set_fill_color(*TEAL)
    pdf.set_draw_color(*WHITE)
    if prev_dot_y is not None and prev_dot_y < y:
        pdf.ellipse(MARGIN + 0.6, prev_dot_y - 1.5, 3, 3, "DF")
    pdf.ellipse(MARGIN + 0.6, y + 1.8, 3, 3, "DF")
    pdf.set_xy(MARGIN + 7, y)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*INK)
    pdf.cell(time_w, 5.8, latin1_safe(when))
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(pdf.w - 2 * MARGIN - 7 - time_w, 5.8, latin1_safe(event), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.ln(1.8)
    return y + 3.3


def footnote(pdf, text):
    pdf.ln(3)
    pdf.set_draw_color(*BORDER)
    pdf.line(MARGIN, pdf.get_y(), pdf.w - MARGIN, pdf.get_y())
    pdf.ln(2.5)
    plain_paragraph(pdf, text, size=8, color=MUTED, italic=True)


# ── data table ───────────────────────────────────────────────────────────────

def data_table(pdf, columns, rows, *, row_h=6.4, zebra=True):
    """columns: [(header, width_mm, align)] where align is 'L' | 'R' | 'C'. Widths are used exactly as given (no auto
    scaling), so a header always sits over its own column. A cell value is a string, or ('chip', text, rgb).

    Every row is placed by explicit x positions computed from the column widths, never by 'advance the cursor and hope':
    the previous periodic-report table advanced the cursor after a chip and then advanced it again, which shifted every
    later column one slot to the right (status under 'Resource', resource under 'Metric', ...).
    """
    total_w = sum(c[1] for c in columns)
    # never strand a header with one or two rows at the foot of a page: need the header and the first few rows to fit
    ensure_room(pdf, row_h * (1 + min(len(rows), 4)) + 8)

    def header_row():
        ensure_room(pdf, row_h * 2)
        y = pdf.get_y()
        pdf.set_fill_color(*NAVY)
        pdf.rect(MARGIN, y, total_w, row_h + 0.8, "F")
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_text_color(*WHITE)
        x = MARGIN
        for header, w, align in columns:
            pdf.set_xy(x + 1.5, y)
            pdf.cell(w - 3, row_h + 0.8, latin1_safe(header).upper(), align=align)
            x += w
        pdf.set_text_color(*INK)
        pdf.set_y(y + row_h + 0.8)

    header_row()
    for i, row in enumerate(rows):
        if pdf.get_y() + row_h > pdf.h - 22:
            pdf.add_page()
            header_row()
        y = pdf.get_y()
        if zebra and i % 2 == 1:
            pdf.set_fill_color(*ROW_ALT)
            pdf.rect(MARGIN, y, total_w, row_h, "F")
        x = MARGIN
        for (header, w, align), value in zip(columns, row):
            if isinstance(value, tuple) and value and value[0] == "chip":
                _, text, color = value
                pdf.set_font("Helvetica", "B", 6.8)
                cw = min(w - 3, pdf.get_string_width(latin1_safe(text)) + 6)
                chip_x = x + 1.5 if align == "L" else x + (w - cw) / 2
                pdf.set_fill_color(*color)
                pdf.set_text_color(*WHITE)
                pdf.set_xy(chip_x, y + 0.9)
                pdf.cell(cw, row_h - 1.8, latin1_safe(text), align="C", fill=True)
            else:
                pdf.set_font("Helvetica", "", 8.5)
                pdf.set_text_color(*INK)
                pdf.set_xy(x + 1.5, y)
                pdf.cell(w - 3, row_h, fit_text(pdf, str(value), w - 3.5), align=align)
            x += w
        pdf.set_text_color(*INK)
        pdf.set_y(y + row_h)
    pdf.ln(2)


# ── chart ────────────────────────────────────────────────────────────────────

def bar_chart(pdf, labels, values, *, height=44, caption=None):
    """Native vector bar chart WITH a y-scale, a value above every bar and a date under every bar. (The previous chart
    had no numbers at all: just unlabeled bars and two end dates.)"""
    ensure_room(pdf, height + 16)
    x0, y0 = MARGIN, pdf.get_y()
    w = pdf.w - 2 * MARGIN
    pdf.set_draw_color(*BORDER)
    pdf.set_fill_color(250, 251, 253)
    pdf.rect(x0, y0, w, height, "DF", round_corners=True, corner_radius=1.5)
    n = len(values)
    max_v = max(values) if values and max(values) > 0 else 1
    pad_l, pad_r, pad_t, pad_b = 14, 5, 9, 3
    plot_w, plot_h = w - pad_l - pad_r, height - pad_t - pad_b
    # y gridlines: 0 / 50% / 100% with their numbers
    pdf.set_font("Helvetica", "", 7)
    for frac in (0.0, 0.5, 1.0):
        gy = y0 + pad_t + plot_h * (1 - frac)
        pdf.set_draw_color(232, 236, 241)
        pdf.line(x0 + pad_l, gy, x0 + w - pad_r, gy)
        pdf.set_text_color(*MUTED)
        pdf.set_xy(x0 + 1, gy - 1.8)
        pdf.cell(pad_l - 3, 3.6, f"{int(round(max_v * frac)):,}", align="R")
    slot = plot_w / n if n else plot_w
    bar_w = max(slot * 0.62, 1.2)
    for i, v in enumerate(values):
        bar_h = (v / max_v) * plot_h
        bx = x0 + pad_l + i * slot + (slot - bar_w) / 2
        by = y0 + pad_t + (plot_h - bar_h)
        pdf.set_fill_color(*(BORDER if v == 0 else TEAL))
        pdf.rect(bx, by, bar_w, max(bar_h, 0.6), "F")
        if n <= 16:                                              # per-bar numbers only while they stay legible
            pdf.set_font("Helvetica", "B", 7)
            pdf.set_text_color(*INK)
            pdf.set_xy(bx - 3, by - 4)
            pdf.cell(bar_w + 6, 3.5, f"{int(v):,}", align="C")
    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(*MUTED)
    step = max(1, n // 10)
    for i, label in enumerate(labels):
        if i % step == 0 or i == n - 1:
            bx = x0 + pad_l + i * slot
            pdf.set_xy(bx - 2, y0 + height + 1)
            pdf.cell(slot + 4, 3.5, latin1_safe(label), align="C")
    pdf.set_text_color(*INK)
    pdf.set_xy(MARGIN, y0 + height + 7)
    if caption:
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(0, 4.6, latin1_safe(caption), new_x="LMARGIN", new_y="NEXT", align="L")
        pdf.set_text_color(*INK)
    pdf.ln(2)
