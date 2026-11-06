"""Compliance page: the 30s auto-refresh must be silent.

Regression guard. loadLogs() used to call setLoading(true) on EVERY refresh,
which unmounted the whole feed: the page flashed "Loading audit logs..." and
jumped back to the top (the document height collapsed), and any expanded row
closed. Only the first load may show the full loading state.
"""
import re
from pathlib import Path

FE = Path(__file__).resolve().parent.parent / "frontend" / "src"
SRC = (FE / "pages" / "Compliance.jsx").read_text(encoding="utf-8")


def _load_logs_body():
    start = SRC.index("const loadLogs = useCallback(")
    end = SRC.index("}, []);", start)
    return SRC[start:end]


def test_full_page_loading_state_is_first_load_only():
    body = _load_logs_body()
    # setLoading(true) must be guarded by the first-load check, never unconditional
    assert not re.search(r"^\s*setLoading\(true\);", body, re.M)
    assert re.search(r"if \(firstLoad\) setLoading\(true\)", body)


def test_timer_refresh_runs_in_background_mode():
    assert "setInterval(() => loadLogs({ background: true }), 30000)" in SRC
    # a failed background refresh keeps the last good feed instead of replacing it with an error
    assert re.search(r"if \(!background\) setError\(", SRC)


def test_feed_stays_mounted_when_a_manual_refresh_fails():
    # the feed is hidden by an error only when there is nothing to show
    assert "{!loading && (!error || logs.length > 0) && (" in SRC


def test_buttons_do_not_pass_the_click_event_as_options():
    # onClick={loadLogs} would hand the event object to loadLogs({ background })
    assert "onClick={loadLogs}" not in SRC
