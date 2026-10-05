# app/llm/rca_report_pdf.py
"""
Renders app/llm/rca_report.py's markdown RCA report as a downloadable, branded PDF.

Deliberately not a full markdown engine. The report markdown only ever uses '#'/'##' headers, '-' bullets, '**bold**'
inline, a fixed run of "- **Label:** value" metadata lines right after the title, "**Lead-in.** text" summary
paragraphs, "- **time** - event" timeline bullets and "- [Title](url)" reference links. Each is handled explicitly.

All drawing lives in app/pdf_kit.py, which the weekly / monthly / quarterly reports use too, so the two kinds of report
look like one product.
"""
import re
from datetime import datetime, timezone

from app import pdf_kit as kit
from app.pdf_kit import latin1_safe as _latin1_safe, strip_bold as _strip_bold_markers  # noqa: F401  (re-exported)

# Rows the PDF leaves out of its details table because the title chips, key-figures strip and footnote already show them
# (the Markdown download keeps every row).
_PDF_SKIP_ROWS = {"alert", "severity", "status", "reading vs limit", "report generated"}

_METADATA_LINE = re.compile(r"^- \*\*(?P<label>[^*]+):\*\* (?P<value>.*)$")
_MD_LINK_BULLET = re.compile(r"^\[(?P<title>[^\]]+)\]\((?P<url>https?://[^)]+)\)$")
_LEAD_IN = re.compile(r"^\*\*(?P<lead>[^*]+?)\.\*\*\s+(?P<rest>.+)$")
_TIMELINE_BULLET = re.compile(r"^- \*\*(?P<time>[^*]+)\*\* \u2014 (?P<event>.+)$")


def _reference_link(pdf, title, url):
    kit.ensure_room(pdf, 14)
    y = pdf.get_y()
    kit.bullet_dot(pdf, y + 3)
    pdf.set_xy(kit.MARGIN + 6, y)
    pdf.set_font("Helvetica", "U", 10)
    pdf.set_text_color(*kit.TEAL_DARK)
    pdf.multi_cell(0, 6, _latin1_safe(title), new_x="LMARGIN", new_y="NEXT", align="L", link=url)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*kit.MUTED)
    pdf.set_x(kit.MARGIN + 6)
    pdf.multi_cell(0, 4, _latin1_safe(url), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.set_text_color(*kit.INK)
    pdf.ln(1.5)


def render_pdf(markdown_text: str, title: str, severity: str = None, *, subtitle: str = None, status: str = None,
               kpis: list = None, alert_ref: str = None) -> bytes:
    pdf = kit.new_document(
        band_subtitle="Root Cause Analysis Report", band_right=alert_ref or "", footer_ref=alert_ref or "",
        title=f"RCA Report - {title}", subject="Root cause analysis" + (f" - {alert_ref}" if alert_ref else ""),
        keywords="RCA, root cause analysis, alert, CloudOps",
    )
    now = datetime.now(timezone.utc)

    lines = markdown_text.split("\n")
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    sev = (severity or "").upper() or "N/A"
    chips = [(sev, kit.SEVERITY_COLORS.get(sev, kit.MUTED))]
    if status:
        chips.append((status.upper(), kit.STATUS_COLORS.get(status.upper(), kit.MUTED)))
    kit.title_block(pdf, title, chips, subtitle)
    kit.kpi_strip(pdf, kpis, severity)

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
        kit.details_table(pdf, shown_rows)

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
            kit.section_header(pdf, section)
            after_header = True
            continue
        after_header = False
        if line.startswith("Note: "):
            kit.banner(pdf, line)
        elif line.startswith("- "):
            tl = _TIMELINE_BULLET.match(line)
            link = _MD_LINK_BULLET.match(line[2:])
            if tl and section.lower() == "timeline":
                prev_dot_y = kit.timeline_event(pdf, tl.group("time"), tl.group("event"), prev_dot_y)
            elif link:
                _reference_link(pdf, link.group("title"), link.group("url"))
            else:
                kit.bullet(pdf, line[2:])
        elif line.startswith("*Generated automatically"):
            generated_footnote_seen = True
            note = line.strip("*")
            if generated_stamp:
                note += f" Generated {generated_stamp}."
            kit.footnote(pdf, note)
        else:
            m = _LEAD_IN.match(line)
            if m:
                kit.paragraph(pdf, m.group("lead"), m.group("rest"))
            else:
                kit.plain_paragraph(pdf, line)

    if not generated_footnote_seen:
        kit.footnote(pdf, f"Generated automatically by AurionPro CloudOps on {now:%d %b %Y, %H:%M UTC}. "
                          f"Verify before external distribution.")
    return bytes(pdf.output())
