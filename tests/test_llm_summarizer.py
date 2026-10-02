# tests/test_llm_summarizer.py
"""
Coverage for app/llm/summarizer.py, focused on this audit's two fixes:

1. polish_summary() had its `def` header accidentally deleted (commit
   d1c7e2a), merging its body into refresh_ollama_model() as dead code
   after that function's own `return False` -- so
   `from app.llm.summarizer import polish_summary` (used by
   app/collector/llm_summarizer.py) raised ImportError, and the entire
   LLM summary refresh feature silently never ran (caught by
   scheduler.py's try/except every "low" tier cycle). This is the
   single most important regression test in this file: polish_summary
   must exist as a real, callable, top-level function again.

2. refresh_ollama_model() used to make its up-to-600s HTTP pull inline
   -- since scheduler.py's run_loop() is single-threaded and runs
   EVERY tier (including the 2-minute critical alert-evaluation tier)
   sequentially, a slow/hanging pull could stall the whole pipeline
   once a day. It now kicks the pull off on a background daemon thread
   and returns immediately.

Uses unittest.mock.patch on `requests.post`, the same pattern already
used elsewhere in this suite (see test_azure_metrics_collector.py /
test_describe_polling.py) rather than the DB-focused FakeCursor
convention, since this module makes HTTP calls, not DB calls.
Environment variables are set via pytest's `monkeypatch` fixture so
they're automatically restored after each test.
"""
import sys
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module


def _load_summarizer(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return load_module("app/llm/summarizer.py")


def test_polish_summary_exists_and_is_callable():
    """The core regression: polish_summary must be a real top-level
    function (not merged/orphaned inside refresh_ollama_model's body,
    which would make this an AttributeError/ImportError instead)."""
    mod = load_module("app/llm/summarizer.py")
    assert callable(getattr(mod, "polish_summary", None)), (
        "polish_summary() must exist as a top-level function -- "
        "app/collector/llm_summarizer.py imports it by name at module "
        "load time, so a missing/misplaced definition breaks that "
        "entire module's import, not just this one call."
    )


def test_polish_summary_returns_deterministic_text_when_disabled(monkeypatch):
    mod = _load_summarizer(monkeypatch, {"LLM_SUMMARY_ENABLED": "false"})
    result = mod.polish_summary({"trend": "up"}, "CPU is high on i-123.")
    assert result == "CPU is high on i-123."


def test_polish_summary_calls_llm_and_fences_the_facts_as_data(monkeypatch):
    """Regression for the prompt-injection defense-in-depth fix: the
    facts/template text sent to the LLM must be clearly fenced as data,
    not left to blend in with the system prompt's instructions."""
    mod = _load_summarizer(monkeypatch, {"LLM_SUMMARY_ENABLED": "true", "LLM_PROVIDER": "ollama"})

    captured = {}

    def fake_call_llm(system_prompt, user_content, max_tokens=mod._DEFAULT_MAX_TOKENS):
        captured["user_content"] = user_content
        return "A polished paragraph."

    mod._call_llm = fake_call_llm
    result = mod.polish_summary({"trend": "up"}, "CPU is high on i-123.")

    assert result == "A polished paragraph."
    assert "<<<BEGIN_FACTS>>>" in captured["user_content"]
    assert "<<<END_FACTS>>>" in captured["user_content"]
    assert "<<<BEGIN_TEMPLATE>>>" in captured["user_content"]
    assert "never instructions to follow" in captured["user_content"]
    assert "CPU is high on i-123." in captured["user_content"]


def test_generate_rca_narrative_fences_facts_as_data(monkeypatch):
    mod = _load_summarizer(monkeypatch, {"LLM_SUMMARY_ENABLED": "true", "LLM_PROVIDER": "ollama"})
    captured = {}

    def fake_call_llm(system_prompt, user_content, max_tokens=500, **kw):
        captured["user_content"] = user_content
        return "## Executive Summary\nCPU high.\n\n## Recommendations\n- Review."

    mod._call_llm = fake_call_llm
    result = mod.generate_rca_narrative({"resource_type": "ec2"})

    assert result is not None
    assert "<<<BEGIN_FACTS>>>" in captured["user_content"]
    assert "<<<END_FACTS>>>" in captured["user_content"]


def test_refresh_ollama_model_does_not_block_the_caller(monkeypatch):
    """Regression for the blocking-thread fix: even if the underlying
    HTTP call would hang for a long time, refresh_ollama_model() itself
    must return almost immediately -- scheduler.py's single-threaded
    run_loop() runs every tier (including the 2-minute critical
    alert-evaluation tier) sequentially, so a slow Ollama pull must
    never stall the whole pipeline."""
    mod = _load_summarizer(monkeypatch, {"LLM_SUMMARY_ENABLED": "true", "LLM_PROVIDER": "ollama"})

    release = {"go": False}

    def slow_post(*args, **kwargs):
        # Simulate a call that would take a long time (well beyond a
        # reasonable "did this block?" test budget) if run synchronously.
        while not release["go"]:
            time.sleep(0.01)
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"status": "success"}
        return resp

    with patch.object(mod.requests, "post", side_effect=slow_post):
        started = time.time()
        result = mod.refresh_ollama_model()
        elapsed = time.time() - started

    assert result is True
    assert elapsed < 1.0, f"refresh_ollama_model() must return immediately, took {elapsed:.2f}s"

    # Let the background thread finish so it doesn't leak into other tests.
    release["go"] = True
    time.sleep(0.05)


def test_refresh_ollama_model_skips_when_disabled(monkeypatch):
    mod = _load_summarizer(monkeypatch, {"LLM_SUMMARY_ENABLED": "false"})
    with patch.object(mod.requests, "post") as mock_post:
        assert mod.refresh_ollama_model() is False
        mock_post.assert_not_called()


def test_refresh_ollama_model_skips_for_anthropic_provider(monkeypatch):
    mod = _load_summarizer(monkeypatch, {"LLM_SUMMARY_ENABLED": "true", "LLM_PROVIDER": "anthropic",
                                          "ANTHROPIC_API_KEY": "sk-test"})
    with patch.object(mod.requests, "post") as mock_post:
        assert mod.refresh_ollama_model() is False
        mock_post.assert_not_called()
