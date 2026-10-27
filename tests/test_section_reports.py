# tests/test_section_reports.py -- cost, host agents, environments, security and SLO reports: numbers, PDFs, permissions.
import sys
from datetime import datetime, date, timedelta

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import pytest
from tests.conftest import load_module, install_stub

START, END = datetime(2026, 9, 27), datetime(2026, 10, 27)
M = {}


@pytest.fixture(autouse=True)
def _load():
    import app.pdf_kit  # noqa: F401  real package first so only app.db is replaced
    install_stub("app.db", get_db_cursor=lambda *a, **k: None, get_connection=lambda: None)
    import app.reports.common  # noqa: F401
    for name in ("cost_report", "host_report", "environment_report", "security_report", "slo_report", "registry"):
        M[name] = load_module(f"app/reports/{name}.py")
    yield


def _is_pdf(b):
    return isinstance(b, (bytes, bytearray)) and bytes(b[:5]) == b"%PDF-" and len(b) > 1500


# ── cost ─────────────────────────────────────────────────────────────

def _rep(daily, groups, billed):
    return {"state": "ok", "periods": [{"key": d.isoformat()} for d in daily], "totals": {"billed_usd": billed},
            "groups": groups, "through_date": max(daily).isoformat(), "reconciliation": {"ok": True},
            "out_of_scope": {"usd": 0}}


def _cost_data(with_monthly=True):
    today = date(2026, 10, 27)
    days = [today - timedelta(days=i) for i in range(29, -1, -1)]
    daily = {d: 1.0 for d in days}
    daily[days[10]] = 3.5
    groups = [{"label": "Prod", "total": 25.0, "share_pct": 80.0, "regions": ["ap-south-1"]},
              {"label": "Dev", "total": 7.5, "share_pct": 20.0, "regions": ["ap-south-1"]}]
    aws = {"provider": "aws", "name": "AWS", "available": True, "state": "ok", "daily": daily, "groups": groups,
           "rep": _rep(days, groups, 32.5)}
    azure = {"provider": "azure", "name": "Azure", "available": False, "state": "disabled", "daily": {}, "groups": [], "rep": None}
    monthly = {"providers": [{"provider": "aws", "name": "AWS", "available": True, "this_month_usd": 27.0, "last_month_usd": 30.0,
                              "same_period_last_month_usd": 24.0}]} if with_monthly else None
    tiers = {"tiers": [{"provider": "aws", "tier": "critical", "label": "Critical (2 min)", "usd": 20.0, "share_pct": 80.0, "calls": 900,
                        "halve_saving_usd": 10.0, "user_triggered": False}], "levers": [{"kind": "x", "text": "Critical is the largest line."}]} if with_monthly else None
    return {"scope": "all", "start": days[0], "end": today, "today": today, "providers": [aws, azure], "monthly": monthly, "tiers": tiers,
            "tier_days": 30, "notes": [], "provisional_days": 2, "period_start": START, "period_end": END}


def test_cost_scope_parsing():
    cr = M["cost_report"]
    assert cr.parse_scope("ALL") == "all" and cr.parse_scope("Azure") == "azure"
    with pytest.raises(ValueError):
        cr.parse_scope("12")
    with pytest.raises(ValueError):
        cr.parse_scope("")


def test_cost_summarize_numbers():
    s = M["cost_report"].summarize(_cost_data())
    assert s["total"] == 32.5 and s["clouds"] == 1 and [r["name"] for r in s["unavailable"]] == ["Azure"]
    assert s["peak"]["usd"] == 3.5 and s["peak"]["provisional"] is False
    assert s["avg_settled_day"] is not None and s["provisional_in_period"] is True
    assert s["accounts"][0]["label"] == "Prod"


def test_cost_change_pct_never_invents_a_number():
    cp = M["cost_report"].change_pct
    assert cp(27, 24) == 12.5 and cp(10, 0) is None and cp(10, None) is None


def test_cost_month_total_row_only_for_all_scope():
    cr = M["cost_report"]
    d = _cost_data()
    d["monthly"]["providers"].append({"provider": "gcp", "name": "GCP", "available": True, "this_month_usd": 1.0, "last_month_usd": 1.0,
                                      "same_period_last_month_usd": 1.0})
    assert [r["name"] for r in cr.monthly_rows(d["monthly"], "all")][-1] == "All clouds"
    assert [r["name"] for r in cr.monthly_rows(d["monthly"], "aws")] == ["AWS"]


def test_cost_long_period_is_charted_weekly():
    cr = M["cost_report"]
    start = date(2026, 1, 1)
    labels, values, weekly = cr._bucket({start: 2.0}, start, start + timedelta(days=200))
    assert weekly is True and len(labels) < 40 and sum(values) == 2.0


def test_cost_pdf_renders_normal_unavailable_and_empty():
    cr = M["cost_report"]
    for d in (_cost_data(), _cost_data(False)):
        pdf = cr.render_cost_pdf(report_type="MONTHLY", scope="all", scope_label="Cloud cost: all clouds", period_start=START,
                                 period_end=END, data=d, generated_by="tester")
        assert _is_pdf(pdf)
    d = _cost_data()
    d["providers"] = [{"provider": "aws", "name": "AWS", "available": False, "state": "error", "daily": {}, "groups": [], "rep": None}]
    d["monthly"] = d["tiers"] = None
    assert _is_pdf(cr.render_cost_pdf(report_type="WEEKLY", scope="aws", scope_label="Cloud cost: AWS", period_start=START,
                                      period_end=END, data=d, generated_by="tester"))
    assert "No billing data" in cr.summary_paragraphs(cr.summarize(d), d, None, None)[0][1]


def test_cost_gather_uses_the_cost_pages_own_functions():
    cr = M["cost_report"]
    calls = {}

    class Fake:
        PROVISIONAL_DAYS = 2

        @staticmethod
        def cost_explorer_report(**kw):
            calls["kw"] = kw
            return {"state": "disabled"}                      # no "periods": nothing to chart

        @staticmethod
        def _daily_totals(rep):
            return {}

        @staticmethod
        def monthly_totals(current_user):
            calls["monthly_user"] = current_user
            return {"providers": []}

        @staticmethod
        def get_connection():
            class C:
                def cursor(self): return type("Cur", (), {"close": lambda s: None})()
                def close(self): pass
            return C()

        @staticmethod
        def _fetch(days, cur):
            return [], False

        @staticmethod
        def env_polling_state():
            return {"enabled": False}

        @staticmethod
        def build_tier_cost(rows, days, env):
            return {"tiers": [], "levers": [], "notes": []}

    now = datetime.utcnow()
    d = cr.gather_cost_data("aws", now - timedelta(days=30), now, "amy", _usage=Fake)
    assert calls["kw"]["cloudops_only"] is True and calls["kw"]["provider"] == "aws" and calls["kw"]["scope"] == "onboarded"
    assert calls["monthly_user"]["role"] == "admin"
    assert d["providers"][0]["available"] is False and d["providers"][0]["state"] == "disabled"


# ── host agents ──────────────────────────────────────────────────────

NOW = datetime(2026, 10, 27, 12, 0)


def _host_data():
    h1 = {"id": 1, "hostname": "web-1", "os": "linux", "agent_version": "0.2.1", "status": "active", "last_seen_at": NOW - timedelta(seconds=30),
          "enrolled_at": datetime(2026, 1, 1), "resource_id": "i-1", "instance_id": "i-1"}
    h2 = {"id": 2, "hostname": "db-1", "os": "linux", "agent_version": "0.1.9", "status": "active", "last_seen_at": NOW - timedelta(hours=5),
          "enrolled_at": datetime(2026, 1, 1), "resource_id": "host:db", "instance_id": None}
    h3 = {"id": 3, "hostname": "old", "os": "windows", "agent_version": "0.2.1", "status": "revoked", "last_seen_at": None,
          "enrolled_at": datetime(2026, 1, 1), "resource_id": None, "instance_id": None}
    alerts = [{"host_id": 2, "heartbeat": True, "severity": "CRITICAL", "status": "firing", "triggered_at": NOW - timedelta(hours=3), "resolved_at": None},
              {"host_id": 2, "heartbeat": True, "severity": "WARNING", "status": "resolved", "triggered_at": NOW - timedelta(hours=4),
               "resolved_at": NOW - timedelta(hours=2, minutes=30)},
              {"host_id": 1, "heartbeat": False, "severity": "CRITICAL", "status": "firing", "triggered_at": NOW - timedelta(days=1), "resolved_at": None}]
    return {"account": {"id": 2, "account_name": "AuroGov", "account_id": "123", "provider": "aws"}, "hosts": [h1, h2, h3], "now": NOW,
            "period_start": NOW - timedelta(days=30), "period_end": NOW, "stale_seconds": 180, "watchdog": {2: {"state_since": NOW - timedelta(hours=3), "offline_reason": None}},
            "services": {1: [{"service_name": "nginx", "state": "running", "state_since": NOW}, {"service_name": "cron", "state": "failed", "state_since": NOW}]},
            "alerts": alerts, "usage_start": NOW - timedelta(days=30),
            "usage": {1: [{"contract": "cpu_percent", "label": None, "avg": 85.0, "max": 99.0, "n": 10},
                          {"contract": "disk_used_percent", "label": "/", "avg": 50.0, "max": 60.0, "n": 10},
                          {"contract": "disk_used_percent", "label": "data", "avg": 80.0, "max": 91.0, "n": 10}]}}


def test_host_derive_state_matches_the_page_rule():
    ds = M["host_report"].derive_state
    assert ds("revoked", NOW, NOW, 180) == "revoked" and ds("active", None, NOW, 180) == "offline"
    assert ds("active", NOW - timedelta(seconds=100), NOW, 180) == "online"
    assert ds("active", NOW - timedelta(seconds=500), NOW, 180) == "stale"
    assert ds("active", NOW - timedelta(seconds=1000), NOW, 180) == "offline"


def test_host_summarize_numbers_and_flags():
    s = M["host_report"].summarize(_host_data())
    assert s["active"] == 2 and s["revoked"] == 1 and s["online"] == 1 and [p["host"]["hostname"] for p in s["offline"]] == ["db-1"]
    db = next(p for p in s["per_host"] if p["host"]["id"] == 2)
    assert db["downtime_min"] == 240                       # 4 h: the warning and the critical alert overlap and are merged, not added
    assert db["reporting"] < 100 and len(db["gaps"]) == 2
    web = next(p for p in s["per_host"] if p["host"]["id"] == 1)
    assert web["alerts_critical"] == 1 and web["alerts_warning"] == 0
    assert web["use"]["disk"]["label"] == "data"           # fullest mount wins
    assert [h["host"]["hostname"] for h in s["disk_high"]] == ["web-1"] and [h["host"]["hostname"] for h in s["cpu_high"]] == ["web-1"]
    assert [p["host"]["hostname"] for p in s["outdated"]] == ["db-1"] and s["newest_version"] == "0.2.1"
    assert len(s["bad_services"]) == 1


def test_host_enrolled_mid_period_is_measured_from_enrolment():
    d = _host_data()
    d["hosts"][1]["enrolled_at"] = NOW - timedelta(hours=4)
    d["alerts"] = [{"host_id": 2, "heartbeat": True, "severity": "CRITICAL", "status": "firing", "triggered_at": NOW - timedelta(hours=2), "resolved_at": None}]
    db = next(p for p in M["host_report"].summarize(d)["per_host"] if p["host"]["id"] == 2)
    assert db["reporting"] == 50.0


def test_host_pdf_renders_for_fleet_empty_and_missing_tables():
    hr = M["host_report"]
    for d in (_host_data(), dict(_host_data(), hosts=[], alerts=[], usage={}, services={}, watchdog={})):
        assert _is_pdf(hr.render_host_pdf(report_type="monthly", scope_id="2", scope_label="AuroGov", period_start=d["period_start"],
                                          period_end=d["period_end"], data=d, generated_by="tester"))


def test_host_scope_parsing():
    assert M["host_report"].parse_scope("2:7") == (2, 7) and M["host_report"].parse_scope("2") == (2, None)
    with pytest.raises(ValueError):
        M["host_report"].parse_scope("x")


# ── environments ─────────────────────────────────────────────────────

def _blk(total, ok, nonstd=0, inv=0, miss=0, nc=0, pend=0):
    pct = lambda n: round(100 * n / total, 1) if total else 0.0
    tags = {k: {"n": n, "pct": pct(n)} for k, n in (("ok", ok), ("nonstandard", nonstd), ("invalid", inv), ("missing", miss), ("not_checked", nc), ("pending", pend))}
    chk = ok + nonstd + inv + miss
    return {"total": total, "by_tag_state": tags, "warnings": 2, "checkable": chk, "ok_of_checkable_pct": round(100 * ok / chk, 1) if chk else 0.0}


def _env_data(empty=False):
    blk = _blk(0, 0) if empty else _blk(100, 60, miss=20, nc=20)
    acct = None if empty else dict(blk, account_id=2, account_name="AuroGov", provider="aws",
                                   types=[dict(_blk(60, 30, miss=20, nc=10), resource_type="ec2"), dict(_blk(40, 30, nc=10), resource_type="s3")])
    trend = [] if empty else [{"date": f"2026-10-{d:02d}", "checkable": 80, "ok_of_checkable_pct": 70.0 + d / 2} for d in range(1, 11)]
    return {"account": {"id": 2, "account_name": "AuroGov", "account_id": "123", "provider": "aws", "default_env": "prod"},
            "placement": [] if empty else [{"env": "prod", "source": "tag", "n": 50}, {"env": "nonprod", "source": "name", "n": 30}, {"env": "unclassified", "source": "none", "n": 20}],
            "compliance": {"totals": blk, "accounts": [acct] if acct else []}, "trend": trend, "trend_available": True,
            "gaps": [] if empty else [["AuroGov", "aws", "ec2", "i-1", "web", "ap-south-1", "prod", "tag", "missing", "Owner; Application"]] * 3,
            "gaps_truncated": False, "classification_on": True, "period_start": START, "period_end": END}


def test_environment_summarize():
    s = M["environment_report"].summarize(_env_data())
    assert s["total"] == 100 and s["prod"] == 50 and s["nonprod"] == 30 and s["unplaced"] == 20 and s["needs_placement"] == 20
    assert s["ok_pct"] == 75.0 and s["needs_tagging"] == 20 and s["missing_tag"][0] == ("Owner", 3)
    assert s["types"][0]["type"] == "ec2" and s["trend_change"] == 4.5


def test_environment_scope_is_account_only_and_pdf_renders():
    er = M["environment_report"]
    assert er.parse_scope("5") == 5
    with pytest.raises(ValueError):
        er.parse_scope("5:1")
    for d in (_env_data(), _env_data(True)):
        assert _is_pdf(er.render_environment_pdf(report_type="monthly", scope_id="2", scope_label="AuroGov", period_start=START,
                                                 period_end=END, data=d, generated_by="tester"))


def test_environment_trend_goes_weekly_when_long():
    er = M["environment_report"]
    trend = [{"date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(), "checkable": 5, "ok_of_checkable_pct": 50.0} for i in range(120)]
    labels, values = er.trend_points(trend)
    assert len(values) < 25


# ── security ─────────────────────────────────────────────────────────

def _sec_data():
    mk = lambda i, sev, status, first, res=None, chk="s3_bucket_public": {"id": i, "check_id": chk, "resource_id": f"bucket-{i}", "severity": sev,
                                                                       "title": "t", "status": status, "first_seen_at": first,
                                                                       "last_seen_at": END - timedelta(minutes=20), "resolved_at": res}
    f = [mk(1, "HIGH", "open", END - timedelta(days=45)), mk(2, "MEDIUM", "open", END - timedelta(days=3), chk="ebs_unencrypted"),
         mk(3, "LOW", "resolved", END - timedelta(days=10), END - timedelta(days=5)), mk(4, "HIGH", "resolved", END - timedelta(days=100), END - timedelta(days=2))]
    return {"account": {"id": 2, "account_name": "AuroGov", "account_id": "123", "provider": "aws", "status": "active"}, "findings": f,
            "now": END, "period_start": START, "period_end": END}


def test_security_summarize():
    s = M["security_report"].summarize(_sec_data())
    assert s["open_total"] == 2 and s["open_by_sev"] == {"HIGH": 1, "MEDIUM": 1, "LOW": 0}
    assert s["new_total"] == 2 and s["fixed_total"] == 2          # ids 2 and 3 first seen in period; 3 and 4 fixed in period
    assert s["avg_fix_days"] == round((5 + 98) / 2, 1)
    assert len(s["old_high"]) == 1 and s["stale_scan"] is False
    assert [r["label"] for r in s["by_check"]][0] == "Public S3 bucket"
    assert sum(a["n"] for a in s["ages"]) == 2


def test_security_flags_a_stopped_scan():
    d = _sec_data()
    for f in d["findings"]:
        f["last_seen_at"] = END - timedelta(hours=9)
    assert M["security_report"].summarize(d)["stale_scan"] is True


def test_security_pdf_renders_with_and_without_findings():
    sr = M["security_report"]
    for d in (_sec_data(), dict(_sec_data(), findings=[])):
        assert _is_pdf(sr.render_security_pdf(report_type="monthly", scope_id="2", scope_label="AuroGov", period_start=START, period_end=END,
                                              data=d, generated_by="tester"))


# ── SLO ──────────────────────────────────────────────────────────────

def _slo_data():
    base = {"aws_account_id": 2, "window_days": 30, "enabled": 1, "synthetic_check_id": None, "resource_id": "i-1", "metric_name": None, "resource_name": "web"}
    slos = [dict(base, id=1, name="Web", target_pct=99.9, status="breached", uptime_pct=99.5, budget_consumed_pct=100.0, budget_remaining_pct=0.0),
            dict(base, id=2, name="API", target_pct=99.0, status="ok", uptime_pct=99.9, budget_consumed_pct=10.0, budget_remaining_pct=90.0, synthetic_check_id=4, synthetic_check_name="api-check"),
            dict(base, id=3, name="New", target_pct=99.0, status="no_data", uptime_pct=None, budget_consumed_pct=None, budget_remaining_pct=None, enabled=0),
            dict(base, id=4, name="Risky", target_pct=99.0, status="warning", uptime_pct=99.2, budget_consumed_pct=80.0, budget_remaining_pct=20.0)]
    return {"account": {"id": 2, "account_name": "AuroGov", "account_id": "123", "provider": "aws"}, "slos": slos, "now": END, "period_start": START, "period_end": END}


def test_slo_summarize_and_order():
    sr = M["slo_report"]
    s = sr.summarize(_slo_data())
    assert [x["name"] for x in s["slos"]] == ["Web", "Risky", "API", "New"]       # worst first
    assert {k: len(v) for k, v in s["by"].items()} == {"breached": 1, "warning": 1, "ok": 1, "no_data": 1}
    assert s["avg_uptime"] == round((99.5 + 99.9 + 99.2) / 3, 3) and len(s["paused"]) == 1
    assert sr.budget_minutes({"window_days": 30, "target_pct": 99.9}) == 43.2
    assert sr.measures(s["slos"][2]) == "Synthetic check: api-check"


def test_slo_pdf_renders_and_empty():
    sr = M["slo_report"]
    for d in (_slo_data(), dict(_slo_data(), slos=[])):
        assert _is_pdf(sr.render_slo_pdf(report_type="monthly", scope_id="2", scope_label="AuroGov", period_start=START, period_end=END,
                                         data=d, generated_by="tester"))


def test_slo_gather_uses_the_slo_pages_compute_function():
    sr = M["slo_report"]
    seen = []

    class Cur:
        def execute(self, sql, params=None): pass
        def fetchone(self): return {"id": 2, "account_name": "A", "account_id": "1", "provider": "aws"}
        def fetchall(self): return [{"id": 9, "name": "S", "window_days": 30, "target_pct": 99.9}]

    class Ctx:
        def __enter__(self): return (None, Cur())
        def __exit__(self, *a): return False

    sr.get_db_cursor = lambda **k: Ctx()
    d = sr.gather_slo_data(2, None, START, END, _compute=lambda cur, s: (seen.append(s["id"]) or {"status": "ok", "uptime_pct": 100.0}))
    assert seen == [9] and d["slos"][0]["status"] == "ok"


# ── permissions (registry.validate) ──────────────────────────────────

def _registry(perms, enabled=True):
    install_stub("app.auth.permissions", has_permission=lambda user, code: code in perms, require_permission=lambda c: (lambda: None))
    install_stub("app.hostagents.settings", enabled=lambda: enabled, stale_seconds=lambda: 180)
    reg = load_module("app/reports/registry.py")
    reg._exists = lambda *a, **k: None
    return reg


def test_cost_report_is_admin_only_and_never_has_an_account():
    reg = _registry(set())
    with pytest.raises(Exception) as e:
        reg.validate("COST", "all", None, {"role": "editor"})
    assert e.value.status_code == 403
    assert reg.validate("COST", "all", 5, {"role": "admin"}) is None
    with pytest.raises(Exception) as e:
        reg.validate("COST", "weird", None, {"role": "admin"})
    assert e.value.status_code == 400


@pytest.mark.parametrize("scope,perm,sid", [("HOST", "hostagents.view", "3"), ("ENVIRONMENT", "environments.view", "3"),
                                            ("SECURITY", "security.view", "3"), ("SLO", "slo.view", "3")])
def test_section_reports_need_the_sections_own_permission(scope, perm, sid):
    with pytest.raises(Exception) as e:
        _registry({"reports.generate"}).validate(scope, sid, 3, {"role": "editor"})
    assert e.value.status_code == 403 and perm in e.value.detail
    assert _registry({perm}).validate(scope, sid, 3, {"role": "editor"}) == 3


def test_scope_must_name_the_selected_account_and_host_feature_must_be_on():
    reg = _registry({"hostagents.view", "environments.view"})
    with pytest.raises(Exception) as e:
        reg.validate("ENVIRONMENT", "4", 3, {"role": "editor"})
    assert e.value.status_code == 400
    with pytest.raises(Exception) as e:
        reg.validate("ENVIRONMENT", "3:1", 3, {"role": "editor"})          # an environment report is whole-account
    assert e.value.status_code == 400
    with pytest.raises(Exception) as e:
        _registry({"hostagents.view"}, enabled=False).validate("HOST", "3", 3, {"role": "editor"})
    assert e.value.status_code == 409


def test_filenames_name_the_section():
    import app.report_names as rn
    n = rn.periodic_report_filename({"report_type": "MONTHLY", "scope_type": "COST", "scope_label": "Cloud cost: all clouds",
                                     "period_start": START, "period_end": END})
    assert n.startswith("CloudOps-Cost-Report-Cloud-cost-all-clouds-2026-09-27_to_2026-10-27") and n.endswith(".pdf")


def test_api_and_worker_constants_match_the_registry():
    import re
    reg = M["registry"]
    api = open("app/api/reports.py").read()
    worker = open("app/reports/worker.py").read()
    sections = set(reg.SECTION_REPORTS)
    assert set(re.search(r"_SECTION_SCOPES = \{([^}]*)\}", api).group(1).replace('"', "").replace(" ", "").split(",")) == sections
    needs = {k for k in sections if reg.needs_account(k)}
    assert set(re.search(r"_SECTION_NEEDS_ACCOUNT = \{([^}]*)\}", api).group(1).replace('"', "").replace(" ", "").split(",")) == needs
    assert set(re.search(r'job\["scope_type"\] in \(([^)]*)\)', worker).group(1).replace('"', "").replace(" ", "").split(",")) == sections
    assert sections <= set(re.search(r"_VALID_SCOPE_TYPES = \{([^}]*)\}", api).group(1).replace('"', "").replace(" ", "").split(","))
