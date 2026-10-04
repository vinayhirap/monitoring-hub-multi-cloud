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
                   trend_points=None, related=None, flapping_threshold=None, flapping_baseline=None,
                   recurrences=0):
    cloud_events = cloud_events or []
    config_changes = config_changes or []
    trend_points = trend_points if trend_points is not None else []

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT id, aws_account_id, resource_id, metric_name, severity, triggered_at"):
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
            elif normalized.startswith("SELECT acc.id AS account_id"):
                # _gather_deployment_signal()'s resource -> account lookup.
                self._pending = [{"account_id": 7}]
            elif normalized.startswith("SELECT message, detail, created_at FROM op_events"):
                # _gather_deployment_signal()'s op_events lookup -- no
                # recent deployment in these tests' windows.
                self._pending = []
            elif normalized.startswith("SELECT COUNT(*) AS recurrences"):
                # explain_alert()'s recurrence count (audit H1) - none unless a test says otherwise.
                self._pending = [{"recurrences": recurrences}]
            elif normalized.startswith("SELECT llm_summary, llm_summary_source_hash"):
                # explain_alert()'s LLM-polish cache check -- nothing cached,
                # so the deterministic template these tests verify is used.
                self._pending = []
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))


def _base_alert(**overrides):
    row = {
        "id": 42, "aws_account_id": 7, "resource_id": "i-abc", "metric_name": "CPUUtilization",
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


def test_capacity_metric_is_never_flagged_as_flapping_and_never_queries_for_it():
    """2026-10-03: a filling disk is not 'natural noise'. disk_used_percent must short-circuit
    before any threshold/baseline query, even when the numbers would otherwise fake the signature."""
    install_stub("app.db", get_connection=lambda: None)
    mod = load_module("app/collector/rca.py")

    class _NoQueries:
        def execute(self, *a, **k):
            raise AssertionError("capacity metrics must not reach the flapping SQL")

    for metric in ("disk_used_percent", "DiskSpaceUtilization", "FreeStorageSpace", "EBSFreeSpacePercent"):
        assert mod._check_flapping(_NoQueries(), 1, "i-1", metric) is False


def test_non_capacity_metric_still_reaches_the_flapping_check():
    flapping_threshold = {"critical_value": 1_000_000, "comparison": ">", "dynamic_k": None}
    flapping_baseline = {"typical_value": 800_000, "typical_stddev": 150_000, "total_samples": 40}
    _install_stub(_base_alert(metric_name="NetworkIn"),
                   flapping_threshold=flapping_threshold, flapping_baseline=flapping_baseline)
    mod = load_module("app/collector/rca.py")
    assert mod.explain_alert(42)["is_likely_flapping"] is True


def test_local_capacity_metric_list_matches_trend_py():
    """rca.py keeps its own copy of the capacity metric names (no heavy import); this keeps it honest."""
    install_stub("app.db", get_connection=lambda: None)
    rca = load_module("app/collector/rca.py")
    trend = load_module("app/collector/trend.py")
    assert set(trend.CAPACITY_METRICS) == set(rca._CAPACITY_METRIC_NAMES)


# -- capacity forecast in the explanation (2026-10-04) ------------------------

def _forecast_rows(**over):
    row = {"resource_id": "i-abc", "aws_account_id": 7, "metric_name": "disk_used_percent",
           "current_value": 83.04, "slope_per_day": 1.43, "days_to_exhaustion": 11.8}
    row.update(over)
    return [row]


def _stub_trend(rows=None, boom=False):
    def fc(resource_id, account_ids):
        if boom:
            raise RuntimeError("db down")
        return rows if rows is not None else []
    install_stub("app.collector.trend", compute_capacity_forecasts=fc,
                 CAPACITY_METRICS={"disk_used_percent": 100.0, "FreeStorageSpace": 0.0,
                                   "DiskSpaceUtilization": 100.0, "EBSFreeSpacePercent": 0.0})


def test_disk_alert_gets_a_days_to_full_sentence_and_fact():
    _install_stub(_base_alert(metric_name="disk_used_percent", current_value=83.04, threshold=80.0))
    _stub_trend(_forecast_rows())
    mod = load_module("app/collector/rca.py")
    result = mod.explain_alert(42)
    assert result["capacity_forecast"] == {"days_to_exhaustion": 11.8, "slope_per_day": 1.43,
                                            "current_value": 83.0, "counts_up": True}
    assert "about 1.4 percentage points per day" in result["summary"]
    assert "reach 100% in about 12 days" in result["summary"]


def test_free_space_metric_uses_the_decline_wording():
    _install_stub(_base_alert(metric_name="FreeStorageSpace"))
    _stub_trend(_forecast_rows(metric_name="FreeStorageSpace", slope_per_day=-2e9, days_to_exhaustion=3.2))
    mod = load_module("app/collector/rca.py")
    result = mod.explain_alert(42)
    assert result["capacity_forecast"]["counts_up"] is False
    assert "free space is projected to run out in about 3.2 days" in result["summary"]


def test_lowercase_alert_metric_name_still_matches_the_capacity_metric():
    _install_stub(_base_alert(metric_name="freestoragespace"))
    _stub_trend(_forecast_rows(metric_name="FreeStorageSpace", slope_per_day=-1.0, days_to_exhaustion=40.0))
    mod = load_module("app/collector/rca.py")
    assert mod.explain_alert(42)["capacity_forecast"]["days_to_exhaustion"] == 40.0


def test_flat_or_unfittable_series_adds_nothing_and_non_capacity_metrics_never_call_trend():
    _install_stub(_base_alert(metric_name="disk_used_percent"))
    _stub_trend([])                                   # flat / falling / too little history
    mod = load_module("app/collector/rca.py")
    result = mod.explain_alert(42)
    assert result["capacity_forecast"] is None and "projected" not in result["summary"]

    _install_stub(_base_alert(metric_name="CPUUtilization"))
    install_stub("app.collector.trend", compute_capacity_forecasts=lambda *a: (_ for _ in ()).throw(
        AssertionError("non-capacity metrics must not run a forecast")), CAPACITY_METRICS={})
    mod = load_module("app/collector/rca.py")
    assert mod.explain_alert(42)["capacity_forecast"] is None


def test_a_forecast_failure_never_breaks_the_explanation():
    _install_stub(_base_alert(metric_name="disk_used_percent"))
    _stub_trend(boom=True)
    mod = load_module("app/collector/rca.py")
    result = mod.explain_alert(42)
    assert result["capacity_forecast"] is None and result["summary"]

# ── Audit H1: persistent conditions are not "isolated fluctuations" ────────

def test_persistence_wording_is_bucketed_and_ignores_short_or_bad_values():
    install_stub("app.db", get_connection=lambda: None)
    mod = load_module("app/collector/rca.py")
    p = mod.persistence_phrase
    assert [p(m) for m in (None, "x", 0, 59)] == [None, None, None, None]
    assert p(60) == "more than an hour" and p(359) == "more than an hour"
    assert p(360) == "more than 6 hours" and p(720) == "more than 12 hours"
    assert p(1440) == "1 day" and p(14400) == "10 days" and p(14400 + 700) == "10 days"     # whole days, stable all day


def test_ten_day_breach_is_described_as_persistent_with_no_isolated_fluctuation_text():
    """The audit's xrai-alb case: an alert active ~10 days with no AWS activity around it."""
    _install_stub(_base_alert(status="active", open_minutes=14400), recurrences=0)
    mod = load_module("app/collector/rca.py")
    out = mod.explain_alert(42)
    assert "breaching for 10 days" in out["summary"] and "persistent problem" in out["summary"]
    assert "isolated fluctuation" not in out["summary"]
    assert "new normal" in out["summary"] or "unaddressed fault" in out["summary"]
    assert out["open_minutes"] == 14400 and out["recurrences_30d"] == 0
    assert out["confidence"] == "low" and "No corroborating signal" in out["confidence_reason"]


def test_short_alert_keeps_the_isolated_fluctuation_wording():
    _install_stub(_base_alert(status="active", open_minutes=12))
    mod = load_module("app/collector/rca.py")
    out = mod.explain_alert(42)
    assert "isolated fluctuation" in out["summary"] and "persistent problem" not in out["summary"]


def test_recurrence_is_reported_and_a_resolved_alert_is_described_in_the_past_tense():
    _install_stub(_base_alert(status="resolved", open_minutes=300), recurrences=7)
    mod = load_module("app/collector/rca.py")
    s = mod.explain_alert(42)["summary"]
    assert "lasted more than an hour before it cleared" in s and "persistent problem" not in s
    assert "triggered 7 other times in the last 30 days" in s


def test_confidence_reason_lists_the_signals_behind_the_level():
    _install_stub(_base_alert(), in_degree=2, related={"incident_id": 1, "other_count": 3})
    mod = load_module("app/collector/rca.py")
    out = mod.explain_alert(42)
    assert out["confidence"] == "high"
    assert "2 supporting signal(s)" in out["confidence_reason"] and "dependent resources" in out["confidence_reason"]


# -- both features together: a long-running disk alert (H1 + capacity forecast) ---

def test_persistent_disk_alert_gets_duration_then_trend_then_days_to_full():
    """A disk that has been over its line for 10 days and is still filling: the reader needs how long it has been
    wrong AND how long until it is full, in that order, and must not be told it is an isolated fluctuation."""
    _install_stub(_base_alert(metric_name="disk_used_percent", status="active", open_minutes=14400,
                              current_value=91.0, threshold=80.0), recurrences=0)
    _stub_trend(_forecast_rows(current_value=91.0, days_to_exhaustion=6.2))
    mod = load_module("app/collector/rca.py")
    out = mod.explain_alert(42)
    s = out["summary"]
    assert "breaching for 10 days" in s and "reach 100% in about 6.2 days" in s
    assert s.index("breaching for 10 days") < s.index("reach 100%")          # how long first, how soon next
    assert "isolated fluctuation" not in s
    assert out["capacity_forecast"]["days_to_exhaustion"] == 6.2 and out["open_minutes"] == 14400
