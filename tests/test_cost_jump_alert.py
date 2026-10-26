"""Cost-jump alert: the rule (pure) and the alert lifecycle against a fake database. No cloud, no network."""
from datetime import date, timedelta

import pytest

import app  # noqa: F401
from tests.conftest import install_stub, load_module

TODAY = date(2026, 10, 25)
SETTLED = TODAY - timedelta(days=2)                      # Oct 23: the day that is judged


@pytest.fixture
def cj(monkeypatch):
    install_stub("app.db", get_connection=lambda: None)
    mod = load_module("app/collector/cost_jump_alert.py")
    for v in ("COST_JUMP_ALERT_ENABLED", "COST_JUMP_THRESHOLD_PCT", "COST_JUMP_MIN_INCREASE_USD"):
        monkeypatch.delenv(v, raising=False)
    return mod


def series(day_usd, base_usd=10.0, base_days=7):
    d = {SETTLED - timedelta(days=k): base_usd for k in range(1, base_days + 1)}
    d[SETTLED] = day_usd
    return d


# ── the rule ────────────────────────────────────────────────────────────────────
def test_exactly_30_percent_is_not_a_jump_but_just_over_is(cj):
    assert cj.evaluate(series(13.0), TODAY)["state"] == "ok"             # +30% exactly: "more than 30%" is not met
    r = cj.evaluate(series(13.01), TODAY)
    assert r["state"] == "jump" and r["baseline_avg"] == pytest.approx(10.0) and r["limit"] == pytest.approx(13.0)
    assert r["increase_pct"] == pytest.approx(30.1)


def test_today_and_yesterday_are_never_judged(cj):
    d = series(10.0)
    d[TODAY] = 99.0
    d[TODAY - timedelta(days=1)] = 99.0                                   # provisional days filling in must be ignored
    assert cj.evaluate(d, TODAY)["state"] == "ok"
    assert cj.evaluate(d, TODAY)["day"] == SETTLED


def test_missing_settled_day_or_thin_history_is_no_data_not_ok(cj):
    d = series(50.0)
    del d[SETTLED]
    assert cj.evaluate(d, TODAY)["state"] == "no_data"
    assert cj.evaluate(series(50.0, base_days=4), TODAY)["state"] == "no_data"      # 4 of 7 days is too few
    assert cj.evaluate(series(50.0, base_days=5), TODAY)["state"] == "jump"          # 5 of 7 is enough, averaged over those 5


def test_noise_floor_stops_tiny_absolute_rises(cj):
    assert cj.evaluate(series(0.15, base_usd=0.10), TODAY)["state"] == "ok"          # +50% but only 5 cents
    assert cj.evaluate(series(0.20, base_usd=0.10), TODAY)["state"] == "jump"        # a rise of exactly the floor counts
    assert cj.evaluate(series(0.30, base_usd=0.10), TODAY, min_increase=0.10)["state"] == "jump"


def test_no_prior_cost_then_cost_is_a_jump_with_no_percentage(cj):
    r = cj.evaluate(series(5.0, base_usd=0.0), TODAY)
    assert r["state"] == "jump" and r["increase_pct"] is None
    assert "no cost in the days before" in cj.build_detail("Acme", "aws", r, 30)


def test_detail_is_plain_and_complete(cj):
    r = cj.evaluate(series(20.0), TODAY)
    t = cj.build_detail("Acme Prod", "aws", r, 30)
    assert "Acme Prod" in t and "$20.00" in t and "100% above" in t and "$10.00" in t and "$13.00" in t and "2026-10-23" in t


# ── lifecycle ───────────────────────────────────────────────────────────────────
GLOBAL_ON = [{"scope_key": 0, "enabled": 1, "threshold_pct": None, "min_increase_usd": None, "updated_by": "t", "updated_at": None}]


def srow(key, enabled=None, pct=None, floor=None):
    return {"scope_key": key, "enabled": enabled, "threshold_pct": pct, "min_increase_usd": floor, "updated_by": "t", "updated_at": None}


class FakeCur:
    def __init__(self, accounts, open_alert, settings, open_alerts=()):
        self.accounts, self.open_alert, self.settings, self.open_alerts, self.log = accounts, open_alert, settings, list(open_alerts), []
        self._rows, self.lastrowid, self.rowcount = [], 77, 1

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), params))
        self._rows = []
        if "FROM cost_jump_settings" in sql:
            if self.settings is None:
                raise Exception("1146 (42S02): Table 'x.cost_jump_settings' doesn't exist")
            self._rows = self.settings
        elif "FROM aws_cost_actuals" in sql:
            self._rows = self.accounts
        elif "SELECT id, aws_account_id FROM alerts" in sql:
            self._rows = self.open_alerts
        elif "FROM alerts" in sql and "SELECT id, status" in sql:
            self._rows = [self.open_alert] if self.open_alert else []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass

    def sqls(self, needle):
        return [p for s, p in self.log if needle in s]


class FakeConn:
    def __init__(self, cur):
        self.cur, self.committed, self.rolled_back = cur, False, False

    def cursor(self, *a, **k):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def cost_rows(daily, pk=5, name="Acme"):
    return [{"pk": pk, "account_name": name, "default_region": "ap-south-1", "usage_type": "APS3-CW:GMD-Metrics",
             "usage_date": d, "amount_usd": v} for d, v in daily.items()]


def run(cj, monkeypatch, daily, open_alert=None, region_rows=(), settings=GLOBAL_ON, open_alerts=()):
    cur = FakeCur(cost_rows(daily) + list(region_rows), open_alert, settings, open_alerts)
    conn = FakeConn(cur)
    monkeypatch.setattr(cj, "get_connection", lambda: conn)
    sent = []
    monkeypatch.setattr(cj, "_notify", lambda *a: sent.append(a))
    stats = cj.run_cost_jump_check(force=True, today=TODAY)
    return stats, cur, conn, sent


def test_nothing_on_anywhere_reads_no_cost_data_and_raises_nothing(cj, monkeypatch):
    stats, cur, conn, sent = run(cj, monkeypatch, series(50.0), settings=[])
    assert stats["skipped"] == "disabled" and cur.sqls("FROM aws_cost_actuals") == [] and cur.sqls("INSERT INTO alerts") == []


def test_a_jump_opens_one_warning_and_notifies_once(cj, monkeypatch):
    stats, cur, conn, sent = run(cj, monkeypatch, series(20.0))
    assert stats["opened"] == 1 and conn.committed
    ins = cur.sqls("INSERT INTO alerts")[0]
    assert ins[2] == "cloud_cost_jump" and ins[3] == "5:cloud_cost:cloud_cost_jump" and ins[1] == "cloud-cost-5"
    assert cur.sqls("INSERT INTO resources")[0][1] == "cloud_cost"
    assert len(sent) == 1 and sent[0][0] == 77 and sent[0][4] == 20.0


def test_an_open_alert_is_refreshed_not_duplicated_or_renotified(cj, monkeypatch):
    stats, cur, conn, sent = run(cj, monkeypatch, series(20.0), open_alert={"id": 9, "status": "active", "silenced": 0})
    assert stats["updated"] == 1 and stats["opened"] == 0 and sent == []
    assert cur.sqls("INSERT INTO alerts") == [] and cur.sqls("UPDATE alerts SET current_value")[0][2] == 9


def test_back_inside_the_limit_resolves_as_recovered(cj, monkeypatch):
    stats, cur, conn, sent = run(cj, monkeypatch, series(10.5), open_alert={"id": 9, "status": "active", "silenced": 0})
    assert stats["resolved"] == 1 and cur.sqls("resolution_reason")[0][0] == "recovered" and sent == []


def test_no_data_leaves_an_open_alert_alone_and_creates_nothing(cj, monkeypatch):
    d = series(20.0)
    del d[SETTLED]
    stats, cur, conn, sent = run(cj, monkeypatch, d, open_alert={"id": 9, "status": "active", "silenced": 0})
    assert stats["no_data"] == 1 and stats["resolved"] == 0 and stats["updated"] == 0
    assert cur.sqls("INSERT INTO alerts") == [] and cur.sqls("INSERT INTO resources") == [] and cur.sqls("UPDATE alerts") == []


def test_cost_outside_the_onboarded_region_is_ignored(cj, monkeypatch):
    other_region = [{"pk": 5, "account_name": "Acme", "default_region": "ap-south-1", "usage_type": "USE1-CW:GMD-Metrics",
                     "usage_date": SETTLED, "amount_usd": 500.0}]
    stats, *_ = run(cj, monkeypatch, series(10.0), region_rows=other_region)
    assert stats["opened"] == 0                                           # a 500 USD charge in us-east-1 is not CloudOps polling here


def test_non_cloudops_charges_are_ignored(cj, monkeypatch):
    other = [{"pk": 5, "account_name": "Acme", "default_region": "ap-south-1", "usage_type": "APS3-CW:MetricMonitorUsage",
              "usage_date": SETTLED, "amount_usd": 500.0}]
    stats, *_ = run(cj, monkeypatch, series(10.0), region_rows=other)
    assert stats["opened"] == 0


def test_the_check_is_throttled_between_runs(cj, monkeypatch):
    run(cj, monkeypatch, series(10.0))
    assert cj.run_cost_jump_check() == {"skipped": "throttled"}


def test_cloud_cost_jump_is_a_system_metric_so_it_is_never_auto_resolved_for_a_missing_threshold():
    from app.alert_rules import SYSTEM_METRICS
    assert "cloud_cost_jump" in SYSTEM_METRICS


# ── settings: all accounts and per account ─────────────────────────────────────
def test_an_account_switched_off_is_skipped_even_when_all_accounts_are_on(cj, monkeypatch):
    stats, cur, *_ = run(cj, monkeypatch, series(50.0), settings=GLOBAL_ON + [srow(5, enabled=0)])
    assert stats["opened"] == 0 and stats["accounts"] == 0


def test_one_account_can_be_on_while_all_accounts_are_off(cj, monkeypatch):
    stats, *_ = run(cj, monkeypatch, series(50.0), settings=[srow(0, enabled=0), srow(5, enabled=1)])
    assert stats["opened"] == 1


def test_switching_an_account_off_resolves_its_open_alert(cj, monkeypatch):
    stats, cur, conn, sent = run(cj, monkeypatch, series(50.0), settings=GLOBAL_ON + [srow(5, enabled=0)],
                                 open_alerts=[{"id": 9, "aws_account_id": 5}])
    assert stats["switched_off"] == 1 and cur.sqls("resolution_reason")[0][0] == "alert_disabled"


def test_switching_everything_off_still_clears_open_alerts(cj, monkeypatch):
    stats, cur, conn, sent = run(cj, monkeypatch, series(50.0), settings=[srow(0, enabled=0)],
                                 open_alerts=[{"id": 9, "aws_account_id": 5}])
    assert stats["switched_off"] == 1 and stats["skipped"] == "disabled" and conn.committed


def test_an_accounts_own_percentage_beats_the_all_accounts_one(cj, monkeypatch):
    on = [srow(0, enabled=1, pct=30.0)]
    assert run(cj, monkeypatch, series(14.0), settings=on)[0]["opened"] == 1                 # +40% > 30
    assert run(cj, monkeypatch, series(14.0), settings=on + [srow(5, pct=50.0)])[0]["opened"] == 0   # +40% < 50 for this account
    assert run(cj, monkeypatch, series(14.0), settings=[srow(0, enabled=1, pct=50.0), srow(5, pct=30.0)])[0]["opened"] == 1


def test_an_accounts_own_minimum_rise_beats_the_all_accounts_one(cj, monkeypatch):
    on = [srow(0, enabled=1, floor=0.10)]
    assert run(cj, monkeypatch, series(14.0), settings=on + [srow(5, floor=5.0)])[0]["opened"] == 0       # rise of 4 is under 5


def test_before_migration_132_the_environment_still_decides(cj, monkeypatch):
    assert run(cj, monkeypatch, series(50.0), settings=None)[0]["skipped"] == "disabled"
    monkeypatch.setenv("COST_JUMP_ALERT_ENABLED", "true")
    assert run(cj, monkeypatch, series(50.0), settings=None)[0]["opened"] == 1


# ── who is emailed ──────────────────────────────────────────────────────────────
class MailCur(FakeCur):
    def __init__(self, users):
        super().__init__([], None, [])
        self.users = users

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), params))
        self._rows = self.users if "FROM users" in sql else []


@pytest.fixture
def mail(cj, monkeypatch):
    import sys
    import types
    sent = []
    cfg = {"on": True, "fail": set()}
    m = types.ModuleType("app.email.mailer")
    m.is_configured = lambda: cfg["on"]
    m.send_email = lambda to, subject, body: (sent.append((to, subject)) or to not in cfg["fail"])
    pkg = types.ModuleType("app.email")
    pkg.mailer = m
    monkeypatch.setitem(sys.modules, "app.email", pkg)
    monkeypatch.setitem(sys.modules, "app.email.mailer", m)
    return sent, cfg


def with_users(cj, monkeypatch, users):
    cur = MailCur(users)
    conn = FakeConn(cur)
    monkeypatch.setattr(cj, "get_connection", lambda: conn)
    return cur


MSG = {"headline": "H", "text": "T"}
U = lambda i, name, email, active=1: {"id": i, "username": name, "email": email, "active": active}


def test_users_are_emailed_at_their_current_address_and_typed_addresses_too(cj, monkeypatch, mail):
    sent, cfg = mail
    with_users(cj, monkeypatch, [U(3, "asha", "asha@corp.com"), U(5, "ravi", "ravi@corp.com")])
    r = cj.send_to_recipients(MSG, [3, 5], ["ops@corp.com"], alert_id=9)
    assert [s[0] for s in sent] == ["asha@corp.com", "ravi@corp.com", "ops@corp.com"]
    assert r["sent"] == ["asha@corp.com", "ravi@corp.com", "ops@corp.com"] and r["failed"] == [] and r["skipped"] == []


def test_a_user_with_no_email_or_inactive_or_gone_is_skipped_with_a_reason(cj, monkeypatch, mail):
    sent, cfg = mail
    with_users(cj, monkeypatch, [U(3, "asha", ""), U(5, "ravi", "ravi@corp.com", active=0)])
    r = cj.send_to_recipients(MSG, [3, 5, 8], ["ops@corp.com"])
    assert sent == [("ops@corp.com", "H")]
    assert {tuple(x) for x in r["skipped"]} == {("asha", "no email on file"), ("ravi", "user is inactive"), ("user 8", "no longer exists")}


def test_the_same_address_is_never_emailed_twice(cj, monkeypatch, mail):
    sent, cfg = mail
    with_users(cj, monkeypatch, [U(3, "asha", "Asha@Corp.com")])
    cj.send_to_recipients(MSG, [3], ["asha@corp.com", "ASHA@corp.com"])
    assert len(sent) == 1


def test_unconfigured_smtp_fails_every_address_with_a_clear_reason_and_sends_nothing(cj, monkeypatch, mail):
    sent, cfg = mail
    cfg["on"] = False
    with_users(cj, monkeypatch, [])
    r = cj.send_to_recipients(MSG, [], ["ops@corp.com"])
    assert sent == [] and r["sent"] == [] and "SMTP is not configured" in r["failed"][0][1]


def test_one_bad_address_does_not_stop_the_others_and_every_try_is_logged(cj, monkeypatch, mail):
    sent, cfg = mail
    cfg["fail"] = {"bad@corp.com"}
    cur = with_users(cj, monkeypatch, [])
    r = cj.send_to_recipients(MSG, [], ["bad@corp.com", "ok@corp.com"], alert_id=9)
    assert r["sent"] == ["ok@corp.com"] and r["failed"][0][0] == "bad@corp.com"
    logged = cur.sqls("INSERT INTO notification_log")
    assert sorted(p[3] for p in logged) == ["failed", "sent"] and all(p[0] == "Cost alert recipients" for p in logged)


def test_nothing_chosen_sends_nothing(cj, monkeypatch, mail):
    sent, cfg = mail
    with_users(cj, monkeypatch, [])
    assert cj.send_to_recipients(MSG, [], []) == {"sent": [], "failed": [], "skipped": []} and sent == []


def test_an_opened_alert_carries_its_all_accounts_plus_own_recipients(cj, monkeypatch):
    st = [srow(0, enabled=1), srow(5)]
    st[0].update(notify_user_ids="[1]", notify_emails="all@corp.com")
    st[1].update(notify_user_ids="[2]", notify_emails="acct@corp.com")
    cur = FakeCur(cost_rows(series(50.0)), None, st)
    conn = FakeConn(cur)
    monkeypatch.setattr(cj, "get_connection", lambda: conn)
    sent = []
    monkeypatch.setattr(cj, "_notify", lambda *a: sent.append(a))
    cj.run_cost_jump_check(force=True, today=TODAY)
    assert sent[0][-1] == ([1, 2], ["all@corp.com", "acct@corp.com"])
