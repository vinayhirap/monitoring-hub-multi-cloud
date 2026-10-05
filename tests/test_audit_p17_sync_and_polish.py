# tests/test_audit_p17_sync_and_polish.py
"""
Round 7: Settings thresholds in step with the limits actually enforced; whole-number limits for count metrics; freshness that
matches how metrics are collected; light-theme popups and colours; a real Status on Security Findings; the notification form;
report fixes. Each test pins one defect from the screenshots.
"""
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

import app.alert_rules          # noqa: F401  (real packages first: conftest stubs would otherwise hide them)
import app.threshold_defaults   # noqa: F401
import app.metric_labels        # noqa: F401

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub  # noqa: E402
from app import threshold_defaults as td  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend/src"


def _t(rel):
    return (FE / rel).read_text()


# ── whole-number limits for COUNT metrics ────────────────────────────────────

_OPS = {">": lambda v, t: v > t, ">=": lambda v, t: v >= t, "<": lambda v, t: v < t, "<=": lambda v, t: v <= t}


@pytest.mark.parametrize("op", [">", ">=", "<", "<="])
def test_rounding_a_learned_limit_never_changes_which_whole_number_readings_breach(op):
    """The reason this is safe to do: for whole-number readings the alert fires on EXACTLY the same readings."""
    for limit in (0.4, 1.0, 11.31, 11.5, 11.99, 12.0, 1717.3, 4361.91, 3320.62, 99999.5):
        whole = td.integerize_limit(limit, op)
        assert float(whole).is_integer()
        for reading in range(0, 400):
            assert _OPS[op](reading, limit) == _OPS[op](reading, whole), (op, limit, whole, reading)
        for reading in (4361, 4362, 4988, 1717, 1718, 99999, 100000):
            assert _OPS[op](reading, limit) == _OPS[op](reading, whole), (op, limit, whole, reading)


def test_the_limits_from_the_screenshots_become_whole_numbers():
    assert td.integerize_limit(11.31, ">") == 11 and td.integerize_limit(4361.91, ">") == 4361
    assert td.integerize_limit(1717.3, ">") == 1717 and td.integerize_limit(None, ">") is None
    assert td.integerize_limit(11.31, ">=") == 12 and td.integerize_limit(11.31, "<") == 12 and td.integerize_limit(11.31, "<=") == 11


def test_only_count_metrics_are_whole_number_and_the_fractional_gauges_keep_decimals():
    assert td.is_integer_metric("Count", "volumereadops") and td.is_integer_metric("count", "httpcode_target_4xx_count")
    for unit, name in (("Percent", "cpuutilization"), ("Bytes", "networkin"), ("Count/Second", "readiops"),
                       ("Seconds", "targetresponsetime"), ("Milliseconds", "duration"), (None, "x")):
        assert not td.is_integer_metric(unit, name), (unit, name)
    for gauge in ("cpucreditbalance", "volumequeuelength", "diskqueuedepth", "wlmqueuelength"):
        assert not td.is_integer_metric("Count", gauge), gauge


def test_the_generated_tables_agree_with_the_rule():
    from app import metric_labels_generated as g
    ints = set(g.INTEGER_METRICS)
    assert {"volumereadops", "volumewriteops", "httpcode_target_4xx_count", "requestcount"} <= ints
    assert not ({"cpuutilization", "networkin", "cpucreditbalance", "volumequeuelength", "targetresponsetime"} & ints)
    assert "GENERATED_INTEGER_METRICS" in _t("utils/metricLabels.generated.js") and "volumereadops" in _t("utils/metricLabels.generated.js")


def test_evaluator_and_chart_lines_apply_it_to_learned_limits_only():
    ev = (ROOT / "app/collector/alert_evaluator.py").read_text()
    block = ev[ev.index("# Count metrics: a learned (dynamic / anomaly) line"):ev.index("is_critical = compare(metric_value, critical_value")]
    assert "anomaly_only or (row.get(\"use_dynamic\") and not is_static_only_metric(metric_name))" in block
    assert "integerize_limit(warning_value, comparison)" in block and "integerize_limit(critical_value, comparison)" in block
    meta = (ROOT / "app/metric_meta.py").read_text()
    assert meta.count("integerize_limit(") >= 3          # anomaly line, dynamic warning and critical on the chart


# ── what Settings shows: the limits actually in force ───────────────────────

class _Cur:
    def __init__(self, thresholds, resources, baselines):
        self.t, self.r, self.b, self._rows = thresholds, resources, baselines, []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "FROM thresholds t LEFT JOIN metric_catalog" in s:
            self._rows = self.t
        elif "FROM resources WHERE aws_account_id" in s:
            self._rows = self.r
        elif "FROM metric_baseline" in s:
            self._rows = self.b
        elif "HOUR(UTC_TIMESTAMP()) AS h" in s:
            self._rows = [{"h": 7, "d": 2}]
        else:
            raise AssertionError("unexpected query: " + s[:80])

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _evaluator():
    install_stub("app.db", get_db_cursor=lambda *a, **k: None, get_connection=lambda: None)
    return load_module("app/collector/alert_evaluator.py")


def _row(i, rtype, name, warn, crit, unit, dyn=0, cmp_=">", enabled=1):
    return {"id": i, "resource_type": rtype, "metric_id": i, "warning_value": warn, "critical_value": crit, "comparison": cmp_,
            "enabled": enabled, "use_dynamic": dyn, "dynamic_k": 3.0, "metric_name": name, "unit": unit}


def _base(rid, name, mean, std, n, stamp=datetime(2026, 10, 5, 6, 0, 0)):
    return {"resource_id": rid, "metric_name": name, "mean_value": mean, "stddev_value": std, "sample_count": n, "updated_at": stamp}


def _effective(thresholds, resources, baselines):
    from app import threshold_effective as te
    te.invalidate()
    ev = _evaluator()
    return te.compute_effective(_Cur(thresholds, resources, baselines), 10, ev)["limits"]


RES = [{"resource_id": "vol-a", "resource_type": "ebs", "name": "vol-a"}, {"resource_id": "vol-b", "resource_type": "ebs", "name": "vol-b"},
       {"resource_id": "i-1", "resource_type": "ec2", "name": "U4RAD-JUMP"}]


def test_anomaly_only_row_reports_the_learned_line_as_whole_numbers_not_the_placeholders():
    lim = _effective([_row(1, "ebs", "VolumeReadOps", 1000000, 5000000, "Count")], RES,
                     [_base("vol-a", "volumereadops", 1000, 100, 30), _base("vol-b", "volumereadops", 2000, 333.37, 30),
                      _base("i-1", "volumereadops", 5, 1, 30)])                       # i-1 is not an EBS volume: ignored
    e = lim[1]
    assert e["mode"] == "anomaly" and e["resources_total"] == 2 and e["with_limit"] == 2
    # vol-a: mean 1000 + 3*100 = 1300 but never under 1.5x the mean = 1500;  vol-b: 2000 + 3*333.37 = 3000.11 -> whole 3000
    assert e["warning"] == {"min": 1500.0, "median": 2250.0, "max": 3000.0}
    assert all(float(v).is_integer() for v in (e["warning"]["min"], e["warning"]["max"], e["critical"]["min"], e["critical"]["max"]))
    assert 1000000 not in (e["warning"]["max"], e["critical"]["max"])


def test_a_resource_still_learning_is_counted_but_has_no_line_yet():
    e = _effective([_row(1, "ebs", "VolumeReadOps", 1000000, 5000000, "Count")], RES,
                   [_base("vol-a", "volumereadops", 1000, 100, 30), _base("vol-b", "volumereadops", 2000, 300, 5)])[1]
    assert e["with_limit"] == 1 and e["learning"] == 1 and e["resources_total"] == 2
    assert [r["name"] for r in e["resources"]] == ["vol-a"]


def test_dynamic_row_uses_the_evaluators_own_guard_rails_and_keeps_decimals_for_non_counts():
    e = _effective([_row(2, "ec2", "CPUUtilization", 70, 90, "Percent", dyn=1)], RES,
                   [_base("i-1", "cpuutilization", 10, 2, 40)])[2]
    assert e["mode"] == "dynamic" and e["with_limit"] == 1
    assert e["warning"]["min"] == 35.0 and e["critical"]["min"] == 90.0       # warning may tighten to 50%; critical may only relax
    assert e["use_dynamic"] is True and e["dynamic_k"] == 3.0 and e["comparison"] == ">"


def test_fixed_rows_and_static_only_metrics_report_no_learned_limit():
    lim = _effective([_row(3, "ec2", "StatusCheckFailed", 0, 1, "Count"), _row(4, "ec2", "HealthyHostCount", 0, 0, "Count", dyn=1),
                      _row(5, "ec2", "CPUUtilization", 70, 90, "Percent")], RES, [])
    assert lim[3]["mode"] == "static" and lim[5]["mode"] == "static"
    assert lim[4]["mode"] == "static"          # use_dynamic=1 but an availability metric: always evaluated against the fixed value


def test_cache_is_dropped_on_every_threshold_write_and_the_endpoint_is_guarded():
    from app import threshold_effective as te
    te._cache[10] = (10 ** 12, {"x": 1})
    te.invalidate(10)
    assert 10 not in te._cache
    src = (ROOT / "app/api/settings.py").read_text()
    assert src.count("threshold_effective.invalidate(") >= 5                 # save, toggle, dynamic, seed, apply-defaults
    ep = src[src.index('@router.get("/thresholds/effective")'):src.index('@router.post("/thresholds")')]
    assert 'require_permission("alerts.view")' in ep and "_require_account_access(account_id, current_user)" in ep


def test_audit_text_names_the_account_and_metric_for_threshold_changes():
    src = (ROOT / "app/api/settings.py").read_text()
    assert "threshold_id={threshold_id} use_dynamic" not in src and "threshold_label(threshold_id)" in src


def test_settings_page_keeps_itself_in_step_without_overwriting_typing():
    jsx = _t("pages/Settings.jsx")
    assert "visibleInterval(refreshEffective, 60000)" in jsx and "mergeLiveThresholds(" in jsx and "holdAfterSave(" in jsx
    assert "learned-block" in jsx and "fixedValuesCaption(mode)" in jsx and 'mode === "anomaly"' in jsx
    assert "getEffectiveThresholds" in _t("api/api.js")


# ── freshness ────────────────────────────────────────────────────────────────

def test_the_server_sends_the_same_staleness_allowance_the_alert_engine_uses():
    install_stub("app.db", get_db_cursor=lambda *a, **k: None, get_connection=lambda: None)
    mm = load_module("app/metric_meta.py")
    assert [mm.stale_after_seconds(s) for s in (120, 300, 900, 3600, 86400)] == [1200, 1200, 2700, 10800, 180000]
    assert mm.stale_after_seconds(600) in (1200, 2700)                       # nearest known cadence
    assert '"stale_after_seconds": stale_after_seconds(' in (ROOT / "app/metric_meta.py").read_text()


def test_chart_card_uses_cadence_and_period_end_and_the_stale_report_script_exists():
    card = _t("components/MetricChartCard.jsx")
    assert "meta?.stale_after_seconds" in card and "displayAge(fr.age, meta?.period_seconds)" in card
    script = (ROOT / "scripts/report_stale_metrics.py").read_text()
    assert "stale_minutes_sql(" in script and "SELECT" in script
    for write in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER "):
        assert write not in script, write                                       # read-only


# ── light theme ──────────────────────────────────────────────────────────────

def test_native_controls_follow_the_theme_and_chart_fills_are_vivid_in_both_themes():
    css = _t("styles/tokens.css")
    assert re.search(r":root\s*\{\s*color-scheme:\s*dark;\s*\}", css) and re.search(r'\[data-theme="light"\]\s*\{\s*color-scheme:\s*light;\s*\}', css)
    assert "select option, select optgroup" in css
    for tok in ("--ok-fill", "--warn-fill", "--crit-fill", "--info-fill"):
        assert css.count(tok + ":") >= 2, tok                                   # defined for dark AND light
    dash = _t("pages/overview/dash.css")
    for needle in (".s-warn { background: var(--warn-fill)", ".s-crit { background: var(--crit-fill)", ".fb-fresh { background: var(--ok-fill)"):
        assert needle in dash, needle
    assert "var(--warn-fg)" not in re.findall(r"\.act-stack[^\n]*\n?[^\n]*\.s-warn[^\n]*", dash)[0]


def test_popups_use_theme_tokens_not_dark_theme_hex_colours():
    toast = _t("components/AlertToast.jsx")
    for bad in ("#dce6f5", "#7c92b4", "backdropFilter", "rgba(239,68,68,0.12)"):
        assert bad not in toast, bad
    assert "var(--ink-1)" in toast and "var(--surface-2)" in toast and "var(--crit-fill)" in toast
    zoom = _t("components/MetricZoomModal.css")
    assert "#0e1626" not in zoom and "#e6ecf7" not in zoom and "var(--surface-2)" in zoom
    badge = _t("components/AlertBadge.jsx")
    assert "var(--crit-fg)" in badge and "#ef4444" not in badge


def test_every_stylesheet_in_the_project_is_imported_by_something():
    """The notification form shipped unstyled once because its .css was never imported."""
    imported = set()
    for f in list(FE.rglob("*.jsx")) + list(FE.rglob("*.js")) + list(FE.rglob("*.css")):
        for m in re.finditer(r"""(?:import\s+|@import\s+(?:url\()?)['"]([^'"]+\.css)['"]""", f.read_text(errors="ignore")):
            imported.add((f.parent / m.group(1)).resolve())
    orphans = [str(p.relative_to(FE)) for p in FE.rglob("*.css") if p.resolve() not in imported]
    assert orphans == [], orphans


# ── Security Findings status ─────────────────────────────────────────────────

def test_security_findings_shows_a_real_status_with_dates_and_explains_resolved():
    jsx = _t("pages/SecurityFindings.jsx")
    assert "<th>Status</th>" in jsx and 'data-label="Status"' in jsx
    assert "Open since ${formatDay(f.first_seen_at" in jsx and "Fixed ${formatDay(f.resolved_at || f.last_seen_at" in jsx
    assert "the hourly scan no longer finds this issue" in jsx and 'status !== "open"' in jsx
    css = _t("pages/SecurityFindings.css")
    assert ".sec-status.is-resolved { background: var(--ok-bg)" in css and ".sec-status.is-open" in css


# ── notification channel form ────────────────────────────────────────────────

def test_notification_form_uses_the_settings_field_layout():
    jsx = _t("components/NotificationChannels.jsx")
    assert 'className="nc-form"' in jsx and jsx.count('className="nc-label"') >= 4 and 'className="nc-check"' in jsx
    assert "<label>Name<input" not in jsx and 'import "./NotificationChannels.css"' in jsx
    assert ".nc-form" in _t("components/NotificationChannels.css")


# ── reports ──────────────────────────────────────────────────────────────────

def test_most_affected_is_ordered_by_alert_count_and_the_log_shows_breadth():
    install_stub("app.db", get_db_cursor=lambda *a, **k: None, get_connection=lambda: None)
    eng = load_module("app/reports/engine.py")
    from datetime import timedelta
    alerts, i = [], 0
    for rid, name, n, status in (("a", "small-open", 27, "active"), ("b", "huge", 204, "resolved"), ("c", "mid", 117, "resolved")):
        for k in range(n):
            i += 1
            alerts.append({"id": i, "resource_id": rid, "resource_name": name, "resource_type": "ec2", "metric_name": "cpuutilization",
                           "value": 1, "severity": "WARNING", "status": status if k == 0 else "resolved",
                           "triggered_at": datetime(2026, 10, 1) + timedelta(minutes=i)})
    s = eng.summarize({"alerts": alerts, "daily_counts": {}})
    assert [r["name"] for r in s["top_resources"]] == ["huge", "mid", "small-open"]
    # 300 alerts from 5 sources: the log takes at most 5 per resource+metric first, so every source is represented
    many = []
    for n, rid in enumerate("abcdefghij"):
        for k in range(40):
            many.append({"id": n * 100 + k, "resource_id": rid, "resource_name": rid, "resource_type": "ec2", "metric_name": "cpuutilization",
                         "value": 1, "severity": "CRITICAL", "status": "resolved", "triggered_at": datetime(2026, 10, 1) + timedelta(minutes=n * 100 + k)})
    chosen = eng.select_significant(many, limit=60, per_source=5)
    counts = {}
    for a in chosen:
        counts[a["resource_id"]] = counts.get(a["resource_id"], 0) + 1
    assert len(chosen) == 60 and set(counts) == set("abcdefghij")             # every source shown ...
    assert max(counts.values()) <= 10                                         # ... and none crowds out the rest (round-robin waves)
    assert [a["triggered_at"] for a in chosen] == sorted(a["triggered_at"] for a in chosen)           # time order


def test_rca_says_learned_limit_and_names_the_unit():
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.collector.rca", explain_alert=lambda i: {})
    install_stub("app.llm.summarizer", generate_rca_narrative=lambda f: None, is_enabled=lambda: False)
    install_stub("app.llm.aws_docs", get_references=lambda *a: [])
    rr = load_module("app/llm/rca_report.py")
    facts = {"alert_id": 9501, "resource_id": "i-1", "resource_name": "Cloudops_Prod", "resource_type": "ec2", "account_name": "AuroGov Mumbai",
             "metric_name": "networkin", "metric_label": "Network In", "metric_unit": "Bytes", "limit_kind": "learned", "severity": "WARNING",
             "status": "active", "triggered_at": "2026-10-05 04:55:17", "resolved_at": None, "duration_minutes": None,
             "current_value": 2480000.0, "threshold": 2440000.0, "threshold_delta_pct": 1.6, "recurrences_30d": 224, "timeline": [], "references": []}
    report = {"facts": facts, "narrative_markdown": rr._fallback_narrative(facts), "narrative_source": "template", "narrative_pending": False}
    k = {x["label"]: x for x in rr.report_kpis(report)}
    assert k["READING"]["note"] == "Network In (Bytes)" and "LEARNED LIMIT" in k and k["LEARNED LIMIT"]["note"] == "learned for this resource"
    md = report["narrative_markdown"]
    assert "went above its learned limit" in md and "limit learned from this resource's own history" in md and "raise the limit" not in md
    assert "against a learned limit of" in rr.render_markdown(report)
    facts["limit_kind"] = "configured"
    assert "went above its alert limit" in rr._fallback_narrative(facts) and "raise the limit" in rr._fallback_narrative(facts)


def test_legacy_incident_titles_are_renamed_after_where_the_incident_really_started():
    """Weekly report: '#577 Correlated breach on U4RAD-PROD-ORTHANC' while its cause said it started with Request Count on u4rad-alb."""
    install_stub("app.db", get_db_cursor=lambda *a, **k: None, get_connection=lambda: None)
    eng = load_module("app/reports/engine.py")
    arn = "arn:aws:elasticloadbalancing:ap-south-1:992382489399:loadbalancer/app/u4rad-alb/7825df3406bbe617"
    inc = {"title": "Correlated breach on i-085a15af2d1524c7c and related resource(s)",
           "probable_cause": f"Earliest breach in this incident: requestcount on {arn} at 2026-10-03 16:28:11.",
           "member_alerts": [{"resource_id": "i-085a15af2d1524c7c", "resource_name": "U4RAD-PROD-ORTHANC"},
                             {"resource_id": arn, "resource_name": "u4rad-alb"}]}
    assert eng.incident_root(inc) == ("requestcount", arn)
    assert eng.humanize_incident_title(inc["title"], inc) == "Request Count breach on u4rad-alb and related resources"
    inc2 = {"title": "Correlated breach on vol-1 and related resources",
            "probable_cause": "Earliest breach in this incident: disk_used_percent on i-046f at 2026-09-29 16:40:33.",
            "member_alerts": [{"resource_id": "i-046f", "resource_name": "U4RAD-UAT-REPORTINGBOT-TEST-ENV"}]}
    assert eng.humanize_incident_title(inc2["title"], inc2) == "Disk Utilization breach on U4RAD-UAT-REPORTINGBOT-TEST-ENV and related resources"
    # a title that already names a metric (created after the correlator fix) is kept, only cleaned up
    new = {"title": "disk_used_percent breach on i-046f and related resource(s)", "probable_cause": inc2["probable_cause"],
           "member_alerts": inc2["member_alerts"]}
    assert eng.humanize_incident_title(new["title"], new) == "disk_used_percent breach on U4RAD-UAT-REPORTINGBOT-TEST-ENV and related resources"
    # no cause line to read: fall back to the old cleanup rather than guessing
    assert eng.humanize_incident_title("Correlated breach on i-046f and related resource(s)", {"member_alerts": inc2["member_alerts"]}) \
        == "Correlated breach on U4RAD-UAT-REPORTINGBOT-TEST-ENV and related resources"
