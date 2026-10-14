# tests/test_synthetic_report.py -- the Synthetic Monitoring report: scope parsing, numbers, PDF rendering.
import sys
from datetime import datetime, date

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import pytest
from tests.conftest import load_module, install_stub

sr = None


@pytest.fixture(autouse=True)
def _load_report():
    global sr
    import app.pdf_kit  # noqa: F401  (real package first, so the db stub only replaces app.db)
    install_stub("app.db", get_db_cursor=lambda *a, **k: None)
    sr = load_module("app/reports/synthetic_report.py")
    yield


START, END = datetime(2026, 9, 14), datetime(2026, 10, 14)


def test_parse_scope():
    assert sr.parse_scope("12") == (12, None)
    assert sr.parse_scope("12:34") == (12, 34)
    for bad in ("", "x", "12:", "12:x", "1:2:3"):
        try:
            r = sr.parse_scope(bad)
        except ValueError:
            continue
        assert r is None or r[0] is None, bad


def _data():
    checks = [{"id": 1, "name": "Site", "check_type": "https", "target": "https://a.example", "enabled": 1,
               "contact_count": 0, "cert_warn_days": None},
              {"id": 2, "name": "API", "check_type": "tcp", "target": "b:443", "enabled": 1, "contact_count": 2}]
    daily = {1: [{"day": date(2026, 10, 1), "probes": 100, "ok": 98, "avg_ms": 200, "p95_ms": 400, "max_ms": 900,
                  "min_cert_days": 5}],
             2: [{"day": date(2026, 10, 1), "probes": 100, "ok": 100, "avg_ms": 50, "p95_ms": 80, "max_ms": 120,
                  "min_cert_days": None}]}
    outages = [{"resource_id": "synthetic-1", "triggered_at": datetime(2026, 9, 1), "resolved_at": datetime(2026, 9, 14, 1),
                "status": "resolved"},
               {"resource_id": "synthetic-1", "triggered_at": datetime(2026, 10, 13, 22), "resolved_at": None, "status": "open"}]
    certs = {1: {"cert_days_left": 5, "cert_not_after": datetime(2026, 10, 19), "cert_issuer": "CA",
                 "cert_subject": "a.example", "tls_version": "TLSv1.3"}}
    return {"account": {"id": 2, "account_name": "AuroGov", "account_id": "123456789012", "provider": "aws"}, "checks": checks, "daily": daily, "today": {},
            "outages": outages, "certs": certs, "cert_alerts": {1: 1}, "period_start": START, "period_end": END}


def test_summarize_numbers_and_flags():
    s = sr.summarize(_data())
    one = next(p for p in s["per_check"] if p["check"]["id"] == 1)
    assert one["availability"] == 98.0 and one["failed"] == 2 and one["status"] == "POOR"
    assert s["probes"] == 200 and s["failed"] == 2 and s["avg_availability"] == 99.0
    assert s["outage_count"] == 2
    assert [c["id"] for c in s["no_contacts"]] == [1]            # check 1 would never tell anyone
    assert [p["check"]["id"] for p in s["certs_due"]] == [1]     # 5 days left is inside the 30-day window


def test_outage_minutes_are_clipped_to_the_period():
    s = sr.summarize(_data())
    first = s["outages"][0]
    assert first["minutes"] == 60                                # only the hour after the period start counts
    assert s["outages"][1]["open"] is True


def test_summarize_handles_no_data():
    d = _data()
    d.update(daily={}, outages=[], certs={}, cert_alerts={})
    s = sr.summarize(d)
    assert s["avg_availability"] is None and s["probes"] == 0
    assert all(p["status"] == "NO DATA" for p in s["per_check"])


def test_pdf_renders_for_normal_and_empty_data():
    for data in (_data(), dict(_data(), daily={}, outages=[], certs={}, cert_alerts={})):
        pdf = sr.render_synthetic_pdf(report_type="monthly", scope_id="2", scope_label="AuroGov / all checks",
                                      period_start=START, period_end=END, data=data, generated_by="tester")
        assert pdf[:4] == b"%PDF" and len(pdf) > 2000


def test_pdf_never_prints_contact_addresses():
    d = _data()
    d["checks"][1]["contacts"] = [{"email": "secret@corp.example"}]
    pdf = sr.render_synthetic_pdf(report_type="weekly", scope_id="2", scope_label="x", period_start=START,
                                  period_end=END, data=d, generated_by="t")
    assert b"secret@corp.example" not in pdf
