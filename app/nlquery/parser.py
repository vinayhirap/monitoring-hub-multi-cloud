# app/nlquery/parser.py
"""
AIOps roadmap #11 -- natural-language dashboard search, PARSING half
(2026-09-14).

DELIBERATE DESIGN CHOICE: this is a rule-based keyword/entity extractor,
NOT a sentence-transformers/embedding-similarity search, even though
the roadmap doc's original sketch mentioned embeddings. Reasoning:

  - The query vocabulary here is genuinely small and closed: severity
    (3 values), status (2 values), a resource-type list pulled live
    from this account's own `resources` table, a handful of relative
    time phrases, and free-text that falls through to a plain SQL LIKE
    over resource name/id. Embedding similarity is the right tool when
    the space of things being matched is open-ended/fuzzy (e.g.
    "find me docs about onboarding") -- it's overkill, and genuinely
    worse, for a closed enum-like filter set where exact/substring
    matching is both correct AND fully explainable ("matched on the
    word 'critical'" beats "matched with 0.83 cosine similarity" for
    an ops person deciding whether to trust the result).
  - sentence-transformers pulls in PyTorch, a multi-hundred-MB
    dependency with real CPU/RAM overhead per inference call, onto a
    production box whose spare capacity for a NEW always-on service is
    unknown (the same "is our hardware enough" question the roadmap
    doc flags for local-LLM summarization) -- for a feature this
    narrow in scope, that cost isn't justified.
  - Zero cold-start: no index to build/maintain, no embedding model to
    download/version, works identically the moment this ships.

This keeps the door open to a real embedding-based upgrade later if the
query vocabulary genuinely grows past what enum-matching can express
(e.g. "resources like the one that broke last Tuesday") -- but that's
a Phase-2 problem, not a Phase-1 one.
"""
import re
from datetime import datetime, timedelta

SEVERITY_KEYWORDS = {
    "critical": "CRITICAL", "crit": "CRITICAL", "severe": "CRITICAL",
    "warning": "WARNING", "warn": "WARNING",
    "info": "INFO", "informational": "INFO",
}

STATUS_KEYWORDS = {
    "active": "active", "open": "active", "firing": "active", "ongoing": "active",
    "resolved": "resolved", "closed": "resolved", "fixed": "resolved",
}

# hour(s)/day(s)/week(s)/minute(s) --> minutes multiplier
_UNIT_MINUTES = {
    "minute": 1, "min": 1,
    "hour": 60, "hr": 60,
    "day": 60 * 24,
    "week": 60 * 24 * 7,
}

_RELATIVE_PHRASES = {
    "today": 60 * 24,
    "right now": 15,
    "just now": 15,
    "this hour": 60,
    "this week": 60 * 24 * 7,
    "yesterday": 60 * 24 * 2,  # includes today's window for simplicity
}

_N_UNIT_RE = re.compile(
    r"\blast\s+(\d+)\s*(minute|min|hour|hr|day|week)s?\b", re.IGNORECASE
)
_SINGLE_UNIT_RE = re.compile(
    r"\blast\s+(minute|min|hour|hr|day|week)s?\b", re.IGNORECASE
)


def _extract_time_window_minutes(text: str):
    """Returns a lookback window in minutes, or None if the query
    doesn't mention a time constraint at all (caller then applies no
    time filter -- matches "show me all critical alerts" with no
    implied recency)."""
    lowered = text.lower()

    m = _N_UNIT_RE.search(lowered)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        return n * _UNIT_MINUTES[unit]

    m = _SINGLE_UNIT_RE.search(lowered)
    if m:
        return _UNIT_MINUTES[m.group(1)]

    for phrase, minutes in _RELATIVE_PHRASES.items():
        if phrase in lowered:
            return minutes

    return None


def _extract_severity(text: str):
    lowered = text.lower()
    for kw, val in SEVERITY_KEYWORDS.items():
        if re.search(rf"\b{kw}\b", lowered):
            return val
    return None


def _extract_status(text: str):
    lowered = text.lower()
    for kw, val in STATUS_KEYWORDS.items():
        if re.search(rf"\b{kw}\b", lowered):
            return val
    return None


def _extract_resource_type(text: str, known_resource_types: list):
    """Matches against resource_type strings actually present in THIS
    account's own resources table (passed in by the caller, see
    app/nlquery/search.py) -- deliberately not a hardcoded cross-cloud
    vocabulary list, since the accurate set differs per account/provider
    mix and a stale hardcoded list would silently stop matching new
    resource types this app starts supporting later."""
    lowered = text.lower()
    for rtype in known_resource_types:
        if re.search(rf"\b{re.escape(rtype.lower())}\b", lowered):
            return rtype
    return None


# Words stripped out of the leftover free-text search before it's used
# as a LIKE pattern against resource name/id -- otherwise a query like
# "critical alerts on prod rds in the last hour" would literally search
# for a resource named "the last hour".
_STOPWORDS = {
    "show", "me", "find", "get", "list", "all", "any", "alerts", "alert",
    "incidents", "incident", "on", "for", "in", "the", "a", "an", "of",
    "with", "and", "or", "is", "are", "was", "were", "from", "to",
    "last", "this", "right", "now", "just",
} | set(SEVERITY_KEYWORDS) | set(STATUS_KEYWORDS) | {
    u + "s" for u in _UNIT_MINUTES
} | set(_UNIT_MINUTES) | {
    word for phrase in _RELATIVE_PHRASES for word in phrase.split()
}


def _extract_free_text(text: str, consumed_resource_type: str = None) -> str:
    tokens = re.findall(r"[a-zA-Z0-9._-]+", text.lower())
    leftover = [
        t for t in tokens
        if t not in _STOPWORDS
        and not t.isdigit()
        and t != (consumed_resource_type or "").lower()
    ]
    return " ".join(leftover).strip()


def parse_query(text: str, known_resource_types: list) -> dict:
    """
    Parses a free-text query into a structured filter dict:
        {
          "severity": "CRITICAL" | "WARNING" | "INFO" | None,
          "status": "active" | "resolved" | None,
          "resource_type": str | None,
          "since_minutes": int | None,
          "free_text": str,           # leftover tokens, for a LIKE match
          "interpreted_as": str,      # human-readable echo of what matched
        }
    known_resource_types should be the DISTINCT resource_type values
    visible to the current caller (see app/nlquery/search.py) so this
    never claims to filter on a resource type the caller can't
    actually see or that doesn't exist in this deployment.
    """
    text = (text or "").strip()
    severity = _extract_severity(text)
    status = _extract_status(text)
    resource_type = _extract_resource_type(text, known_resource_types)
    since_minutes = _extract_time_window_minutes(text)
    free_text = _extract_free_text(text, resource_type)

    parts = []
    if severity:
        parts.append(f"severity = {severity}")
    if status:
        parts.append(f"status = {status}")
    if resource_type:
        parts.append(f"resource type = {resource_type}")
    if since_minutes:
        if since_minutes >= 60 * 24:
            parts.append(f"within the last {since_minutes // (60 * 24)} day(s)")
        elif since_minutes >= 60:
            parts.append(f"within the last {since_minutes // 60} hour(s)")
        else:
            parts.append(f"within the last {since_minutes} minute(s)")
    if free_text:
        parts.append(f"matching \"{free_text}\"")

    interpreted_as = "; ".join(parts) if parts else "no recognized filters -- showing recent alerts"

    return {
        "severity": severity,
        "status": status,
        "resource_type": resource_type,
        "since_minutes": since_minutes,
        "free_text": free_text,
        "interpreted_as": interpreted_as,
    }
