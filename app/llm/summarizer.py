# app/llm/summarizer.py
"""
AIOps roadmap #13/#15 -- LLM summary polishing (2026-09-15, switched to
a free local default after the person confirmed they don't want any
paid API calls).

WHAT THIS DOES: takes the DETERMINISTIC, already-correct summary
app/collector/rca.py's explain_alert() builds from real gathered
signals (CloudTrail events, audit_logs, topology, trend, flapping,
related alerts -- see rca.py's own docstring) and asks an LLM to
rewrite it as one fluent paragraph. The LLM is used PURELY for
PRESENTATION -- it never sees raw database rows, never gathers its own
signals, and is explicitly instructed not to add any fact not already
in the input. If it does anything else (times out, errors, returns
something that looks unusable), the caller keeps the original
deterministic template text -- this module can only ever IMPROVE
phrasing, never introduce a wrong fact into a customer-facing alert
explanation.

TWO PROVIDERS, chosen via LLM_PROVIDER in .env:
  - "ollama" (DEFAULT, genuinely free forever): a self-hosted model
    running on this app's own server via Ollama
    (https://ollama.com -- open source, Apache 2.0, no account, no
    API key, no per-call cost of any kind). See the "OLLAMA SETUP"
    section below for exact install steps -- this is the one part of
    switching to free that has a real prerequisite: a model has to
    actually be running locally for this to produce anything.
  - "anthropic" (optional, real per-call cost): kept as an alternative
    for later IF the person ever wants hosted quality over local/free
    -- never used unless LLM_PROVIDER is explicitly set to it AND
    ANTHROPIC_API_KEY is set. Not the default; costs money.

OLLAMA SETUP (do this once, on whichever box runs this app):
    curl -fsSL https://ollama.com/install.sh | sh
    ollama pull qwen3:4b
    sudo systemctl enable --now ollama
That's the whole cost: a one-time download and some disk/RAM, never a
bill. As of 2026-09-15, Qwen3:4b is the best all-round pick for a
CPU-only box with 8GB+ RAM free; on tighter RAM (~4GB), pull
`phi4-mini` instead and set OLLAMA_MODEL=phi4-mini -- this changes
every few months as new models ship, so re-check before assuming this
comment is still current. Swap models anytime with OLLAMA_MODEL=<name>
in .env -- no code change needed.

AUTO-REFRESH, NOT AUTO-UPGRADE: refresh_ollama_model() below runs once
a day (scheduler.py's slow_extended tier) and re-pulls whatever model
OLLAMA_MODEL names -- if the publisher has pushed new weights to that
exact tag, this picks them up automatically, with zero intervention.
It deliberately does NOT switch to a newer/different model
automatically (e.g. moving qwen3:4b -> qwen3:8b on its own) -- a
different model could need more RAM than the box has, or change every
alert explanation's tone with no review. Changing which model this
uses is a one-line .env edit + restart, same as any other config
change in this app.

OFF BY DEFAULT: LLM_SUMMARY_ENABLED must be explicitly set to "true"
in .env, or every call below is a no-op regardless of provider. With
LLM_PROVIDER=ollama (the default), enabling this costs nothing ever,
no matter how many alerts/postmortems it processes -- there's no
metering to think about, unlike the old Anthropic path.
"""
import hashlib
import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

# ── Ollama (free, local, default) ────────────────────────────────────
_OLLAMA_DEFAULT_HOST = "http://localhost:11434"
_OLLAMA_DEFAULT_MODEL = "qwen3:4b"

# ── Anthropic (optional, paid, opt-in only) ──────────────────────────
_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

_DEFAULT_TIMEOUT_SECONDS = 20  # local inference on modest CPU hardware is slower than a hosted API
_DEFAULT_MAX_TOKENS = 220

_SUMMARY_SYSTEM_PROMPT = (
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

_POSTMORTEM_SYSTEM_PROMPT = (
    "You write the Executive Summary and Recommendations sections of an "
    "incident postmortem document for a cloud-operations team. STRICT RULES: "
    "(1) Do not introduce any fact, number, name, resource ID, or timestamp "
    "not already present in the input JSON. (2) Do not speculate about root "
    "cause beyond what the input's probable_trigger/recent_deployment/trend "
    "fields state -- if those are empty, say the cause is undetermined. "
    "(3) Recommendations must be concrete and directly tied to facts present "
    "in the input (e.g. only recommend a deploy-process change if "
    "recent_deployment is non-null). (4) Output ONLY these two sections as "
    "markdown, in this exact format, no other text:\n\n"
    "## Executive Summary\n<2-4 sentences>\n\n## Recommendations\n<2-4 bullet points>"
)


def _provider() -> str:
    return os.getenv("LLM_PROVIDER", "ollama").strip().lower()


def is_enabled() -> bool:
    if os.getenv("LLM_SUMMARY_ENABLED", "false").strip().lower() != "true":
        return False
    if _provider() == "anthropic":
        return bool(os.getenv("ANTHROPIC_API_KEY"))
    return True  # ollama needs no API key -- just a reachable local server, checked at call time


def source_hash(deterministic_summary: str) -> str:
    """Stable hash of the deterministic summary text -- used as the
    cache-invalidation key in app/collector/llm_summarizer.py /
    alerts.llm_summary_source_hash (migration 030). If rca.py's gathered
    signals ever change what the deterministic summary says, this hash
    changes too, and the stale cached LLM paragraph is no longer served."""
    return hashlib.sha256(deterministic_summary.encode("utf-8")).hexdigest()


def _call_ollama(system_prompt: str, user_content: str, timeout: float) -> str:
    """Raises on any failure -- caller (_call_llm) handles fallback.
    Ollama's /api/chat mirrors the OpenAI/Anthropic chat-message shape
    closely enough that this reuses the same system+user prompt
    strings the Anthropic path already built."""
    host = os.getenv("OLLAMA_HOST", _OLLAMA_DEFAULT_HOST).rstrip("/")
    model = os.getenv("OLLAMA_MODEL", _OLLAMA_DEFAULT_MODEL)
    response = requests.post(
        f"{host}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    return (data.get("message", {}) or {}).get("content", "").strip()


def _call_anthropic(system_prompt: str, user_content: str, model: str, max_tokens: int, timeout: float) -> str:
    """Raises on any failure -- caller (_call_llm) handles fallback."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    response = requests.post(
        _ANTHROPIC_API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
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
    return "".join(text_blocks).strip()


def _call_llm(system_prompt: str, user_content: str, max_tokens: int = _DEFAULT_MAX_TOKENS) -> str:
    """
    Single dispatch point for both polish_summary() and
    generate_postmortem_narrative() below -- picks the provider from
    LLM_PROVIDER, calls it, and returns "" (never raises, never None)
    on ANY failure so both callers can use the same
    "empty string means fall back" check regardless of which provider
    is configured or how it failed (timeout, connection refused
    because Ollama isn't running, bad response shape, etc).
    """
    timeout = float(os.getenv("LLM_SUMMARY_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS))
    provider = _provider()
    try:
        if provider == "anthropic":
            model = os.getenv("LLM_SUMMARY_MODEL", _ANTHROPIC_DEFAULT_MODEL)
            return _call_anthropic(system_prompt, user_content, model, max_tokens, timeout)
        else:
            return _call_ollama(system_prompt, user_content, timeout)
    except requests.exceptions.ConnectionError as e:
        if provider != "anthropic":
            logger.warning(
                f"[llm_summarizer] could not reach Ollama at "
                f"{os.getenv('OLLAMA_HOST', _OLLAMA_DEFAULT_HOST)} ({e}) -- is it installed and "
                f"running? See app/llm/summarizer.py's module docstring for setup steps. "
                f"Falling back to the template summary this cycle."
            )
        else:
            logger.warning(f"[llm_summarizer] connection failed ({e}), keeping template summary")
        return ""
    except requests.exceptions.Timeout:
        logger.warning(f"[llm_summarizer] timed out after {timeout}s, keeping template summary")
        return ""
    except requests.exceptions.RequestException as e:
        logger.warning(f"[llm_summarizer] request failed ({e}), keeping template summary")
        return ""
    except (KeyError, ValueError, TypeError) as e:
        logger.warning(f"[llm_summarizer] unexpected response shape ({e}), keeping template summary")
        return ""


def refresh_ollama_model() -> bool:
    """
    Re-pulls the currently-configured OLLAMA_MODEL tag once a day (see
    scheduler.py's slow_extended tier hook) -- Ollama's own pull
    endpoint is a no-op download-wise if the tag hasn't actually
    changed server-side, so this is safe to call daily regardless of
    whether the model publisher has actually pushed an update.

    DELIBERATELY DOES NOT switch to a different model family or a
    newer major version automatically -- only refreshes the SAME
    named tag the person chose in .env (e.g. re-pulling "qwen3:4b"
    picks up new weights if Alibaba pushes an update to that exact
    tag, but never silently moves someone from "qwen3:4b" to
    "qwen3:8b" or a different model entirely). A full auto-upgrade-to-
    whatever-is-newest policy is a genuine production risk for this
    feature -- a bigger/different model could need more RAM than the
    box has, or change the tone of every alert explanation with zero
    review -- so this intentionally stops short of that. Changing
    OLLAMA_MODEL to a different model is a deliberate one-line .env
    edit + restart, same as any other config change in this app.
    """
    if not is_enabled() or _provider() != "ollama":
        return False

    host = os.getenv("OLLAMA_HOST", _OLLAMA_DEFAULT_HOST).rstrip("/")
    model = os.getenv("OLLAMA_MODEL", _OLLAMA_DEFAULT_MODEL)
    try:
        response = requests.post(
            f"{host}/api/pull",
            json={"model": model, "stream": False},
            timeout=600,  # a real re-download can take minutes on a slow link; this runs once/day, off the request path
        )
        response.raise_for_status()
        status = (response.json() or {}).get("status", "unknown")
        logger.info(f"[llm_summarizer] refreshed Ollama model '{model}': {status}")
        return True
    except requests.exceptions.ConnectionError:
        logger.warning(
            f"[llm_summarizer] could not reach Ollama at {host} to refresh model '{model}' "
            f"(non-fatal, today's cached weights keep working either way)"
        )
        return False
    except Exception as e:
        logger.warning(f"[llm_summarizer] Ollama model refresh failed (non-fatal): {e}")
        return False
    """
    Returns a polished paragraph, or the ORIGINAL deterministic_summary
    unchanged if the feature is disabled, misconfigured, or the call
    fails/times out/returns something unusable for any reason. Never
    raises -- every caller can treat this as a drop-in replacement for
    the plain deterministic string.

    `facts` should be a small, already-verified JSON-serializable dict
    (e.g. {"probable_trigger": ..., "trend": ..., "related_alert_count": ...,
    "is_likely_flapping": bool, "in_degree": int}) -- the same signals
    rca.py already gathered, not raw database rows.
    """
    if not is_enabled():
        return deterministic_summary

    max_tokens = int(os.getenv("LLM_SUMMARY_MAX_TOKENS", _DEFAULT_MAX_TOKENS))
    user_content = (
        "Input facts (JSON):\n"
        f"{json.dumps(facts, default=str)}\n\n"
        "Existing plain-template summary (rewrite this, do not add anything new):\n"
        f"{deterministic_summary}"
    )
    polished = _call_llm(_SUMMARY_SYSTEM_PROMPT, user_content, max_tokens)
    return polished or deterministic_summary


def generate_postmortem_narrative(facts: dict) -> str:
    """
    Used by app/llm/postmortem.py -- writes the "Executive Summary" and
    "Recommendations" prose sections of a downloadable postmortem
    document. Everything else in a generated postmortem (the timeline
    table, resource/severity/duration fields) is assembled
    DETERMINISTICALLY by postmortem.py from real rows, never touched by
    this function -- this is scoped ONLY to prose, under the exact same
    fact-grounding system prompt discipline as polish_summary() above.

    Returns None (not a fallback string) on any failure -- the CALLER
    decides what to show instead (postmortem.py falls back to a plain
    bullet-point rendering of the same facts), since "no narrative
    available" reads differently in a formal document than it does in
    a one-line alert explanation.
    """
    if not is_enabled():
        return None
    narrative = _call_llm(_POSTMORTEM_SYSTEM_PROMPT, json.dumps(facts, default=str), max_tokens=500)
    return narrative or None
