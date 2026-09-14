# app/llm/postmortem_pdf.py
"""
Renders app/llm/postmortem.py's markdown postmortem as a downloadable
PDF, using fpdf2 (pure-Python, no compiled system dependency). Simple,
deliberately not a full markdown-to-PDF engine -- this app's
postmortem markdown only ever uses '#'/'##' headers, '-' bullets, and
'**bold**' inline, all handled explicitly below rather than pulling in
a general markdown-rendering dependency for three constructs.
"""
import re

from fpdf import FPDF

_MARGIN = 15


_UNICODE_REPLACEMENTS = {
    "\u2022": "-",   # •
    "\u2014": "--",  # —
    "\u2013": "-",   # –
    "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"',
    "\u2026": "...",
}


def _latin1_safe(text: str) -> str:
    """fpdf2's core Helvetica font only supports latin-1 -- this app's
    generated text (postmortem.py's timeline lines, LLM narrative
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
    # helper -- for a postmortem's supporting prose, stripping the
    # ** markers and keeping the words is a fine tradeoff over pulling
    # in a heavier PDF layout engine for real inline bold.
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text)


def render_pdf(markdown_text: str, title: str) -> bytes:
    pdf = FPDF(format="A4")
    pdf.set_margins(_MARGIN, _MARGIN, _MARGIN)
    pdf.set_auto_page_break(auto=True, margin=_MARGIN)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(0, 10, _latin1_safe(title), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    for raw_line in markdown_text.split("\n"):
        line = _latin1_safe(raw_line.rstrip())
        if not line:
            pdf.ln(3)
            continue
        if line.startswith("# "):
            pdf.set_font("Helvetica", "B", 14)
            pdf.multi_cell(0, 9, _strip_bold_markers(line[2:]), new_x="LMARGIN", new_y="NEXT")
        elif line.startswith("## "):
            pdf.set_font("Helvetica", "B", 12)
            pdf.ln(2)
            pdf.multi_cell(0, 8, _strip_bold_markers(line[3:]), new_x="LMARGIN", new_y="NEXT")
        elif line.startswith("- "):
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 6, f"- {_strip_bold_markers(line[2:])}", new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 6, _strip_bold_markers(line), new_x="LMARGIN", new_y="NEXT")

    output = pdf.output()
    return bytes(output)
