"""
app/email/templates.py + the HTML/multipart path in app/email/mailer.py.

What must stay true:
  * every dynamic value is HTML-escaped (a resource tag, scope label or alert detail can hold markup);
  * every mail has a plain-text twin carrying the same facts;
  * times are shown in IST with UTC beside them;
  * the alert mail names the resource a person recognises and deep-links to the alert;
  * a link is only ever rendered as a button when it is a real http(s) URL;
  * the From name is "CloudOps ..." on the unchanged MAIL_FROM address, and a non-ASCII subject survives encoding.
"""
import email
import os
from datetime import datetime

import pytest

from tests.conftest import load_module, install_stub, install_real_templates

ARN = "arn:aws:elasticloadbalancing:ap-south-1:992382489399:loadbalancer/app/xrai-alb/ed0179eb87c820af"
ALERT = {"id": 812, "severity": "CRITICAL", "priority": 1, "metric_name": "httpcode_target_4xx_count",
         "resource_id": ARN, "account_name": "U4RAD", "value": 121, "threshold": 28,
         "triggered_at": datetime(2026, 11, 2, 6, 3, 13), "region": "ap-south-1"}


@pytest.fixture
def t():
    return load_module("app/email/templates.py")


@pytest.fixture
def mailer(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_FROM", "SMTP_FROM",
              "MAIL_FROM_NAME", "PUBLIC_APP_URL", "SMTP_SECURITY", "SMTP_USE_TLS"):
        monkeypatch.delenv(k, raising=False)
    install_real_templates()
    return load_module("app/email/mailer.py")


# ── resource / time / value wording ──────────────────────────────────

def test_short_resource_names(t):
    assert t.short_resource(ARN) == "xrai-alb"
    assert t.short_resource("arn:aws:elasticloadbalancing:ap-south-1:1:targetgroup/api-tg/0123456789abcdef") == "api-tg"
    assert t.short_resource("arn:aws:rds:ap-south-1:1:db:orders-db") == "orders-db"
    assert t.short_resource("synthetic-3 (Payment API)") == "Payment API"
    assert t.short_resource("i-0abc123") == "i-0abc123"
    assert t.short_resource(None) == ""


def test_times_are_ist_with_utc_beside_them(t):
    assert t.fmt_when(datetime(2026, 11, 2, 6, 3, 13)) == "02 Nov 2026, 11:33 IST (06:03 UTC)"
    assert t.fmt_when("2026-11-02 06:03:13") == "02 Nov 2026, 11:33 IST (06:03 UTC)"
    assert t.fmt_when("not a date") == "" and t.fmt_when(None) == ""


def test_report_periods_are_dates_not_raw_timestamps(t):
    assert t.fmt_period("2026-10-22 09:09:44", "2026-10-29 09:09:44") == "22–29 Oct 2026"
    assert t.fmt_period(datetime(2026, 9, 28), datetime(2026, 10, 5)) == "28 Sep 2026 – 05 Oct 2026"


def test_values_carry_units(t):
    assert t.value_text("cloud_cost_jump", 12.4) == "$12.40"
    assert t.value_text("httpcode_target_4xx_count", 121) == "121"
    assert t.value_text("anything", None) == "-"


# ── alert mail ───────────────────────────────────────────────────────

def test_alert_subject_is_short_and_names_the_resource(t):
    m = t.alert_email("opened", ALERT, "http://app")
    assert m["subject"].startswith("[P1 CRITICAL] ")
    assert "xrai-alb" in m["subject"] and "U4RAD" in m["subject"] and "121" in m["subject"] and "28" in m["subject"]
    assert "arn:aws" not in m["subject"] and len(m["subject"]) < 140


def test_alert_body_has_facts_time_and_a_deep_link(t):
    m = t.alert_email("opened", ALERT, "http://app")
    for needle in ("xrai-alb", ARN, "U4RAD", "ap-south-1", "02 Nov 2026, 11:33 IST (06:03 UTC)",
                   "http://app/alerts?alert=812", "View alert"):
        assert needle in m["html"], needle
    for needle in ("xrai-alb", "U4RAD", "11:33 IST", "http://app/alerts?alert=812"):
        assert needle in m["text"], needle


def test_escalated_alert_names_the_group(t):
    m = t.alert_email("escalated", {**ALERT, "group_name": "L2-platform"}, "http://app")
    assert "ESCALATED" in m["subject"] and "L2-platform" in m["subject"]
    assert "L2-platform" in m["html"] and "L2-platform" in m["text"]


def test_alert_without_id_links_to_the_list_and_without_url_has_no_button(t):
    assert "http://app/alerts" in t.alert_email("opened", {**ALERT, "id": None}, "http://app")["html"]
    assert "?alert=" not in t.alert_email("opened", {**ALERT, "id": None}, "http://app")["html"]
    assert "View alert" not in t.alert_email("opened", ALERT, "")["html"]


def test_every_dynamic_value_is_escaped(t):
    evil = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
    m = t.alert_email("opened", {**ALERT, "resource_id": evil, "account_name": evil, "detail": evil,
                                 "metric_name": evil, "group_name": evil}, "http://app")
    assert "<script>" not in m["html"] and "<img src=x" not in m["html"] and "onerror=alert(2)>" not in m["html"]
    assert "&lt;script&gt;" in m["html"]


def test_a_non_http_url_never_becomes_a_link(t):
    html, text = t.render(title="x", button=("Go", "javascript:alert(1)"))
    assert "javascript:" not in html and "javascript:" not in text
    html, _ = t.render(title="x", button=("Go", 'http://a/"onclick="x'))
    assert 'onclick="x' not in html


def test_severity_colours_differ(t):
    crit = t.alert_email("opened", ALERT, "")["html"]
    warn = t.alert_email("opened", {**ALERT, "severity": "WARNING"}, "")["html"]
    assert "#C62828" in crit and "#C62828" not in warn and "#B26A00" in warn


def test_builders_survive_junk_input(t):
    assert t.alert_email("opened", {}, "")["subject"]
    assert t.alert_email("opened", None, None)["html"]
    assert t.alert_email("opened", {**ALERT, "value": "n/a", "threshold": object()}, "")["text"]


# ── other mails ──────────────────────────────────────────────────────

def test_account_created_names_username_role_and_link(t):
    m = t.account_created_email(username="asha", role="viewer", link="https://h/reset-password?token=abc", hours=24,
                                created_by="admin")
    assert m["subject"] == "Set up your CloudOps account"
    for s in (m["html"], m["text"]):
        assert "asha" in s and "Viewer" in s and "https://h/reset-password?token=abc" in s and "24 hours" in s
    assert "by admin" in m["text"]


def test_admin_reset_says_who_asked_and_self_reset_does_not_blame_an_admin(t):
    a = t.password_reset_email(username="asha", link="https://h/r?token=1", valid_text="24 hours, single use",
                               requested_by="admin")
    assert "administrator (admin)" in a["text"]
    s = t.self_reset_email(username="asha", link="https://h/r?token=1", valid_text="30 minutes, single use")
    assert "administrator" not in s["text"].split("--")[0].lower().replace("contact your cloudops administrator", "")
    assert "30 minutes" in s["text"] and "didn't ask for this" in s["text"]


def test_report_mail_subject_and_attachment_name(t):
    rep = {"report_type": "WEEKLY", "scope_type": "ACCOUNT", "scope_label": "AuroGov Mumbai", "scope_id": "3",
           "period_start": datetime(2026, 10, 22, 9, 9, 44), "period_end": datetime(2026, 10, 29, 9, 9, 44),
           "s3_key": "reports/weekly_20261022-20261029_68ab9154.pdf"}
    name = t.report_attachment_name(rep)
    assert name.endswith(".pdf") and "68ab9154" not in name and "AuroGov" in name and "2026-10-22" in name
    m = t.report_email(rep, filename=name, size_bytes=27 * 1024, app_url="http://app")
    assert m["subject"] == "CloudOps Weekly Report · AuroGov Mumbai · 22–29 Oct 2026"
    assert "--" not in m["subject"]
    assert "22–29 Oct 2026" in m["html"] and "27 KB" in m["html"] and "09:09:44" not in m["html"]
    assert "http://app/reports" in m["html"]


def test_report_label_cannot_inject_markup_or_line_breaks(t):
    rep = {"report_type": "WEEKLY", "scope_label": "<b>x</b>\r\nBcc: evil@x.com", "period_start": None, "period_end": None}
    m = t.report_email(rep, app_url="")
    assert "\r" not in m["subject"] and "\n" not in m["subject"]
    assert "<b>x</b>" not in m["html"]


def test_cost_and_synthetic_test_mails_look_like_the_real_thing(t):
    c = t.cost_test_email("http://app")
    assert c["subject"] == "[CloudOps] Test: cost-jump alert recipients"
    assert "Example alert limit" in c["html"] and "http://app/cloud-cost" in c["html"]
    s = t.synthetic_test_email(check_name="Aurionpro", target="https://www.aurionpro.com/", sent_by="admin", app_url="http://app")
    assert s["subject"] == "[CloudOps] Test: alert contacts for Aurionpro" and "admin" in s["html"]


def test_from_plain_turns_label_lines_into_a_table_and_the_link_into_a_button(t):
    body = ("Aurionpro is DOWN.\n\nTarget:       https://www.aurionpro.com/\nAccount:      Aurionpro\n"
            "Failed probes in a row: 3\nLast error:   timeout\n\nOpen in CloudOps: http://app/synthetic-checks")
    m = t.from_plain("[CloudOps] DOWN: Aurionpro", body, accent="CRITICAL", eyebrow="Synthetic check")
    assert "Aurionpro is DOWN." in m["html"] and "timeout" in m["html"]
    assert 'href="http://app/synthetic-checks"' in m["html"]
    assert "https://www.aurionpro.com/" in m["text"] and m["subject"] == "[CloudOps] DOWN: Aurionpro"


# ── mailer: multipart, From, encoding ────────────────────────────────

def _capture(mailer, monkeypatch):
    got = {}

    def fake_deliver(c, to_addr, msg):
        got["msg"], got["to"] = msg, to_addr
        return True, ""
    monkeypatch.setattr(mailer, "_deliver", fake_deliver)
    monkeypatch.setenv("SMTP_HOST", "smtp.example")
    monkeypatch.setenv("MAIL_FROM", "FinOps HCS <cloudops@aurionpro.com>")
    return got


def test_html_mail_is_multipart_alternative_with_text_first(mailer, monkeypatch):
    got = _capture(mailer, monkeypatch)
    ok, err = mailer.send_email_ex("a@x.com", "[CRITICAL] x · y → z", "plain body", "<p>html body</p>", "Alerts")
    assert ok and err == ""
    parsed = email.message_from_string(got["msg"].as_string())
    assert parsed.get_content_type() == "multipart/alternative"
    kinds = [p.get_content_type() for p in parsed.get_payload()]
    assert kinds == ["text/plain", "text/html"]
    assert parsed.get_payload()[0].get_payload(decode=True) == b"plain body"


def test_plain_only_mail_is_unchanged(mailer, monkeypatch):
    got = _capture(mailer, monkeypatch)
    assert mailer.send_email("a@x.com", "s", "just text") is True
    assert got["msg"].get_content_type() == "text/plain"


def test_from_name_is_cloudops_on_the_same_address(mailer, monkeypatch):
    got = _capture(mailer, monkeypatch)
    mailer.send_email("a@x.com", "s", "b", None, "Alerts")
    assert email.message_from_string(got["msg"].as_string())["From"] == "CloudOps Alerts <cloudops@aurionpro.com>"
    mailer.send_email("a@x.com", "s", "b")
    assert email.message_from_string(got["msg"].as_string())["From"] == "CloudOps <cloudops@aurionpro.com>"
    monkeypatch.setenv("MAIL_FROM_NAME", "Aurionpro CloudOps")
    mailer.send_email("a@x.com", "s", "b", None, "Alerts")
    assert email.message_from_string(got["msg"].as_string())["From"] == "Aurionpro CloudOps Alerts <cloudops@aurionpro.com>"
    assert mailer._envelope_sender(os.environ["MAIL_FROM"]) == "cloudops@aurionpro.com"


def test_non_ascii_subject_is_encoded_not_mangled(mailer, monkeypatch):
    got = _capture(mailer, monkeypatch)
    mailer.send_email("a@x.com", "[P1 CRITICAL] Errors · xrai-alb → ops", "b", "<p>b</p>")
    raw = got["msg"].as_string()
    assert "=?utf-8?" in raw.split("\n\n")[0].lower()
    decoded = str(email.header.make_header(email.header.decode_header(email.message_from_string(raw)["Subject"])))
    assert decoded == "[P1 CRITICAL] Errors · xrai-alb → ops"


def test_crlf_in_subject_is_still_refused_with_html(mailer, monkeypatch):
    got = _capture(mailer, monkeypatch)
    ok, _ = mailer.send_email_ex("a@x.com", "s\r\nBcc: evil@x.com", "b", "<p>b</p>")
    assert ok is False and "msg" not in got


def test_report_mail_has_a_pdf_attachment_with_a_clean_name(mailer, monkeypatch):
    got = _capture(mailer, monkeypatch)
    rep = {"report_type": "WEEKLY", "scope_type": "ACCOUNT", "scope_label": "AuroGov Mumbai", "scope_id": "3",
           "period_start": datetime(2026, 10, 22), "period_end": datetime(2026, 10, 29),
           "s3_key": "reports/weekly_20261022-20261029_68ab9154.pdf"}
    assert mailer.send_report_email("a@x.com", rep, b"%PDF-1.4 fake") is True
    parsed = email.message_from_string(got["msg"].as_string())
    assert parsed.get_content_type() == "multipart/mixed"
    types = [p.get_content_type() for p in parsed.walk()]
    assert "text/html" in types and "text/plain" in types and "application/pdf" in types
    pdf = [p for p in parsed.walk() if p.get_content_type() == "application/pdf"][0]
    assert pdf.get_filename().startswith("CloudOps-Weekly-Report-AuroGov") and "68ab9154" not in pdf.get_filename()
    assert pdf.get_payload(decode=True) == b"%PDF-1.4 fake"
    assert email.message_from_string(got["msg"].as_string())["From"].startswith("CloudOps Reports <")


# ── link safety ──────────────────────────────────────────────────────

def test_plain_http_public_url_is_flagged_but_localhost_and_https_are_not(mailer, monkeypatch):
    monkeypatch.setenv("PUBLIC_APP_URL", "http://35.154.149.94")
    assert "unencrypted" in mailer.public_url_problem()
    monkeypatch.setenv("SMTP_HOST", "smtp.example")
    assert any("PUBLIC_APP_URL" in p for p in mailer.config_problems())
    monkeypatch.setenv("PUBLIC_APP_URL", "https://cloudops.aurionpro.com")
    assert mailer.public_url_problem() == ""
    monkeypatch.setenv("PUBLIC_APP_URL", "http://localhost:5173")
    assert mailer.public_url_problem() == ""


# ── button rendering ─────────────────────────────────────────────────

def test_button_padding_is_on_the_cell_and_colour_is_brand_not_severity(t):
    """Clients that drop padding from <a> collapsed the button to a highlighted word; the padding lives on the <td> now.
    The button is brand teal for every severity (the stripe and chip already carry the alert colour)."""
    for sev in ("CRITICAL", "WARNING", "INFO"):
        html = t.alert_email("opened", {**ALERT, "severity": sev}, "http://app")["html"]
        cell = html.split('bgcolor="#0F766E"', 1)[1].split("</td>", 1)[0]
        assert "padding:12px 28px" in cell and "View alert" in cell
        assert 'bgcolor="#C62828"' not in html and 'bgcolor="#B26A00"' not in html


def test_test_mails_are_not_green(t):
    assert "#2E7D32" not in t.synthetic_test_email(check_name="x", target="https://x", app_url="http://app")["html"]
    assert "#2E7D32" not in t.test_notification_email("http://app", "ch")["html"]
    # green is still what a recovery looks like
    assert "#2E7D32" in t.render(title="Recovered", accent="OK")[0]


def test_fact_cells_set_their_own_font(t):
    """Outlook and some webmail do not inherit font-family through nested tables (values fell back to Times/black)."""
    html = t.alert_email("opened", ALERT, "")["html"]
    assert html.count("font-family:-apple-system") >= 2 * 8
