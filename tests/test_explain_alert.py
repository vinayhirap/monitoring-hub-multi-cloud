# tests/test_explain_alert.py
"""
Coverage for app/collector/rca.py's explain_alert() -- the customer-
facing, per-alert deep-RCA entry point added 2026-09-14, distinct from
rank_probable_cause() (internal, incident-only, already covered by
tests/test_aiops_phase1.py).
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub, FakeCursor, FakeConn


def _install_stub(alert_row, in_degree=0, cloud_events=None, config_changes=None,
                   trend_points=None, related=None, flapping_threshold=None, flapping_baseline=None):
    cloud_events = cloud_events or []
    config_changes = config_changes or []
    trend_points = trend_points if trend_points is not None else []

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT id, resource_id, metric_name, severity, triggered_at"):
                self._pending = [alert_row] if alert_row else []
            elif "COUNT(DISTINCT source_resource_id)" in normalized:
                self._pending = [{"in_degree": in_degree}]
            elif normalized.startswith("SELECT ce.event_name"):
                self._pending = cloud_events
            elif normalized.startswith("SELECT actor, action, payload"):
                self._pending = config_changes
            elif normalized.startswith("SELECT UNIX_TIMESTAMP(h.metric_timestamp)"):
                self._pending = trend_points
            elif normalized.startswith("SELECT ia.incident_id, COUNT(*)"):
                self._pending = [related] if related else []
            elif normalized.startswith("SELECT t.critical_value, t.comparison, t.dynamic_k"):
                # _check_flapping()'s threshold lookup -- defaults to "no
                # static threshold configured" so existing tests that
                # don't care about flapping are unaffected.
                self._pending = [flapping_threshold] if flapping_threshold else []
            elif normalized.startswith("SELECT AVG(mean_value) AS typical_value"):
                # _check_flapping()'s baseline lookup.
                self._pending = [flapping_baseline] if flapping_baseline else []
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))


def _base_alert(**overrides):
    row = {
        "id": 42, "resource_id": "i-abc", "metric_name": "CPUUtilization",
        "severity": "CRITICAL", "triggered_at": "2026-09-14 10:00:00",
        "current_value": 95.0, "threshold": 80.0,
    }
    row.update(overrides)
    return row


def test_explain_alert_detects_flapping():
    """THE REAL PRODUCTION CASE: mean healthy (1.78M), but mean +
    3*stddev crosses the 5M critical line -- explain_alert() must
    surface this in plain language, and must NOT fall back to the
    generic 'isolated fluctuation' text even though no cloud event/
    config change/dependent/related-alert signal exists either."""
    flapping_threshold = {"critical_value": 5_000_000, "comparison": ">", "dynamic_k": None}
    flapping_baseline = {"typical_value": 1_780_000, "typical_stddev": 1_200_000, "total_samples": 1553}
    _install_stub(_base_alert(metric_name="NetworkOut"),
                   flapping_threshold=flapping_threshold, flapping_baseline=flapping_baseline)
    mod = load_module("app/collector/rca.py")

    result = mod.explain_alert(42)

    assert result["is_likely_flapping"] is True
    assert "flapping" in result["summary"].lower()
    assert "isolated fluctuation" not in result["summary"]


def test_explain_alert_no_flapping_when_mean_itself_breaches():
    """If the mean ALREADY breaches critical, this isn't flapping --
    it's a genuine chronic-mean case (a different, already-existing
    concept) -- _check_flapping() must return False here."""
    flapping_threshold = {"critical_value": 1_000_000, "comparison": ">", "dynamic_k": None}
    flapping_baseline = {"typical_value": 2_300_000, "typical_stddev": 300_000, "total_samples": 40}
    _install_stub(_base_alert(metric_name="NetworkIn"),
                   flapping_threshold=flapping_threshold, flapping_baseline=flapping_baseline)
    mod = load_module("app/collector/rca.py")

    result = mod.explain_alert(42)
    assert result["is_likely_flapping"] is False


def test_explain_alert_no_flapping_when_no_static_threshold_configured():
    """No threshold row at all (e.g. already dynamic) -- flapping check
    must degrade to False, not error."""
    _install_stub(_base_alert(), flapping_threshold=None)
    mod = load_module("app/collector/rca.py")

    result = mod.explain_alert(42)
    assert result["is_likely_flapping"] is False


def test_explain_alert_returns_none_for_missing_alert():
    _install_stub(alert_row=None)
    mod = load_module("app/collector/rca.py")
    assert mod.explain_alert(999) is None


def test_explain_alert_low_confidence_when_no_signals_found():
    _install_stub(_base_alert(), in_degree=0, cloud_events=[], config_changes=[], related=None)
    mod = load_module("app/collector/rca.py")

    result = mod.explain_alert(42)

    assert result["confidence"] == "low"
    assert "isolated fluctuation" in result["summary"]
    assert result["probable_trigger"] is None
    assert result["related_alert_count"] == 0


def test_explain_alert_high_confidence_with_cloud_event_and_dependents():
    cloud_events = [{
        "event_name": "AuthorizeSecurityGroupIngress", "event_source": "ec2.amazonaws.com",
        "username": "vinay.hirap", "event_time": "2026-09-14 09:58:00", "resource_ids": "[]",
    }]
    _install_stub(_base_alert(), in_degree=4, cloud_events=cloud_events, config_changes=[], related=None)
    mod = load_module("app/collector/rca.py")

    result = mod.explain_alert(42)

    assert result["confidence"] == "high"  # 2 signals: cloud event + in_degree
    assert "AuthorizeSecurityGroupIngress" in result["summary"]
    assert "vinay.hirap" in result["summary"]
    assert "4 other resource(s)" in result["summary"]
    assert result["probable_trigger"]["event_name"] == "AuthorizeSecurityGroupIngress"
    # Plain-language check: no internal jargon like "topology in-degree"
    # or "incident" leaking into the customer-facing summary text.
    assert "in-degree" not in result["summary"].lower()
    assert "incident" not in result["summary"].lower()


def test_explain_alert_mentions_related_alerts_without_saying_incident():
    _install_stub(_base_alert(), related={"incident_id": 7, "other_count": 2})
    mod = load_module("app/collector/rca.py")

    result = mod.explain_alert(42)

    assert result["related_alert_count"] == 2
    assert "2 related alert(s)" in result["summary"]
    assert "incident" not in result["summary"].lower()


def test_explain_alert_flags_config_change_note():
    _install_stub(_base_alert(), config_changes=[{"actor": "admin", "action": "update_threshold",
                                                    "payload": "{}", "created_at": "2026-09-14 09:59:00"}])
    mod = load_module("app/collector/rca.py")

    result = mod.explain_alert(42)
    assert "configuration change" in result["summary"]


# ── _trend_context ───────────────────────────────────────────────────

def test_trend_context_insufficient_data():
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    mod = load_module("app/collector/rca.py")

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            self._pending = [{"ts": 0, "metric_value": 50}, {"ts": 300, "metric_value": 51}]  # only 2 points

    result = mod._trend_context(_Cursor([]), "i-abc", "CPUUtilization", "2026-09-14 10:00:00")
    assert result["pattern"] == "insufficient_data"


def test_trend_context_detects_sudden_spike():
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    mod = load_module("app/collector/rca.py")
    # Flat for a long time, then a sharp jump in the last 15 minutes.
    points = [{"ts": i * 300, "metric_value": 40} for i in range(20)]  # ~100 min flat
    points += [{"ts": 20 * 300 + i * 60, "metric_value": 40 + i * 8} for i in range(10)]  # sharp climb

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            self._pending = points

    result = mod._trend_context(_Cursor([]), "i-abc", "CPUUtilization", "2026-09-14 10:00:00")
    assert result["pattern"] == "sudden_spike"


def test_trend_context_detects_gradual_climb():
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    mod = load_module("app/collector/rca.py")
    # Steady linear climb across the whole window, no sharp final jump.
    points = [{"ts": i * 300, "metric_value": 30 + i * 0.5} for i in range(30)]

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            self._pending = points

    result = mod._trend_context(_Cursor([]), "i-abc", "CPUUtilization", "2026-09-14 10:00:00")
    assert result["pattern"] == "gradual_trend"
    assert "climbing" in result["description"]
