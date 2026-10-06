# tests/test_audit_p14_rca_report.py
"""
Audit of the first real RCA PDF (alert 9467). Each test pins one defect that report had:

  * the summary opened with the fragment "It has also triggered 108 other times"
  * raw keys and unformatted numbers: "VolumeReadOps", "1267.0 / 1116.1497530515035", resource type "ebs"
  * no time zone on any timestamp; no region/environment/alert id; "still active" with no duration
  * "1 other resource(s) rely on this one" with the resource not named
  * recommendations that were not recommendations ("prioritize resolution", "See References")
  * a thin, unlabelled timeline; duplicate "verify before distribution" lines; no PDF metadata
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import app                 # noqa: F401  (real package first: the tests below stub app.db / app.llm.* / app.collector.rca)
import app.metric_labels as ml

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

FACTS = {
    "alert_id": 9467, "resource_id": "vol-033fecea4fe16e25d", "resource_name": "vol-033fecea4fe16e25d",
    "resource_type": "ebs", "account_name": "AuroGov Mumbai", "metric_name": "volumereadops",
    "metric_label": "Volume Read Operations", "region": "ap-south-1", "environment": "prod", "severity": "WARNING",
    "status": "active", "triggered_at": "2026-10-04 16:13:45", "resolved_at": None, "duration_minutes": None,
    "current_value": 1267.0, "threshold": 1116.1497530515035, "threshold_delta_pct": 13.5, "confidence": "medium",
    "confidence_reason": "1 supporting signal: dependent resources.",
    "persistence": "This alert has triggered 108 other times in the last 30 days.", "recurrences_30d": 108,
    "dependents": ["U4RAD-PROD-ORTHANC"], "dependent_count": 1,
    "trend": {"description": "The reading jumped sharply in the last 15 minutes before the alert, rather than building up gradually."},
    "is_likely_flapping": False, "capacity_forecast": None, "probable_trigger": None, "recent_deployment": None,
    "related_alert_count": 0, "template_summary": "x",
    "timeline": [{"time": "2026-10-04 16:13:45", "event": "Alert opened: Volume Read Operations on vol-033fecea4fe16e25d"}],
    "references": [{"title": "Amazon CloudWatch metrics for Amazon EBS",
                    "url": "https://docs.aws.amazon.com/ebs/latest/userguide/using_cloudwatch_ebs.html"}],
}


@pytest.fixture()
def rr():
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.collector.rca", explain_alert=lambda i: {})
    install_stub("app.llm.summarizer", generate_rca_summary=lambda f, d: None, is_enabled=lambda: False)
    install_stub("app.llm.aws_docs", get_references=lambda *a: [])
    return load_module("app/llm/rca_report.py")


def _report(rr, facts=None, pending=False, source="template"):
    f = dict(FACTS, **(facts or {}))
    return {"facts": f, "narrative_markdown": rr._fallback_narrative(f), "narrative_source": source,
            "narrative_pending": pending}


# ── metric wording and numbers ────────────────────────────────────────────────

def test_metric_labels_match_the_ui_wording():
    assert ml.metric_label("volumereadops") == "Volume Read Operations"
    assert ml.metric_label("VolumeReadOps") == "Volume Read Operations"          # case-insensitive lookup, same as the UI
    assert ml.metric_label("cpuutilization") == "CPU Utilization"
    assert ml.metric_label("disk_used_percent") == "Disk Utilization"
    assert ml.metric_label("disk_used_percent__var_lib_mysql") == "Disk Utilization (/var/lib/mysql)"
    assert ml.metric_label("some_new_custom_metric") == "Some New Custom Metric"       # tokenizer fallback
    assert ml.metric_label("") == "" and ml.metric_label(None) == ""


def test_value_formatting_is_unit_aware_and_never_prints_13_decimals():
    f = ml.format_metric_value
    assert f("cpuutilization", 95.134) == "95.13%"
    assert f("volumereadops", 1116.1497530515035, grouped=True) == "1,116"          # a COUNT metric: whole numbers
    assert f("cpucreditbalance", 1116.1497530515035, grouped=True) == "1,116.15"    # a fractional gauge keeps its decimals
    assert f("volumereadops", 1267.0, grouped=True) == "1,267" and f("volumereadops", 1267.0) == "1267"
    assert f("networkin", 12100) == "12.1K" and f("networkin", 2_500_000) == "2.50M"
    assert f("x", 0.05) == "0.05" and f("x", None) == "-" and f("x", "n/a") == "n/a"


def test_label_tables_are_generated_from_one_source():
    out = subprocess.run([sys.executable, str(ROOT / "scripts/generate_metric_labels.py"), "--check"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, "run: python3 scripts/generate_metric_labels.py"


# ── the executive summary ─────────────────────────────────────────────────────

def test_summary_is_structured_formatted_and_has_no_fragment_or_raw_keys(rr):
    md = rr._fallback_narrative(dict(FACTS))
    assert md.startswith("## Executive Summary\n\n**What happened.** Volume Read Operations on vol-033fecea4fe16e25d (EBS volume)")
    for lead in ("**Pattern.**", "**Impact.**", "**Probable cause.**"):
        assert lead in md
    assert "1,267 against a limit of 1,116 (13.5% over)" in md
    assert "has been open for" in md and "UTC" in md
    for bad in ("It has also triggered 108", "resource(s)", "VolumeReadOps", "volumereadops", "1116.1497", "1267.0", " ebs "):
        assert bad not in md, bad
    assert "1 other resource relies on this one (U4RAD-PROD-ORTHANC)" in md


def test_resolved_alert_reports_how_long_it_lasted_and_a_deployment_as_the_probable_cause(rr):
    md = rr._fallback_narrative(dict(FACTS, status="resolved", resolved_at="2026-10-04 18:40:10", duration_minutes=146.4,
                                     recent_deployment={"message": "payment-api v2.14.0"}))
    assert "It resolved after 2 hours 26 min." in md
    assert "A deployment shortly before the alert is the most likely trigger, though not confirmed: payment-api v2.14.0." in md
    assert "No deployment or AWS change was found" not in md


def test_a_filling_disk_gets_the_forecast_in_the_summary_and_the_action_item(rr):
    md = rr._fallback_narrative(dict(FACTS, metric_name="disk_used_percent",
                                     capacity_forecast={"days_to_exhaustion": 6.2, "slope_per_day": 1.43, "counts_up": True}))
    assert "projected to reach 100% in about 6.2 days" in md
    assert "- Capacity: at the recent rate this resource reaches its limit in about 6.2 days - clean up or extend storage" in md


# ── recommendations ───────────────────────────────────────────────────────────

def test_recommendations_are_grounded_and_the_filler_lines_are_gone(rr):
    recs = rr._build_recommendations(dict(FACTS))
    text = "\n".join(recs)
    assert "fired 108 other times in 30 days" in text and "auto-tuning" in text
    assert "Check the dependent resource for impact: U4RAD-PROD-ORTHANC." in recs
    assert any(r.startswith("Suggested first checks:") and "provisioned IOPS" in r for r in recs)
    assert "prioritize resolution" not in text and "See References" not in text


def test_a_long_running_alert_is_told_to_decide_between_new_normal_and_fault(rr, monkeypatch):
    monkeypatch.setattr(rr, "_open_minutes", lambda facts, now=None: 14400)
    recs = rr._build_recommendations(dict(FACTS, recurrences_30d=0))
    assert any("Open for 10 days" in r and "new normal" in r for r in recs)


def test_no_signal_at_all_still_yields_one_honest_line(rr, monkeypatch):
    # Frozen clock: FACTS has a fixed triggered_at, so once the real clock passed 24 h after it the "open for N days"
    # recommendation (correctly) appeared and this test started failing on its own (2026-10-05).
    monkeypatch.setattr(rr, "_open_minutes", lambda facts, now=None: 45)
    recs = rr._build_recommendations(dict(FACTS, metric_name="some_unknown_metric", recurrences_30d=0,
                                          dependents=[], dependent_count=0))
    assert recs == ["No specific recommendation could be derived automatically from the signals gathered for this alert."]


def test_guidance_is_only_a_suggestion_and_covers_the_common_families(rr):
    for name in ("cpuutilization", "disk_used_percent__boot", "networkin", "healthyhostcount", "volumewriteops",
                 "statuscheckfailed", "daystoexpiry", "databaseconnections"):
        g = rr._metric_guidance(name)
        assert g and g.startswith("Suggested first checks:"), name
    assert rr._metric_guidance("something_else_entirely") is None


# ── the markdown (also the .md download) ──────────────────────────────────────

def test_markdown_metadata_is_complete_formatted_and_labelled_with_a_zone(rr, monkeypatch):
    monkeypatch.setattr(rr, "_open_minutes", lambda facts, now=None: 45)      # frozen clock, see above
    md = rr.render_markdown(_report(rr))
    assert md.startswith("# RCA Report: Volume Read Operations on vol-033fecea4fe16e25d")
    for row in ("- **Alert:** #9467", "- **Resource:** vol-033fecea4fe16e25d (EBS volume)", "- **Region:** ap-south-1",
                "- **Environment:** PROD", "- **Severity:** Warning", "- **Triggered:** 04 Oct 2026, 16:13:45 UTC",
                "- **Reading vs limit:** 1,267 against a limit of 1,116 (13.5% over)",
                "- **RCA confidence:** Medium. 1 supporting signal: dependent resources."):
        assert row in md, row
    assert re.search(r"- \*\*Status:\*\* Active, open for \d+ (minutes?|hours?)", md)
    assert re.search(r"- \*\*Report generated:\*\* \d{2} \w{3} \d{4}, \d{2}:\d{2}:\d{2} UTC", md)
    assert "- **Resource ID:**" not in md            # only shown when the name differs from the id


def test_a_named_resource_shows_its_id_separately_and_unknown_environment_is_omitted(rr):
    md = rr.render_markdown(_report(rr, {"resource_name": "U4RAD-PROD-DB", "environment": "unknown"}))
    assert "- **Resource:** U4RAD-PROD-DB (EBS volume)" in md and "- **Resource ID:** vol-033fecea4fe16e25d" in md
    assert "**Environment:**" not in md


def test_timeline_is_formatted_and_the_closing_note_says_which_summary_it_is(rr):
    facts = dict(FACTS, timeline=[{"time": "2026-10-04 16:13:45", "event": "Alert opened: X"},
                                  {"time": "2026-10-04 16:20:00", "event": "Acknowledged by tejas"}])
    md = rr.render_markdown(_report(rr, facts))
    assert "- **04 Oct 2026, 16:13:45 UTC** \u2014 Alert opened: X" in md and "Acknowledged by tejas" in md
    assert md.rstrip().endswith("*Generated automatically by AurionPro CloudOps (rule-based summary). Verify before external distribution.*")
    assert "AI-written summary)" in rr.render_markdown(_report(rr, source="llm"))
    assert md.count("verify before external distribution") + md.count("Verify before external distribution") == 1


def test_pending_note_keeps_the_phrase_the_ui_and_tests_rely_on(rr):
    md = rr.render_markdown(_report(rr, pending=True))
    assert "being generated" in md and "rule-based summary" in md
    assert "asterisk" not in md and "\n*Note" not in md


def test_key_figures(rr):
    k = {x["label"]: x for x in rr.report_kpis(_report(rr))}
    assert k["READING"]["value"] == "1,267" and k["ALERT LIMIT"]["value"] == "1,116"
    assert k["OVER LIMIT BY"]["value"] == "13.5%" and k["OVER LIMIT BY"]["tone"] == "severity"
    assert "OPEN FOR" in k
    under = {x["label"]: x for x in rr.report_kpis(_report(rr, {"threshold_delta_pct": -20.0, "resolved_at": "2026-10-04 17:00:00",
                                                                 "duration_minutes": 45.0}))}
    assert under["UNDER LIMIT BY"]["value"] == "20%" and under["DURATION"]["value"] == "45 minutes"
    assert rr.report_title(_report(rr)) == "Volume Read Operations above its limit"


def test_title_for_a_value_that_fell_below_its_limit(rr):
    assert rr.report_title(_report(rr, {"threshold_delta_pct": -5.0})) == "Volume Read Operations below its limit"
    assert rr.report_title(_report(rr, {"threshold_delta_pct": None})).endswith("alert")


def test_cache_key_does_not_contain_anything_that_changes_every_minute():
    """open_minutes would change the facts hash each minute and discard the cached AI narrative on every download."""
    src = (ROOT / "app/llm/rca_report.py").read_text()
    gather = src[src.index("def _gather_facts"):src.index("# ── presentation helpers")]
    assert '"open_minutes":' not in gather and "datetime.now" not in gather


def test_formatting_helpers_handle_bad_input(rr):
    assert rr._fmt_utc("not a date") == "not a date" and rr._fmt_utc(None) == "-"
    assert rr._fmt_utc("2026-10-04 16:13:45.123456") == "04 Oct 2026, 16:13:45 UTC"
    assert [rr._minutes_text(x) for x in (0.2, 1, 59, 60, 125, 1440, 3000, None)] == \
        ["less than a minute", "1 minute", "59 minutes", "1 hour", "2 hours 5 min", "1 day", "2 days 2 hr", "-"]


# ── the PDF ───────────────────────────────────────────────────────────────────

def _pdf_module():
    return load_module("app/llm/rca_report_pdf.py")


def test_pdf_renders_with_odd_characters_and_sparse_facts(rr):
    pdfm = _pdf_module()
    odd = _report(rr, {"resource_name": "DB \u2014 \u201cmain\u201d \u2192 replica \u00e9 \u4e2d\u6587", "timeline": []})
    out = pdfm.render_pdf(rr.render_markdown(odd), "Odd \u2014 title", severity="CRITICAL", subtitle="a \u00b7 b",
                          status="Active", kpis=rr.report_kpis(odd), alert_ref="Alert #1")
    assert out[:5] == b"%PDF-" and len(out) > 2000
    bare = pdfm.render_pdf("# T\n\n- **Account:** A\n\n## Executive Summary\n\ntext", "T")          # old call shape still works
    assert bare[:5] == b"%PDF-"


def test_pdf_carries_document_properties_and_the_alert_reference(rr):
    pdfm = _pdf_module()
    out = pdfm.render_pdf(rr.render_markdown(_report(rr)), "Volume Read Operations above its limit", severity="WARNING",
                          status="Active", kpis=rr.report_kpis(_report(rr)), alert_ref="Alert #9467")
    assert b"AurionPro CloudOps" in out and b"RCA Report - Volume Read Operations above its limit" in out


def test_pdf_text_has_the_new_content_if_pdftotext_is_available(rr, tmp_path):
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext not installed")
    pdfm = _pdf_module()
    report = _report(rr, pending=True)
    path = tmp_path / "r.pdf"
    path.write_bytes(pdfm.render_pdf(rr.render_markdown(report), rr.report_title(report), severity="WARNING", status="Active",
                                     kpis=rr.report_kpis(report), alert_ref="Alert #9467"))
    text = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True).stdout
    for needle in ("Volume Read Operations above its limit", "OVER LIMIT BY", "13.5%", "1,116", "WHAT HAPPENED", "PROBABLE CAUSE",
                   "04 Oct 2026, 16:13:45 UTC", "CONFIDENTIAL", "Alert #9467", "being generated"):
        assert needle in text, needle
    for gone in ("1116.1497", "It has also triggered 108", "resource(s)", "still active -- prioritize"):
        assert gone not in text, gone
    assert text.lower().count("verify before external distribution") == 1


def test_endpoint_passes_the_friendly_title_status_figures_and_alert_reference():
    src = (ROOT / "app/api/alerts.py").read_text()
    fn = src[src.index("def get_rca_report"):src.index("# ── MARK / UNMARK FALSE POSITIVE")]
    for needle in ("report_title(report)", "report_kpis(report)", 'alert_ref=f"Alert #{f[\'alert_id\']}"', "subtitle=", "status="):
        assert needle in fn, needle
