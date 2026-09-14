# app/llm/summarizer.py
"""
AIOps roadmap #13/#15 -- hosted-LLM summary polishing (2026-09-14).

WHAT THIS DOES: takes the DETERMINISTIC, already-correct summary
app/collector/rca.py's explain_alert() builds from real gathered
signals (CloudTrail events, audit_logs, topology, trend, flapping,
related alerts -- see rca.py's own docstring) and asks a hosted LLM to
rewrite it as one fluent paragraph. The LLM is used PURELY for
PRESENTATION -- it never sees raw database rows, never gathers its own
signals, and is explicitly instructed not to add any fact not already
in the input. If it does anything else (times out, errors, returns
something that looks like it invented a detail), the caller keeps the
original deterministic template text -- this module can only ever
IMPROVE phrasing, never introduce a wrong fact into a customer-facing
alert explanation.

WHY HOSTED, NOT LOCAL (Ollama/open-weight) FOR THIS PHASE:
the roadmap doc itself flags local-LLM (#13) as "the first item in the
whole roadmap with a genuine 'is our hardware enough' question" -- this
app's dev/prod boxes are sized for a FastAPI app + MySQL + a Python
collector, not for hosting an LLM's memory/compute footprint alongside
that, and answering the hardware question needs real capacity data this
module can't get from here. The hosted path (#15) needs zero new
infrastructure, costs a small amount per call (fewer than one call per
alert per background cycle -- see app/collector/llm_summarizer.py's
"only regenerate if the source facts changed" caching), and can be
swapped for a local model later behind this same function signature
without touching any caller.

OFF BY DEFAULT: LLM_SUMMARY_ENABLED must be explicitly set to "true" in
.env, and ANTHROPIC_API_KEY must be set, or every call below is a
no-op. Nothing about this feature runs, costs money, or makes a single
network call unless BOTH are explicitly configured.
"""
import hashlib
import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"  # fast/cheap -- summary polishing, not analysis
_DEFAULT_TIMEOUT_SECONDS = 8
_DEFAULT_MAX_TOKENS = 220

_SYSTEM_PROMPT = (
    "You rewrite pre-verified operational facts into one fluent, concise paragraph "
    "(2-4 sentences) for a cloud-operations engineer reading an alert explanation. "
    "STRICT RULES: (1) Do not introduce any fact, number, name, resource ID, or "
    "timestamp that is not already present in the input facts. (2) Do not speculate "
    "or guess beyond what the input states. (3) If the input facts are sparse, write "
    "a short paragraph rather than inventing additional detail. (4) Do not use the "
    "words 'incident' or internal jargon like 'topology in-degree' -- this is "
    "customer-facing. (5) Output ONLY the rewritten paragraph, no preamble, no "
    "markdown, no quotation marks around it."
)


def is_enabled() -> bool:
    return (
        os.getenv("LLM_SUMMARY_ENABLED", "false").strip().lower() == "true"
        and bool(os.getenv("ANTHROPIC_API_KEY"))
    )


def source_hash(deterministic_summary: str) -> str:
    """Stable hash of the deterministic summary text -- used as the
    cache-invalidation key in app/collector/llm_summarizer.py /
    alerts.llm_summary_source_hash (migration 030). If rca.py's gathered
    signals ever change what the deterministic summary says, this hash
    changes too, and the stale cached LLM paragraph is no longer served."""
    return hashlib.sha256(deterministic_summary.encode("utf-8")).hexdigest()


def polish_summary(facts: dict, deterministic_summary: str) -> str:
    """
    Returns a polished paragraph, or the ORIGINAL deterministic_summary
    unchanged if the feature is disabled, misconfigured, or the API
    call fails/times out/returns something unusable for any reason.
    Never raises -- every caller can treat this as a drop-in
    replacement for the plain deterministic string.

    `facts` should be a small, already-verified JSON-serializable dict
    (e.g. {"probable_trigger": ..., "trend": ..., "related_alert_count": ...,
    "is_likely_flapping": bool, "in_degree": int}) -- the same signals
    rca.py already gathered, not raw database rows.
    """
    if not is_enabled():
        return deterministic_summary

    api_key = os.getenv("ANTHROPIC_API_KEY")
    model = os.getenv("LLM_SUMMARY_MODEL", _DEFAULT_MODEL)
    timeout = float(os.getenv("LLM_SUMMARY_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS))
    max_tokens = int(os.getenv("LLM_SUMMARY_MAX_TOKENS", _DEFAULT_MAX_TOKENS))

    user_content = (
        "Input facts (JSON):\n"
        f"{json.dumps(facts, default=str)}\n\n"
        "Existing plain-template summary (rewrite this, do not add anything new):\n"
        f"{deterministic_summary}"
    )

    try:
        response = requests.post(
            _API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_content}],
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        text_blocks = [
            block.get("text", "") for block in data.get("content", [])
            if block.get("type") == "text"
        ]
        polished = "".join(text_blocks).strip()
        if not polished:
            logger.warning("[llm_summarizer] empty response from LLM, keeping template summary")
            return deterministic_summary
        return polished

    except requests.exceptions.Timeout:
        logger.warning(f"[llm_summarizer] timed out after {timeout}s, keeping template summary")
        return deterministic_summary
    except requests.exceptions.RequestException as e:
        logger.warning(f"[llm_summarizer] API call failed ({e}), keeping template summary")
        return deterministic_summary
    except (KeyError, ValueError, TypeError) as e:
        logger.warning(f"[llm_summarizer] unexpected response shape ({e}), keeping template summary")
        return deterministic_summary
