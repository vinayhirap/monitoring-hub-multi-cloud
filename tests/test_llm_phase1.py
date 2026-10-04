"""
tests/test_llm_phase1.py -- AI/ML audit Phase 1 (2026-10-02).

Covers: Ollama options actually sent (num_predict/num_ctx/temperature; keep_alive only when
configured), truncated-output trimming, the deterministic grounding verifier on polish_summary
and generate_rca_narrative, per-alert commits in refresh_llm_summaries(), and the cached,
non-blocking RCA narrative in app/llm/rca_report.py.
"""
import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
# rca_report.py imports app.metric_labels (a real module). Import the real package first so the stubs the tests install
# for app.db / app.llm.* do not turn `app` into a path-less namespace (same idiom as the audit tests).
import app  # noqa: E402,F401
import app.metric_labels  # noqa: E402,F401
from conftest import load_module, install_stub, FakeConn, FakeCursor, contains  # noqa: E402

FACTS = {"metric_name": "CPUUtilization", "resource_id": "i-0abc1234", "current_value": 91.3,
         "threshold": 80, "template_summary": "CPU on i-0abc1234 is 91.3 against 80."}


def _summ(monkeypatch, **env):
    env.setdefault("LLM_SUMMARY_ENABLED", "true")
    env.setdefault("LLM_PROVIDER", "ollama")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return load_module("app/llm/summarizer.py")


def _resp(content, done_reason="stop"):
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = {"message": {"content": content}, "done_reason": done_reason}
    return r


# ── Ollama request shape ─────────────────────────────────────────
def test_ollama_payload_bounds_output_and_context(monkeypatch):
    mod = _summ(monkeypatch)
    monkeypatch.delenv("OLLAMA_KEEP_ALIVE", raising=False)
    with patch.object(mod.requests, "post", return_value=_resp("ok.")) as post:
        mod._call_ollama("sys", "user", 20, 160)
    payload = post.call_args.kwargs["json"]
    assert payload["think"] is False and payload["stream"] is False
    assert payload["options"]["num_predict"] == 160
    assert payload["options"]["num_ctx"] == 3072
    assert payload["options"]["temperature"] == 0.2
    assert "keep_alive" not in payload  # Ollama's own default unless configured


def test_ollama_keep_alive_only_when_configured(monkeypatch):
    mod = _summ(monkeypatch, OLLAMA_KEEP_ALIVE="15m", OLLAMA_NUM_CTX="2048")
    with patch.object(mod.requests, "post", return_value=_resp("ok.")) as post:
        mod._call_ollama("sys", "user", 20, 100)
    payload = post.call_args.kwargs["json"]
    assert payload["keep_alive"] == "15m"
    assert payload["options"]["num_ctx"] == 2048


def test_call_llm_passes_max_tokens_to_ollama(monkeypatch):
    mod = _summ(monkeypatch)
    with patch.object(mod.requests, "post", return_value=_resp("ok.")) as post:
        assert mod._call_llm("sys", "user", 123) == "ok."
    assert post.call_args.kwargs["json"]["options"]["num_predict"] == 123


def test_length_truncated_output_is_trimmed_to_a_complete_sentence(monkeypatch):
    mod = _summ(monkeypatch)
    with patch.object(mod.requests, "post",
                      return_value=_resp("CPU is high. It rose after the deploy and then", "length")):
        assert mod._call_ollama("s", "u", 20, 10) == "CPU is high."


def test_trim_drops_half_a_bullet():
    mod = load_module("app/llm/summarizer.py")
    text = "## Recommendations\n- Review the deploy.\n- Consider widening the thres"
    assert mod._trim_to_complete(text) == "## Recommendations\n- Review the deploy."


# ── grounding verifier ───────────────────────────────────────────
def test_verifier_accepts_grounded_and_rounded_numbers():
    mod = load_module("app/llm/summarizer.py")
    assert mod.ungrounded_tokens("CPU on i-0abc1234 hit 91% versus 80.", '{"v": 91.3, "t": 80, "id": "i-0abc1234"}') == []


def test_verifier_rejects_invented_number_id_and_url():
    mod = load_module("app/llm/summarizer.py")
    bad = mod.ungrounded_tokens("Hit 95% on i-0fff9999 in us-east-1, see https://x.io/a",
                                '{"v": 91.3, "id": "i-0abc1234"}')
    assert {"95", "i-0fff9999", "us-east-1", "https://x.io/a"} <= set(bad)


def test_verifier_ignores_timestamps_words_and_list_markers():
    mod = load_module("app/llm/summarizer.py")
    src = "Alert at 2026-09-30 05:54:42 on web-prod"
    assert mod.ungrounded_tokens("1. Alert at 2026-09-30 05:54:42 on web-prod, t3.large.", src) == []


def test_polish_summary_falls_back_when_output_invents_a_fact(monkeypatch):
    mod = _summ(monkeypatch)
    mod._call_llm = lambda *a, **k: "CPU hit 99% on i-0abc1234."
    assert mod.polish_summary(FACTS, FACTS["template_summary"]) == FACTS["template_summary"]


def test_polish_summary_keeps_grounded_output(monkeypatch):
    mod = _summ(monkeypatch)
    mod._call_llm = lambda *a, **k: "CPU on i-0abc1234 reached 91.3, above the 80 threshold."
    assert mod.polish_summary(FACTS, FACTS["template_summary"]).startswith("CPU on i-0abc1234 reached")


def test_verifier_can_be_disabled(monkeypatch):
    mod = _summ(monkeypatch, LLM_VERIFY_OUTPUT="false")
    mod._call_llm = lambda *a, **k: "CPU hit 99%."
    assert mod.polish_summary(FACTS, FACTS["template_summary"]) == "CPU hit 99%."


def test_rca_narrative_requires_both_sections_and_grounding(monkeypatch):
    mod = _summ(monkeypatch)
    good = "## Executive Summary\nCPU reached 91.3 against 80.\n\n## Recommendations\n- Review i-0abc1234."
    mod._call_llm = lambda *a, **k: good
    assert mod.generate_rca_narrative(FACTS) == good
    mod._call_llm = lambda *a, **k: "## Executive Summary\nOnly one section."
    assert mod.generate_rca_narrative(FACTS) is None
    mod._call_llm = lambda *a, **k: good.replace("91.3", "97.7")
    assert mod.generate_rca_narrative(FACTS) is None


def test_rca_narrative_uses_its_own_cap_and_timeout(monkeypatch):
    mod = _summ(monkeypatch)
    seen = {}

    def fake(system_prompt, user_content, max_tokens=0, timeout=None):
        seen.update(max_tokens=max_tokens, timeout=timeout)
        return ""
    mod._call_llm = fake
    mod.generate_rca_narrative(FACTS)
    assert seen == {"max_tokens": 320, "timeout": 180.0}


# ── refresh_llm_summaries commits per alert ──────────────────────
class _CountingConn(FakeConn):
    def __init__(self, script):
        super().__init__(script)
        self.commits = 0

    def cursor(self, dictionary=True):
        class C(FakeCursor):
            rowcount = 1
        return C(self.script)

    def commit(self):
        self.commits += 1


def test_refresh_commits_after_each_alert_not_only_at_the_end():
    conn = _CountingConn([
        (contains("FROM alerts", "ORDER BY triggered_at DESC"),
         [{"id": 1, "llm_summary_source_hash": None}, {"id": 2, "llm_summary_source_hash": None}]),
        (contains("UPDATE alerts", "SET llm_summary"), None),
    ])
    install_stub("app.db", get_connection=lambda: conn)
    install_stub("app.llm.summarizer", is_enabled=lambda: True,
                 polish_summary=lambda f, d: f"polished {d}", source_hash=lambda t: f"h:{t}")
    install_stub("app.collector.rca", explain_alert=lambda i: {
        "template_summary": f"t{i}", "confidence": "high", "trend": None,
        "is_likely_flapping": False, "probable_trigger": None, "related_alert_count": 0})
    mod = load_module("app/collector/llm_summarizer.py")
    assert mod.refresh_llm_summaries() == 2
    # 1 (end read snapshot) + 2 (one per alert) + 1 (final) -- never a single commit at the end
    assert conn.commits >= 4


# ── RCA report: cached, non-blocking ─────────────────────────────
def _rca_module(monkeypatch, narrative_fn, cache=None, enabled=True):
    monkeypatch.setenv("LLM_RCA_WAIT_SECONDS", "0.3")
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.collector.rca", explain_alert=lambda i: {})
    install_stub("app.llm.summarizer", generate_rca_narrative=narrative_fn, is_enabled=lambda: enabled)
    install_stub("app.llm.aws_docs", get_references=lambda *a: [])
    mod = load_module("app/llm/rca_report.py")
    facts = {"alert_id": 7, "metric_name": "CPUUtilization", "resource_type": "ec2", "resource_id": "i-1",
             "resource_name": "web", "account_name": "a", "severity": "WARNING", "status": "active",
             "triggered_at": "2026-10-01 00:00:00", "resolved_at": None, "duration_minutes": None,
             "current_value": 91.3, "threshold": 80, "threshold_delta_pct": 14.1, "confidence": "high",
             "trend": None, "is_likely_flapping": False, "probable_trigger": None, "recent_deployment": None,
             "related_alert_count": 0, "template_summary": "CPU high.", "timeline": [], "references": []}
    store = cache if cache is not None else {}
    mod._gather_facts = lambda alert_id: dict(facts)
    mod._read_cache = lambda alert_id, h: store.get((alert_id, h))
    mod._write_cache = lambda alert_id, h, text: store.__setitem__((alert_id, h), text)
    return mod, store


def test_rca_report_serves_cached_narrative_without_calling_the_llm(monkeypatch):
    calls = []
    mod, store = _rca_module(monkeypatch, lambda f: calls.append(1) or "x")
    store[(7, mod._facts_hash(mod._gather_facts(7)))] = "## Executive Summary\ncached\n\n## Recommendations\n- ok"
    rep = mod.generate_rca_report(7)
    assert rep["narrative_source"] == "llm" and rep["narrative_pending"] is False
    assert "cached" in rep["narrative_markdown"] and calls == []


def test_rca_report_returns_template_fast_then_cache_fills_in(monkeypatch):
    release = threading.Event()

    def slow(facts):
        release.wait(5)
        return "## Executive Summary\nllm text\n\n## Recommendations\n- do x"
    mod, store = _rca_module(monkeypatch, slow)
    t0 = time.time()
    rep = mod.generate_rca_report(7)
    assert time.time() - t0 < 2, "must not block for the whole LLM call"
    assert rep["narrative_source"] == "template" and rep["narrative_pending"] is True
    assert "being generated" in mod.render_markdown(rep)

    release.set()
    deadline = time.time() + 3
    while time.time() < deadline and not store:
        time.sleep(0.02)
    again = mod.generate_rca_report(7)
    assert again["narrative_source"] == "llm" and "llm text" in again["narrative_markdown"]


def test_rca_report_does_not_start_duplicate_generations(monkeypatch):
    calls = []
    release = threading.Event()

    def slow(facts):
        calls.append(1)
        release.wait(5)
        return None
    mod, _ = _rca_module(monkeypatch, slow)
    mod.generate_rca_report(7)
    mod.generate_rca_report(7)
    assert len(calls) == 1
    release.set()


def test_rca_report_failure_cools_down_and_falls_back(monkeypatch):
    calls = []
    mod, _ = _rca_module(monkeypatch, lambda f: calls.append(1) or None)
    first = mod.generate_rca_report(7)
    assert first["narrative_source"] == "template" and first["narrative_pending"] is False
    mod.generate_rca_report(7)  # within cooldown: no new LLM call
    assert len(calls) == 1


def test_rca_report_disabled_llm_never_starts_a_thread(monkeypatch):
    calls = []
    mod, _ = _rca_module(monkeypatch, lambda f: calls.append(1), enabled=False)
    rep = mod.generate_rca_report(7)
    assert rep["narrative_source"] == "template" and rep["narrative_pending"] is False and calls == []


def test_pending_note_is_plain_text_near_the_top_not_a_trailing_asterisk_line(monkeypatch):
    mod, _ = _rca_module(monkeypatch, lambda f: None)
    facts = mod._gather_facts(7)
    md = mod.render_markdown({"facts": facts, "narrative_markdown": "## Executive Summary\nx\n\n## Recommendations\n- y",
                              "narrative_source": "template", "narrative_pending": True})
    note = [ln for ln in md.splitlines() if ln.startswith("Note: an AI-written")]
    assert len(note) == 1 and "*" not in note[0]
    assert md.index(note[0]) < md.index("## Executive Summary")
    assert "being generated" not in md.split("## Timeline")[1]


# -- model drift visibility / pin mode (2026-10-03) ---------------------------

class _InlineThread:
    """threading.Thread stand-in that runs the target synchronously, so the background pull is testable."""
    def __init__(self, target=None, name=None, daemon=None, **kw):
        self._target = target

    def start(self):
        self._target()


def _tags(digest):
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = {"models": [{"name": "llama3.2:3b", "digest": "sha256:" + digest}]}
    return r


def _pull_ok():
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = {"status": "success"}
    return r


def test_refresh_warns_loudly_when_the_tag_now_points_at_different_weights(monkeypatch, caplog):
    import logging
    mod = _summ(monkeypatch)
    monkeypatch.setattr(mod.threading, "Thread", _InlineThread)
    digests = iter([_tags("a80c4f17acd5ffff"), _tags("b11d00e9f3a2eeee")])
    monkeypatch.setattr(mod.requests, "get", lambda *a, **k: next(digests))
    monkeypatch.setattr(mod.requests, "post", lambda *a, **k: _pull_ok())
    with caplog.at_level(logging.INFO):
        assert mod.refresh_ollama_model() is True
    warned = [r for r in caplog.records if r.levelname == "WARNING" and "CHANGED upstream" in r.getMessage()]
    assert len(warned) == 1 and "a80c4f17acd5 -> b11d00e9f3a2" in warned[0].getMessage()


def test_refresh_logs_the_digest_quietly_when_nothing_changed(monkeypatch, caplog):
    import logging
    mod = _summ(monkeypatch)
    monkeypatch.setattr(mod.threading, "Thread", _InlineThread)
    monkeypatch.setattr(mod.requests, "get", lambda *a, **k: _tags("a80c4f17acd5ffff"))
    monkeypatch.setattr(mod.requests, "post", lambda *a, **k: _pull_ok())
    with caplog.at_level(logging.INFO):
        mod.refresh_ollama_model()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("(digest a80c4f17acd5)" in r.getMessage() for r in caplog.records)


def test_pin_mode_never_pulls(monkeypatch):
    mod = _summ(monkeypatch, OLLAMA_AUTO_REFRESH="false")

    def boom(*a, **k):
        raise AssertionError("pin mode must not touch Ollama")
    monkeypatch.setattr(mod.requests, "post", boom)
    monkeypatch.setattr(mod.requests, "get", boom)
    assert mod.refresh_ollama_model() is False


def test_digest_lookup_failure_never_breaks_the_refresh(monkeypatch, caplog):
    import logging
    mod = _summ(monkeypatch)
    monkeypatch.setattr(mod.threading, "Thread", _InlineThread)

    def down(*a, **k):
        raise mod.requests.exceptions.ConnectionError("no tags")
    monkeypatch.setattr(mod.requests, "get", down)
    monkeypatch.setattr(mod.requests, "post", lambda *a, **k: _pull_ok())
    with caplog.at_level(logging.INFO):
        assert mod.refresh_ollama_model() is True
    assert any("refreshed Ollama model" in r.getMessage() for r in caplog.records)


# -- capacity forecast in the report (2026-10-04) -----------------------------

def test_report_fallback_recommends_acting_when_a_disk_fills_within_30_days(monkeypatch):
    mod, _ = _rca_module(monkeypatch, lambda f: None)
    facts = mod._gather_facts(7)
    facts["capacity_forecast"] = {"days_to_exhaustion": 11.8, "slope_per_day": 1.43, "current_value": 83.0, "counts_up": True}
    md = mod._fallback_narrative(facts)
    assert "- Capacity: at the recent rate this resource reaches its limit in about 12 days" in md
    facts["capacity_forecast"]["days_to_exhaustion"] = 200.0
    assert "- Capacity:" not in mod._fallback_narrative(facts)        # far-off forecasts are not an action item
    facts["capacity_forecast"] = None
    assert "- Capacity:" not in mod._fallback_narrative(facts)


def test_rca_prompt_lets_the_model_cite_the_forecast_but_the_verifier_still_guards_the_numbers(monkeypatch):
    mod = _summ(monkeypatch)
    assert "capacity_forecast" in mod._RCA_REPORT_SYSTEM_PROMPT
    facts = dict(FACTS, capacity_forecast={"days_to_exhaustion": 11.8, "slope_per_day": 1.43})
    good = ("## Executive Summary\nDisk will fill in about 12 days at 1.43 per day.\n\n"
            "## Recommendations\n- Review i-0abc1234.")
    bad = good.replace("12 days", "3 days")
    mod._call_llm = lambda *a, **k: good
    assert mod.generate_rca_narrative(facts) == good
    mod._call_llm = lambda *a, **k: bad
    assert mod.generate_rca_narrative(facts) is None
