# app/help_content/blocks.py
"""Block constructors for the in-app Help Center. Pure data, no imports beyond the stdlib.

A block is a plain dict: {"type": "heading"|"paragraph"|"list"|"table"|"note"|"code"|"link", ...}.
Any block may carry "unverified": True -- the behaviour could not be confirmed in code, and the page shows
an "unverified" badge on it. Every such block is also listed by help_content.unverified_blocks().
"""


def _u(block, unverified):
    if unverified:
        block["unverified"] = True
    return block


def h(text, level=3):
    return {"type": "heading", "text": text, "level": level}


def p(text, unverified=False):
    return _u({"type": "paragraph", "text": text}, unverified)


def ul(items, ordered=False, unverified=False):
    return _u({"type": "list", "ordered": bool(ordered), "items": list(items)}, unverified)


def table(columns, rows, unverified=False):
    return _u({"type": "table", "columns": list(columns), "rows": [[str(c) for c in r] for r in rows]}, unverified)


def note(text, tone="info", unverified=False):
    return _u({"type": "note", "tone": tone, "text": text}, unverified)


def code(text, unverified=False):
    return _u({"type": "code", "text": text}, unverified)


def link(text, href, unverified=False):
    return _u({"type": "link", "text": text, "href": href}, unverified)


def section(sid, title, group, audience, requires, blocks, summary=""):
    return {"id": sid, "title": title, "group": group, "audience": audience,
            "requires": requires, "summary": summary, "blocks": blocks}


def fmt_interval(seconds):
    if not seconds:
        return "-"
    if seconds % 86400 == 0:
        return f"{seconds // 86400} day" + ("s" if seconds > 86400 else "")
    if seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} s"
