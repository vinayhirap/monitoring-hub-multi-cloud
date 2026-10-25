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
    monkeypatch.setenv("COST_JUMP_ALERT_ENABLED", "true")
    monkeypatch.delenv("COST_JUMP_THRESHOLD_PCT", raising=False)
    monkeypatch.delenv("COST_JUMP_MIN_INCREASE_USD", raising=False)
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


def test_env_overrides_and_bad_values_fall_back(cj, monkeypatch):
    monkeypatch.setenv("COST_JUMP_THRESHOLD_PCT", "50")
    assert cj.threshold_pct() == 50.0
    monkeypatch.setenv("COST_JUMP_THRESHOLD_PCT", "banana")
    assert cj.threshold_pct() == 30.0
    monkeypatch.setenv("COST_JUMP_THRESHOLD_PCT", "-5")
    assert cj.threshold_pct() == 30.0


# ── lifecycle ───────────────────────────────────────────────────────────────────
class FakeCur:
    def __init__(self, accounts, open_alert):
        self.accounts, self.open_alert, self.log = accounts, open_alert, []
        self._rows, self.lastrowid, self.rowcount = [], 77, 1

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), params))
        self._rows = []
        if "FROM aws_cost_actuals" in sql:
            self._rows = self.accounts
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


def run(cj, monkeypatch, daily, open_alert=None, region_rows=()):
    cur = FakeCur(cost_rows(daily) + list(region_rows), open_alert)
    conn = FakeConn(cur)
    monkeypatch.setattr(cj, "get_connection", lambda: conn)
    sent = []
    monkeypatch.setattr(cj, "_notify", lambda *a: sent.append(a))
    stats = cj.run_cost_jump_check(force=True, today=TODAY)
    return stats, cur, conn, sent


def test_disabled_never_touches_the_database(cj, monkeypatch):
    monkeypatch.setenv("COST_JUMP_ALERT_ENABLED", "false")
    monkeypatch.setattr(cj, "get_connection", lambda: pytest.fail("database touched while disabled"))
    assert cj.run_cost_jump_check(force=True) == {"skipped": "disabled"}


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
    assert stats["opened"] == 0                                           # a $500 charge in us-east-1 is not CloudOps polling here


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
