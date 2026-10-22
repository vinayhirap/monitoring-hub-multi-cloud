# tests/test_metric_labels_cert_expiry.py
"""The TLS certificate-expiry alert reads properly (label + unit).

`synthetic_cert_expiry` is raised by the synthetic HTTPS check; its current_value / threshold are DAYS left.
It used to render as "Synthetic Cert Expiry" and "12 / 30". These tests pin the proper label ("TLS Certificate
Expiry"), the unit ("12 days", "1 day"), that the Python and JS formatters agree, that the generated files are
in sync with the generator, and that nothing else (synthetic_uptime, percent metrics, counts) changed.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)
import app  # noqa: F401,E402
import app.metric_labels as ml  # noqa: E402
import generate_metric_labels as gen  # noqa: E402
from tests.conftest import load_module, install_stub  # noqa: E402

CERT = "synthetic_cert_expiry"


# ── label ────────────────────────────────────────────────────────────

def test_label_is_proper_in_generator_and_runtime():
    labels, conflicts = gen.build()
    assert conflicts == []
    assert labels[CERT] == "TLS Certificate Expiry"
    assert ml.metric_label(CERT) == "TLS Certificate Expiry"
    assert ml.metric_label(CERT.upper()) == "TLS Certificate Expiry"      # stored names are matched case-insensitively


def test_synthetic_uptime_presentation_is_unchanged():
    assert ml.metric_label("synthetic_uptime") == "Synthetic Check Availability"
    assert ml.metric_unit_name("synthetic_uptime") == ""
    assert ml.format_metric_value("synthetic_uptime", 2) == "2"            # consecutive failures: still unitless
    assert ml.format_metric_value("synthetic_uptime", 1) == "1"
    assert "synthetic_uptime" not in ml.APP_UNITS


# ── unit ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (12, "12 days"), (30, "30 days"), (7, "7 days"), (0, "0 days"),
    (1, "1 day"), (1.0, "1 day"), ("1", "1 day"), ("12", "12 days"), (-3, "-3 days"), (-1, "-1 day"),
])
def test_cert_values_carry_days(value, expected):
    assert ml.format_metric_value(CERT, value) == expected
    assert ml.format_metric_value(CERT, value, grouped=True) == expected


def test_cert_empty_values_have_no_unit():
    assert ml.format_metric_value(CERT, None) == "-"
    assert ml.format_metric_value(CERT, "") == "-"
    assert ml.format_metric_value(CERT, "n/a") == "n/a"


# Outputs captured from the code BEFORE this change (base 25b58fc): formatting of every other metric is pinned.
@pytest.mark.parametrize("name,value,plain,grouped", [
    ("cpuutilization", 95.134, "95.13%", "95.13%"),
    ("disk_used_percent", 83.1, "83.1%", "83.1%"),
    ("burstbalance", 40, "40%", "40%"),
    ("volumewriteops", 12100, "12.1K", "12.1K"),
    ("volumewriteops", 4361.91, "4362", "4,362"),            # COUNT metric: whole number
    ("networkin", 2480000, "2.48M", "2.48M"),
])
def test_other_metrics_format_exactly_as_before(name, value, plain, grouped):
    assert ml.format_metric_value(name, value) == plain
    assert ml.format_metric_value(name, value, grouped=True) == grouped


def test_percent_helper_stays_percent_only():
    assert ml.metric_unit(CERT) == ""
    assert ml.metric_unit("cpuutilization") == "%"


def test_app_units_is_a_deliberate_short_list():
    assert ml.APP_UNITS == {CERT: "day"}


def test_unit_word_for_reports():
    assert ml.app_unit_word(CERT) == "Days"
    assert ml.app_unit_word("cpuutilization") == ""
    assert ml.metric_unit_name(CERT) == "Days"                             # generated UNITS, used by RCA report wording


# ── generated files ──────────────────────────────────────────────────

def test_generated_files_are_in_sync_with_the_generator():
    labels, conflicts = gen.build()
    assert conflicts == []
    assert open(gen.OUT, encoding="utf-8").read() == gen.render(labels), "run: python3 scripts/generate_metric_labels.py"
    assert open(gen.OUT_PY, encoding="utf-8").read() == gen.render_py(labels), "run: python3 scripts/generate_metric_labels.py"


def test_generated_js_exports_the_app_units():
    js = open(gen.OUT, encoding="utf-8").read()
    assert "export const GENERATED_APP_UNITS = {" in js
    assert f'"{CERT}": "day"' in js


def test_generator_check_flag_passes():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "generate_metric_labels.py"), "--check"])
    assert r.returncode == 0


# ── the frontend helper agrees with Python ───────────────────────────

@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frontend_helper_matches_python():
    cases = [(CERT, v) for v in (12, 30, 7, 0, 1, "1", "12", -3, None, "")] + [
        ("cpuutilization", 95.134), ("disk_used_percent", 83.1), ("volumewriteops", 12100),
        ("synthetic_uptime", 2), ("synthetic_uptime", 1)]
    script = ("import { metricLabel, metricUnit, formatMetricValue } from 'file://" + ROOT +
              "/frontend/src/utils/metricLabels.js';\n"
              "const cases = JSON.parse(process.argv[1]);\n"
              "console.log(JSON.stringify({label: metricLabel('" + CERT + "'), unit: metricUnit('" + CERT + "'),"
              " v: cases.map(([n, v]) => formatMetricValue(n, v))}));\n")
    out = subprocess.run(["node", "--input-type=module", "-e", script, json.dumps(cases)],
                         capture_output=True, text=True, check=True).stdout
    got = json.loads(out)
    assert got["label"] == "TLS Certificate Expiry" and got["unit"] == ""
    for (name, value), js_text in zip(cases, got["v"]):
        py_text = ml.format_metric_value(name, value)
        # the UI uses an em dash for "no value" where Python uses a hyphen; every real value must match exactly
        assert js_text == ("\u2014" if py_text == "-" else py_text), (name, value, js_text, py_text)
    assert dict(zip(map(tuple, cases), got["v"]))[(CERT, 12)] == "12 days"
    assert dict(zip(map(tuple, cases), got["v"]))[(CERT, 1)] == "1 day"


# ── not touched: alert logic ─────────────────────────────────────────

def test_system_metrics_unchanged():
    from app import alert_rules
    assert alert_rules.SYSTEM_METRICS == ("multivariate_anomaly", "synthetic_uptime", "synthetic_cert_expiry",
                                          "host_agent_heartbeat")  # host_agent_heartbeat: Phase D watchdog


# ── RCA report wording (uses the generated UNITS + format_metric_value) ──

@pytest.fixture()
def rr():
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.collector.rca", explain_alert=lambda i: {})
    install_stub("app.llm.summarizer", generate_rca_summary=lambda f, d: None, is_enabled=lambda: False)
    install_stub("app.llm.aws_docs", get_references=lambda *a: [])
    return load_module("app/llm/rca_report.py")


def test_rca_report_reads_days(rr):
    facts = {"metric_name": CERT, "metric_label": "TLS Certificate Expiry", "metric_unit": ml.metric_unit_name(CERT),
             "current_value": 12, "threshold": 30}
    assert rr._reading(facts, "current_value") == "12 days"
    assert rr._reading(facts, "threshold") == "30 days"
    assert rr._reading_note(facts) == "TLS Certificate Expiry (Days)"


def test_rca_report_wording_for_other_metrics_unchanged(rr):
    up = {"metric_name": "synthetic_uptime", "metric_label": "Synthetic Check Availability",
          "metric_unit": ml.metric_unit_name("synthetic_uptime"), "current_value": 2}
    assert rr._reading(up, "current_value") == "2"
    assert rr._reading_note(up) == "Synthetic Check Availability"
    cpu = {"metric_name": "cpuutilization", "metric_label": "CPU Utilization",
           "metric_unit": ml.metric_unit_name("cpuutilization"), "current_value": 95.134}
    assert rr._reading(cpu, "current_value") == "95.13%"
    assert rr._reading_note(cpu) == "CPU Utilization"
