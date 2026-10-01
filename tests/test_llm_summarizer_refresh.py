"""
tests/test_llm_summarizer_refresh.py

Regression test for the 2026-09-23 incident: refresh_llm_summaries()'s
candidate-selection query ordered by `created_at`, a column `alerts`
has never had (the real column is `triggered_at` -- see
db/migrations/*.sql and DESCRIBE alerts on both Dev and Prod). Because
that SELECT lived inside this function's own try/except, the resulting
1054 "Unknown column" error was caught the same way any other failure
here would be and logged as [llm_summary_refresh_failed] -- non-fatal,
easy to miss, and silent about WHICH alerts were affected (all of
them, every cycle, for as long as the feature had been enabled).

This asserts the query's shape via FakeCursor's contains() predicate --
see tests/conftest.py -- so a regression back to `created_at` fails
loudly here (FakeCursor raises "no script entry matched") instead of
only surfacing as a WARNING-level log line in production days later.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from conftest import load_module, install_stub, FakeConn, FakeCursor, contains  # noqa: E402


class _FakeCursorWithRowcount(FakeCursor):
    """The shared FakeCursor has no notion of an UPDATE's rowcount
    (nothing in the existing suite needed one -- same gap
    tests/test_users_admin_rbac.py hit for INSERT's lastrowid, same
    fix shape). refresh_llm_summaries() reads cursor.rowcount right
    after its UPDATE; this always reports 1, which is all that matters
    here -- confirming the UPDATE branch actually ran, not the exact
    count."""
    @property
    def rowcount(self):
        return 1


class _FakeConnWithRowcount(FakeConn):
    def cursor(self, dictionary=True):
        return _FakeCursorWithRowcount(self.script)


def _load_llm_summarizer_module(script, conn_factory=_FakeConnWithRowcount):
    install_stub("app.db", get_connection=lambda: conn_factory(script))
    install_stub(
        "app.llm.summarizer",
        is_enabled=lambda: True,
        polish_summary=lambda facts, deterministic_summary: f"polished: {deterministic_summary}",
        source_hash=lambda text: f"hash:{text}",
    )
    install_stub(
        "app.collector.rca",
        explain_alert=lambda alert_id: {
            "template_summary": f"template for alert {alert_id}",
            "confidence": "medium",
            "trend": "rising",
            "is_likely_flapping": False,
            "probable_trigger": None,
            "related_alert_count": 0,
        },
    )
    return load_module("app/collector/llm_summarizer.py")


def test_candidate_query_orders_by_triggered_at_not_created_at():
    """
    The exact bug: this SELECT must order by `triggered_at` (the real
    column) -- a regression back to `created_at` makes FakeCursor raise
    AssertionError("no script entry matched"), failing this test.
    """
    script = [
        (contains("FROM alerts", "status = 'active'", "ORDER BY triggered_at DESC"),
         [{"id": 101, "llm_summary_source_hash": None}]),
        (contains("UPDATE alerts", "SET llm_summary"), None),
    ]
    mod = _load_llm_summarizer_module(script)

    refreshed = mod.refresh_llm_summaries()

    assert refreshed == 1


def test_returns_zero_and_touches_no_db_when_disabled():
    """COST CONTROL per this module's own docstring: disabled means
    zero DB reads. FakeCursor would raise on ANY query here since the
    script is empty, so this also proves get_connection() is never
    even called when the feature is off."""
    install_stub("app.db", get_connection=lambda: (_ for _ in ()).throw(
        AssertionError("get_connection() should not be called when disabled")
    ))
    install_stub("app.llm.summarizer", is_enabled=lambda: False,
                 polish_summary=lambda *a: None, source_hash=lambda *a: None)
    install_stub("app.collector.rca", explain_alert=lambda *a: None)
    mod = load_module("app/collector/llm_summarizer.py")

    assert mod.refresh_llm_summaries() == 0


def test_unchanged_facts_skip_the_update_and_are_not_counted():
    """A candidate whose current hash matches its cached
    llm_summary_source_hash should not trigger an UPDATE -- polish_summary
    stubbed here always returns a fixed string, so source_hash of that
    fixed deterministic_summary matching the stored hash is what should
    cause the skip."""
    script = [
        (contains("FROM alerts", "ORDER BY triggered_at DESC"),
         [{"id": 202, "llm_summary_source_hash": "hash:template for alert 202"}]),
        # No UPDATE entry in this script at all -- if the code issues
        # one anyway, FakeCursor raises "no script entry matched",
        # failing this test exactly as intended.
    ]
    mod = _load_llm_summarizer_module(script)

    refreshed = mod.refresh_llm_summaries()

    assert refreshed == 0


def test_fallback_result_leaves_hash_null_so_it_retries_next_cycle():
    """
    2026-09-29 regression -- confirmed live on Prod, not hypothetical:
    alerts 5614/5615 both had rca.py's own verbatim "flat_then_breach"
    template sentence cached as their llm_summary after a
    LLM_SUMMARY_TIMEOUT_SECONDS timeout, under a REAL hash and a real
    llm_summary_generated_at -- indistinguishable from a genuine success
    by every signal the table stored. The old code wrote current_hash
    unconditionally, so the next cycle's skip check (current_hash ==
    stored hash) matched immediately, treating a transient timeout as
    "already polished, don't touch" for as long as the alert's facts
    stayed the same.

    polish_summary's stub here returns the SAME text it was given --
    exactly what the real function does on a timeout or when disabled
    (see app/llm/summarizer.py's own docstring) -- so this simulates
    that failure mode without needing a real slow/dead Ollama. Asserts
    the UPDATE's hash parameter is NULL specifically (not the real
    computed hash) when that happens, via a custom predicate that
    inspects bound params directly rather than just the SQL text.

    _MAX_CONSECUTIVE_FALLBACKS (added alongside the 90s per-cycle
    budget by a separate fix) doesn't interfere here: with a single
    candidate, `fallbacks` reaches 1, never the threshold of 2 that
    would end the batch early -- this test is about what gets cached
    for that one alert, not the early-exit logic around it.
    """
    def update_writes_null_hash(sql, params):
        return ("UPDATE alerts" in sql and "SET llm_summary" in sql
                and params[1] is None)  # llm_summary_source_hash position

    script = [
        (contains("FROM alerts", "ORDER BY triggered_at DESC"),
         [{"id": 404, "llm_summary_source_hash": None}]),
        (update_writes_null_hash, None),
    ]
    install_stub("app.db", get_connection=lambda: _FakeConnWithRowcount(script))
    install_stub(
        "app.llm.summarizer",
        is_enabled=lambda: True,
        # Fallback simulation: returns the unchanged deterministic
        # text, exactly polish_summary()'s documented behavior on a
        # timeout/disabled/unreachable-API failure.
        polish_summary=lambda facts, deterministic_summary: deterministic_summary,
        source_hash=lambda text: f"hash:{text}",
    )
    install_stub(
        "app.collector.rca",
        explain_alert=lambda alert_id: {
            "template_summary": f"template for alert {alert_id}",
            "confidence": "medium", "trend": "rising",
            "is_likely_flapping": False, "probable_trigger": None,
            "related_alert_count": 0,
        },
    )
    mod = load_module("app/collector/llm_summarizer.py")

    refreshed = mod.refresh_llm_summaries()

    # Still counted -- the row IS written (correct template content,
    # just not "genuinely polished") -- what matters, and what the
    # update_writes_null_hash predicate above actually enforces, is
    # THAT hash column's value, not this count.
    assert refreshed == 1
