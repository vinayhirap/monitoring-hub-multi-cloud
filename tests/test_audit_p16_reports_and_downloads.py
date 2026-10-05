# tests/test_audit_p16_reports_and_downloads.py
"""
Audit of the first real weekly report (U4RAD, 26 Sep - 03 Oct) and of the Reports / download experience:

  * "Generate report" returned HTTP 500 (see test_audit_p5_report_idempotency: unread GET_LOCK result)
  * downloads opened a new tab that flashed and closed
  * default file names were storage keys: weekly_20260926-20261003_83ed45d4.pdf, rca-report-alert-9467.pdf
  * the alert-log table had every column shifted one slot (status chip under 'Resource', resource under 'Metric'...)
  * raw metric keys, full ARNs in sentences, "resource(s)", "--" dashes, justified text with huge gaps, 40 near-identical
    'affected resource' lines, 200 rows of alert log, a bar chart with no numbers
  * the two PDFs (RCA and weekly) looked like different products
"""
import re
import shutil
import subprocess
import sys
import types
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import app          # noqa: F401
import app.metric_labels  # noqa: F401

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub  # noqa: E402
from app import report_names as rn  # noqa: E402
from app import pdf_kit as kit  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend/src"


# ── file names ───────────────────────────────────────────────────────────────

def test_periodic_report_file_names_are_readable_not_storage_keys():
    f = rn.periodic_report_filename({"report_type": "WEEKLY", "scope_type": "ACCOUNT", "scope_label": "U4RAD",
                                     "period_start": datetime(2026, 9, 26, 8, 4), "period_end": datetime(2026, 10, 3, 8, 4)})
    assert f == "CloudOps-Weekly-Report-U4RAD-2026-09-26_to_2026-10-03.pdf"
    assert "83ed45d4" not in f and not f.startswith("weekly_")


def test_names_are_ascii_safe_and_bounded():
    f = rn.periodic_report_filename({"report_type": "CUSTOM", "scope_type": "CLIENT", "scope_label": "Acme Corp \u2014 \u00c9t\u00e9 / \"x\"",
                                     "scope_id": "x", "period_start": datetime(2026, 1, 1), "period_end": datetime(2026, 3, 31)})
    assert f == "CloudOps-Custom-Report-Acme-Corp-Ete-x-2026-01-01_to_2026-03-31.pdf"
    assert re.fullmatch(r"[A-Za-z0-9._-]+", f)
    assert len(rn.slug("x" * 200, 40)) == 40 and rn.slug("") == "" and rn.slug(None) == ""
    assert rn.periodic_report_filename({"report_type": "WEEKLY", "scope_type": "INCIDENT", "scope_label": "#12"}).startswith("CloudOps-Incident-Report-12")


def test_rca_file_name_carries_alert_metric_account_and_date():
    assert rn.rca_report_filename(9467, "Volume Read Operations", "AuroGov Mumbai", "2026-10-04") == \
        "CloudOps-RCA-Alert-9467-Volume-Read-Operations-AuroGov-Mumbai-2026-10-04.pdf"
    assert rn.rca_report_filename(1, None, None, None, "md") == "CloudOps-RCA-Alert-1.md"


def test_endpoints_use_the_readable_names():
    reports = (ROOT / "app/api/reports.py").read_text()
    assert "filename = periodic_report_filename(report)" in reports and 's3_key"].rsplit' not in reports.split("def download_report")[1].split("_EMAIL_RE")[0]
    alerts = (ROOT / "app/api/alerts.py").read_text()
    assert "rca_report_filename(" in alerts and 'rca-report-alert-{alert_id}' not in alerts


# ── table alignment (the shifted-column bug) ────────────────────────────────

def test_every_cell_is_drawn_inside_its_own_column_even_with_chips():
    pdf = kit.new_document(band_subtitle="t")
    seen = []
    real = pdf.cell

    def spy(w=None, h=None, text="", *a, **k):
        seen.append((round(pdf.get_x(), 1), round(float(w or 0), 1), str(text)))
        return real(w, h, text, *a, **k)
    pdf.cell = spy
    cols = [("Time", 28, "L"), ("Severity", 24, "C"), ("Status", 22, "C"), ("Resource", 46, "L"), ("Metric", 36, "L"), ("Value", 24, "R")]
    kit.data_table(pdf, cols, [("26 Sep, 08:18", ("chip", "WARNING", kit.SEVERITY_COLORS["WARNING"]),
                                ("chip", "RESOLVED", kit.STATUS_COLORS["RESOLVED"]), "Aurionpro-Finops", "Target 4xx Errors", "13")])
    starts, x = [], kit.MARGIN
    for _, w, _a in cols:
        starts.append(x)
        x += w
    by_text = {t: (cx, cw) for cx, cw, t in seen}
    # header text sits in its column; the row cell for the same column sits in the same column
    for (header, w, align), col_x, row_text in zip(cols, starts, ["26 Sep, 08:18", "WARNING", "RESOLVED", "Aurionpro-Finops", "Target 4xx Errors", "13"]):
        hx, _ = by_text[header.upper()]
        rx, rw = by_text[row_text]
        assert col_x <= hx < col_x + w, (header, hx, col_x)
        assert col_x - 0.1 <= rx and rx + rw <= col_x + w + 0.1, (row_text, rx, rw, col_x, w)


def test_over_long_values_are_truncated_not_printed_into_the_next_column():
    pdf = kit.new_document(band_subtitle="t")
    pdf.set_font("Helvetica", "", 8.5)
    out = kit.fit_text(pdf, "arn:aws:elasticloadbalancing:ap-south-1:992382489399:loadbalancer/app/xrai-alb/ed0179eb87c820af", 40)
    assert out.endswith("...") and pdf.get_string_width(out) <= 40


# ── report content ───────────────────────────────────────────────────────────

@pytest.fixture()
def eng():
    install_stub("app.db", get_db_cursor=lambda *a, **k: None, get_connection=lambda: None)
    return load_module("app/reports/engine.py")


ARN = "arn:aws:elasticloadbalancing:ap-south-1:992382489399:loadbalancer/app/u4rad-alb/7825df3406bbe617"


def test_arns_become_names(eng):
    assert eng.short_resource(ARN) == "u4rad-alb"
    assert eng.short_resource("arn:aws:lambda:ap-south-1:924922671984:function:EC2StartStopLambda") == "EC2StartStopLambda"
    assert eng.short_resource("i-0abc") == "i-0abc" and eng.short_resource(None) == ""


def test_incident_text_is_rewritten_for_a_reader(eng):
    inc = {"member_alerts": [{"resource_id": "i-046fecd2da9485b99", "resource_name": "U4RAD-UAT-REPORTINGBOT-TEST-ENV"},
                             {"resource_id": ARN, "resource_name": "u4rad-alb"}]}
    cause = eng.humanize_cause("Earliest breach in this incident: disk_used_percent on i-046fecd2da9485b99 at 2026-09-29 16:40:33. "
                               "1 other resource(s) depend on it in the topology graph.", inc)
    assert cause == ("Started with Disk Utilization on U4RAD-UAT-REPORTINGBOT-TEST-ENV at 29 Sep 2026, 16:40 UTC. "
                     "1 other resource depends on it.")
    arn_cause = eng.humanize_cause(f"Earliest breach in this incident: httpcode_target_4xx_count on {ARN} at 2026-09-28 08:21:32.", inc)
    assert "arn:" not in arn_cause and "u4rad-alb" in arn_cause and "Target 4xx Errors" in arn_cause
    assert eng.humanize_incident_title("Correlated breach on i-046fecd2da9485b99 and related resource(s)", inc) == \
        "Correlated breach on U4RAD-UAT-REPORTINGBOT-TEST-ENV and related resources"
    assert "resource(s)" not in eng.humanize_cause("3 other resource(s) depend on it in the topology graph.", inc)
    assert eng.humanize_cause(None, inc) == ""


def test_report_titles_by_kind(eng):
    assert eng.report_title("WEEKLY", "ACCOUNT") == "Weekly Operations Report"
    assert eng.report_title("MONTHLY", "ACCOUNT") == "Monthly Operations Review"
    assert eng.report_title("WEEKLY", "INCIDENT") == "Incident Report"
    assert eng.report_title("CUSTOM", "RESOURCE") == "Resource Report"
    assert eng.report_title("WEEKLY", "CLIENT") == "Weekly Client Report"


def _alert(i, rid, name, rtype, metric, sev, status, day):
    t = datetime(2026, 9, 26, 9, 0) + timedelta(days=min(day, 6), minutes=i % 600)
    return {"id": i, "resource_id": rid, "resource_name": name, "resource_type": rtype, "metric_name": metric, "value": 13.0,
            "severity": sev, "status": status, "triggered_at": t, "region": "ap-south-1"}


def _data(alerts, incidents=()):
    daily = OrderedDict(((datetime(2026, 9, 26) + timedelta(days=d)).date(), 0) for d in range(8))
    for a in alerts:
        daily[a["triggered_at"].date()] += 1
    sc = {"CRITICAL": sum(a["severity"] == "CRITICAL" for a in alerts), "WARNING": sum(a["severity"] == "WARNING" for a in alerts), "OTHER": 0}
    return {"account": {"id": 10, "account_id": "992382489399", "account_name": "U4RAD", "default_region": "ap-south-1", "provider": "aws"},
            "alerts": list(alerts), "incidents": list(incidents), "affected_resources": [], "severity_counts": sc,
            "open_count": sum(a["status"] == "active" for a in alerts), "total_count": len(alerts), "daily_counts": daily}


def test_summary_facts_and_the_one_metric_dominates_note(eng):
    alerts = [_alert(i, ARN, "u4rad-alb", "elb", "httpcode_target_4xx_count", "CRITICAL" if i % 9 == 0 else "WARNING",
                     "active" if i == 0 else "resolved", i % 7) for i in range(80)]
    alerts += [_alert(100 + i, "i-1", "U4RAD-JUMP", "ec2", "disk_used_percent", "WARNING", "resolved", 2) for i in range(10)]
    data = _data(alerts)
    s = eng.summarize(data)
    assert s["total"] == 90 and s["resources"] == 2 and s["top_resources"][0]["name"] == "u4rad-alb"
    assert s["dominant"] == {"label": "Target 4xx Errors", "share": 89}
    paras = dict(eng.summary_paragraphs(data, s, datetime(2026, 9, 26), datetime(2026, 10, 3)))
    assert "90 alerts were raised on 2 resources" in paras["Overview"] and "1 remains open." in paras["Overview"]
    assert "89% of all alerts came from one metric, Target 4xx Errors" in paras["Alert sources"]
    assert paras["Most affected"].startswith("u4rad-alb (") and "Still open" in paras
    quiet = eng.summary_paragraphs(_data([]), eng.summarize(_data([])), datetime(2026, 9, 26), datetime(2026, 10, 3))
    assert quiet == [("Overview", "No alerts were raised between 26 Sep 2026 and 03 Oct 2026.")]


def _render(eng, data, **kw):
    args = dict(report_type="WEEKLY", scope_type="ACCOUNT", scope_id="10", scope_label="U4RAD",
                period_start=datetime(2026, 9, 26, 8, 4), period_end=datetime(2026, 10, 3, 8, 4), data=data, generated_by="admin")
    args.update(kw)
    return eng.render_report_pdf(**args)


def _text(pdf_bytes, tmp_path):
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext not installed")
    p = tmp_path / "r.pdf"
    p.write_bytes(pdf_bytes)
    return subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, text=True).stdout


def test_weekly_pdf_reads_like_a_report_not_a_dump(eng, tmp_path):
    alerts = [_alert(i, ARN if i % 3 else "i-1", "u4rad-alb" if i % 3 else "U4RAD-JUMP", "elb" if i % 3 else "ec2",
                     "httpcode_target_4xx_count" if i % 3 else "disk_used_percent", "CRITICAL" if i % 11 == 0 else "WARNING",
                     "active" if i == 5 else "resolved", i % 7) for i in range(400)]
    inc = [{"id": 565, "title": "Correlated breach on i-1 and related resource(s)", "severity": "WARNING", "status": "active",
            "started_at": datetime(2026, 9, 29, 17, 4), "resolved_at": None, "last_seen_at": datetime(2026, 9, 29, 17, 5),
            "probable_cause": f"Earliest breach in this incident: httpcode_target_4xx_count on {ARN} at 2026-09-29 16:40:33. "
                              "1 other resource(s) depend on it in the topology graph.",
            "member_alerts": [{"resource_id": "i-1", "resource_name": "U4RAD-JUMP", "region": "ap-south-1"}]}]
    pdf = _render(eng, _data(alerts, inc))
    assert pdf[:5] == b"%PDF-"
    text = _text(pdf, tmp_path)
    text_ws = re.sub(r"\s+", " ", text)                       # pdftotext -layout pads columns with runs of spaces
    for needle in ("Weekly Operations Report", "ALERTS RAISED", "OPEN NOW", "Executive Summary", "OVERVIEW", "Alerts per Day",
                   "Most Affected Resources", "Top Alert Sources", "Most Significant Alerts", "Resolution and Current Status",
                   "Target 4xx Errors", "CONFIDENTIAL | AurionPro CloudOps | U4RAD", "Incident #565"):
        assert needle in text_ws, needle
    for gone in ("httpcode_target_4xx_count", "arn:aws", "resource(s)", " -- ", "Incident Timeline / Alerts"):
        assert gone not in text, gone
    assert not re.search(r"^\s*Affected Resources\s*$", text, re.M)       # the old 40-line resource dump heading
    assert text.count("Page ") >= 2


def test_big_reports_stay_short(eng, tmp_path):
    alerts = [_alert(i, f"vol-{i % 40}", None, "ebs", "volumereadops", "WARNING", "resolved", i % 7) for i in range(2429)]
    pdf = _render(eng, _data(alerts))
    if shutil.which("pdfinfo"):
        info = subprocess.run(["pdfinfo", "-"], input=pdf, capture_output=True).stdout.decode()
        pages = int(re.search(r"Pages:\s+(\d+)", info).group(1))
        assert pages <= 8, pages                                        # was 14 for the same volume of data (now incl. a cover page)
    assert eng._MAX_TIMELINE_ROWS <= 60 and eng._MAX_INCIDENT_CARDS <= 10


def test_an_empty_report_renders(eng):
    assert _render(eng, _data([]))[:5] == b"%PDF-"
    assert _render(eng, dict(_data([]), account=None), scope_type="CLIENT", scope_label="Acme")[:5] == b"%PDF-"


def test_both_pdfs_are_built_from_the_one_design_kit():
    for rel in ("app/reports/engine.py", "app/llm/rca_report_pdf.py"):
        src = (ROOT / rel).read_text()
        assert "from app import pdf_kit as kit" in src and "FPDF(" not in src and "FPDF)" not in src, rel
    kit_src = (ROOT / "app/pdf_kit.py").read_text()
    assert 'cell(0, 8, latin1_safe(left), align="L")' in kit_src and "AURIONPRO" in kit_src
    assert "multi_cell" in kit_src and 'align="J"' not in kit_src          # no justified text


# ── front end ────────────────────────────────────────────────────────────────

def _t(rel):
    return (FE / rel).read_text()


def test_downloads_no_longer_open_a_tab():
    for rel, marker in (("pages/Alerts.jsx", "rcaReportUrl(a.id"), ("components/AlertInvestigation.jsx", "rcaReportUrl(id"),
                        ("pages/Reports.jsx", "reportDownloadUrl(r.id)")):
        text = _t(rel)
        i = text.index(marker)
        window = text[max(0, i - 260):i + 260]
        assert "DownloadButton" in window and 'target="_blank"' not in window, rel
    assert "async function downloadFile" in _t("api/api.js") and "createObjectURL" in _t("api/api.js")
    btn = _t("components/DownloadButton.jsx")
    assert "e.stopPropagation()" in btn and "Preparing" in btn and 'role="alert"' in btn


def test_reports_page_error_is_a_sentence_and_the_generate_button_is_centred():
    assert 'describeApiError(e, "queuing the report")' in _t("pages/Reports.jsx")
    assert "err.requestId = res.headers.get(\"x-request-id\")" in _t("api/api.js")
    assert re.search(r"\.c-btn-primary \{[^}]*justify-content:\s*center", _t("pages/Compliance.css"), re.S)
    assert ".rp-go .c-btn-primary" in _t("pages/Reports.css") and "justify-content: center" in _t("pages/Reports.css")


def test_security_search_box_is_styled_like_the_selects():
    css = _t("pages/SecurityFindings.css")
    assert re.search(r"\.sec-field select,\s*\.sec-field input\[type=\"search\"\]\s*\{[^}]*background:\s*var\(--bg-input\)", css, re.S)


def test_resolved_share_never_rounds_up_to_100_while_something_is_open(eng, tmp_path):
    alerts = [_alert(i, "i-1", "U4RAD-JUMP", "ec2", "disk_used_percent", "WARNING", "active" if i == 0 else "resolved", 1) for i in range(2429)]
    text = re.sub(r"\s+", " ", _text(_render(eng, _data(alerts)), tmp_path))
    assert "2,428 of 2,429 alerts were resolved" in text and "(99%)" in text and "(100%)" not in text
    clean = re.sub(r"\s+", " ", _text(_render(eng, _data([a for a in alerts if a["status"] == "resolved"])), tmp_path))
    assert "(100%)" in clean and "Nothing remains open." in clean


def test_both_reports_carry_the_real_brand_mark_and_the_periodic_report_keeps_its_cover(eng, tmp_path):
    """Merged with upstream c0a75e6 (real CloudOps mark instead of the boxed AslOps placeholder): the mark now lives in
    the shared kit, so the RCA report gets it too."""
    assert (ROOT / "app/reports/assets/cloudops_mark.png").exists()
    weekly = _render(eng, _data([_alert(1, "i-1", "U4RAD-JUMP", "ec2", "disk_used_percent", "WARNING", "active", 1)]))
    assert b"/Subtype /Image" in weekly
    rca_mod = load_module("app/llm/rca_report_pdf.py")
    rca = rca_mod.render_pdf("# T\n\n- **Account:** A\n\n## Executive Summary\n\ntext", "T", severity="WARNING", alert_ref="Alert #1")
    assert b"/Subtype /Image" in rca
    text = _text(weekly, tmp_path)
    first_page = text.split("\x0c")[0]
    assert "Weekly Operations Report" in first_page and "AURIONPRO" in first_page and "Account: U4RAD" in first_page
    assert "Confidential: for the intended recipient only" in first_page and " -- " not in first_page
    assert "Page 1/" not in first_page and "Page 2/" in text                       # no band/footer on the cover; numbering continues
    kit_src = (ROOT / "app/pdf_kit.py").read_text()
    assert "aslops_logo" not in kit_src and "cloudops_mark.png" in kit_src
