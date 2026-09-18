# app/llm/rca_report_pdf.py
"""
Renders app/llm/rca_report.py's markdown RCA report as a downloadable,
branded PDF, using fpdf2 (pure-Python, no compiled system dependency).

Deliberately not a full markdown-to-PDF engine -- this app's report
markdown only ever uses '#'/'##' headers, '-' bullets, '**bold**'
inline, a fixed run of "- **Label:** value" metadata lines right after
the title, and "- [Title](url)" reference links, all handled
explicitly below rather than pulling in a general markdown-rendering
dependency for five constructs.

Branding matches the app shell exactly (see frontend/src/components/
Layout.jsx's sidebar logo and frontend/src/pages/Alerts.css's --accent
var): navy #0b1220 header band, teal #2bb3ac accent, "AURIONPRO" /
"CloudOps" wordmark -- so a report looks like it came from the same
product a reader already trusts, not a bare text dump.
"""
import re
from datetime import datetime, timezone

from fpdf import FPDF

_MARGIN = 15

# Same palette as the app shell (Layout.jsx logo tile / Alerts.css
# --accent) -- keep these two files in sync if the app's theme colors
# ever change.
_NAVY = (11, 18, 32)          # #0b1220 -- sidebar/header band
_TEAL = (43, 179, 172)        # #2bb3ac -- brand accent
_WHITE = (255, 255, 255)
_INK = (30, 38, 54)           # body text -- near-black, not pure black
_MUTED = (110, 124, 150)      # secondary text / labels
_ROW_ALT = (244, 247, 250)    # zebra-striped metadata table rows
_BORDER = (222, 228, 236)

_SEVERITY_COLORS = {
    "CRITICAL": (214, 62, 62),
    "WARNING": (196, 130, 30),
    "INFO": (60, 130, 200),
}

_UNICODE_REPLACEMENTS = {
    "\u2022": "-",   # •
    "\u2014": "--",  # —
    "\u2013": "-",   # –
    "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"',
    "\u2026": "...",
}

# Matches the fixed "- **Label:** value" metadata lines rca_report.py's
# render_markdown() always emits right after the title -- rendered as a
# proper table below instead of plain bullets.
_METADATA_LINE = re.compile(r"^- \*\*(?P<label>[^*]+):\*\* (?P<value>.*)$")

# Matches a References-section bullet: "- [Title](https://...)" --
# rendered as a real clickable link instead of literal brackets.
_MD_LINK_BULLET = re.compile(r"^\[(?P<title>[^\]]+)\]\((?P<url>https?://[^)]+)\)$")


def _latin1_safe(text: str) -> str:
    """fpdf2's core Helvetica font only supports latin-1 -- this app's
    generated text (rca_report.py's timeline lines, LLM narrative
    output) routinely includes em-dashes/smart quotes/bullets. Map the
    common ones to ASCII equivalents, then hard-fallback (replace, not
    raise) for anything else so an unexpected character from, say, a
    resource name never crashes PDF generation -- a slightly-mangled
    character beats a 500 error on a download endpoint."""
    for uni, ascii_equiv in _UNICODE_REPLACEMENTS.items():
        text = text.replace(uni, ascii_equiv)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _strip_bold_markers(text: str) -> str:
    # fpdf2's core Helvetica font has no separate bold-inline run
    # support inside a single multi_cell call without a rich-text
    # helper -- for a report's supporting prose, stripping the **
    # markers and keeping the words is a fine tradeoff over pulling in
    # a heavier PDF layout engine for real inline bold.
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text)


class _RCAReportPDF(FPDF):
    """Adds the branded header band (every page) and footer (every
    page) that plain FPDF doesn't give you for free -- fpdf2 calls
    header()/footer() automatically on add_page()/output()."""

    subtitle = "Root Cause Analysis Report"

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
        self.set_text_color(*_INK)
        self.set_y(28)

    def footer(self):
        self.set_y(-15)
        self.set_draw_color(*_BORDER)
        self.line(_MARGIN, self.get_y(), self.w - _MARGIN, self.get_y())
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*_MUTED)
        self.cell(0, 8, "CONFIDENTIAL -- Generated automatically, verify before external distribution", align="L")
        self.set_xy(-40, -12)
        self.cell(25, 8, f"Page {self.page_no()}/{{nb}}", align="R")
        self.set_text_color(*_INK)


def _draw_title_block(pdf: _RCAReportPDF, title: str, severity: str):
    y_start = pdf.get_y()
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(*_INK)
    chip_w = 26
    avail_w = pdf.w - 2 * _MARGIN - chip_w - 4  # leave room for the severity chip
    pdf.multi_cell(avail_w, 8, _latin1_safe(title), new_x="LMARGIN", new_y="NEXT", align="L")
    title_bottom = pdf.get_y()

    sev = (severity or "").upper()
    color = _SEVERITY_COLORS.get(sev, _MUTED)
    pdf.set_xy(pdf.w - _MARGIN - chip_w, y_start + 1)
    pdf.set_fill_color(*color)
    pdf.set_text_color(*_WHITE)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(chip_w, 7, sev or "N/A", align="C", fill=True)
    pdf.set_text_color(*_INK)

    pdf.set_xy(_MARGIN, title_bottom + 3)
    pdf.set_draw_color(*_TEAL)
    pdf.set_line_width(0.8)
    pdf.line(_MARGIN, pdf.get_y(), pdf.w - _MARGIN, pdf.get_y())
    pdf.set_line_width(0.2)
    pdf.ln(5)


def _draw_metadata_table(pdf: _RCAReportPDF, rows):
    # Dynamic label column width -- a fixed 45mm clipped longer labels
    # like "CURRENT VALUE / THRESHOLD" (found via visual inspection
    # before shipping). Measured against the actual bold font used for
    # labels, with a floor/ceiling so one long label can't crush the
    # value column on the other rows.
    pdf.set_font("Helvetica", "B", 9)
    padding = 6
    label_w = max(
        45,
        min(80, max(pdf.get_string_width(label.upper()) for label, _ in rows) + padding),
    )
    value_w = pdf.w - 2 * _MARGIN - label_w
    row_h = 7
    for i, (label, value) in enumerate(rows):
        fill = _ROW_ALT if i % 2 == 0 else _WHITE
        pdf.set_fill_color(*fill)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*_MUTED)
        x0, y0 = pdf.get_x(), pdf.get_y()
        pdf.cell(label_w, row_h, _latin1_safe(label.upper()), fill=True)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(*_INK)
        pdf.set_xy(x0 + label_w, y0)
        pdf.cell(value_w, row_h, _latin1_safe(value), fill=True)
        pdf.set_xy(_MARGIN, y0 + row_h)
    pdf.ln(4)


def _draw_section_header(pdf: _RCAReportPDF, text: str):
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*_INK)
    pdf.multi_cell(0, 8, _strip_bold_markers(text), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.set_draw_color(*_TEAL)
    pdf.set_line_width(0.6)
    pdf.line(_MARGIN, pdf.get_y(), _MARGIN + 30, pdf.get_y())
    pdf.set_line_width(0.2)
    pdf.set_draw_color(*_BORDER)
    pdf.ln(4)


def render_pdf(markdown_text: str, title: str, severity: str = None) -> bytes:
    pdf = _RCAReportPDF(format="A4")
    pdf.alias_nb_pages()
    pdf.set_margins(_MARGIN, _MARGIN, _MARGIN)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    lines = markdown_text.split("\n")

    # Title is always the markdown's leading "# " line -- consume it
    # here so it renders inside the styled title block, not as a plain
    # generic header further down.
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    _draw_title_block(pdf, title, severity)

    # Consume the fixed metadata bullet run (see _METADATA_LINE) into a
    # proper table; everything else falls through to generic rendering.
    metadata_rows = []
    while lines and lines[0].strip() == "":
        lines.pop(0)
    while lines:
        m = _METADATA_LINE.match(lines[0].strip())
        if not m:
            break
        metadata_rows.append((m.group("label"), m.group("value")))
        lines.pop(0)
    if metadata_rows:
        _draw_metadata_table(pdf, metadata_rows)

    generated_footnote_seen = False
    for raw_line in lines:
        line = _latin1_safe(raw_line.rstrip())
        if not line:
            pdf.ln(3)
            continue
        if line.startswith("# "):
            _draw_section_header(pdf, line[2:])
        elif line.startswith("## "):
            _draw_section_header(pdf, line[3:])
        elif line.startswith("- "):
            bullet_text = line[2:]
            link_match = _MD_LINK_BULLET.match(bullet_text)
            if link_match:
                # A References-section entry -- real clickable link
                # (teal, underlined) with the literal URL printed below
                # in small muted text so a printed copy is still usable.
                pdf.set_font("Helvetica", "U", 10)
                pdf.set_text_color(*_TEAL)
                pdf.cell(4, 6, "-")
                pdf.multi_cell(
                    0, 6, _latin1_safe(link_match.group("title")),
                    new_x="LMARGIN", new_y="NEXT", align="L",
                    link=link_match.group("url"),
                )
                pdf.set_font("Helvetica", "", 8)
                pdf.set_text_color(*_MUTED)
                pdf.set_x(_MARGIN + 4)
                pdf.multi_cell(0, 4, _latin1_safe(link_match.group("url")), new_x="LMARGIN", new_y="NEXT", align="L")
                pdf.set_text_color(*_INK)
                pdf.ln(1)
            else:
                pdf.set_font("Helvetica", "", 10)
                pdf.set_text_color(*_TEAL)
                pdf.cell(4, 6, "-")
                pdf.set_text_color(*_INK)
                pdf.multi_cell(0, 6, _strip_bold_markers(bullet_text), new_x="LMARGIN", new_y="NEXT", align="L")
        elif line.startswith("*Generated automatically"):
            generated_footnote_seen = True
            pdf.ln(2)
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(*_MUTED)
            pdf.multi_cell(0, 5, _strip_bold_markers(line.strip("*")), new_x="LMARGIN", new_y="NEXT", align="L")
            pdf.set_text_color(*_INK)
        else:
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 6, _strip_bold_markers(line), new_x="LMARGIN", new_y="NEXT", align="L")

    if not generated_footnote_seen:
        pdf.ln(2)
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(*_MUTED)
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        pdf.multi_cell(0, 5, f"Generated automatically by AurionPro CloudOps on {generated_at} -- verify before external distribution.", align="L")

    output = pdf.output()
    return bytes(output)
