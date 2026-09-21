# tests/test_alert_lifecycle_integration.py
"""
REAL-DATABASE tests for the 2026-09-20 alerts audit fixes.

Every other test in this suite mocks the cursor, which is exactly why SQL
regressions (an alert INSERT that silently omitted aws_account_id for weeks, a
JOIN that mixed two accounts) shipped unnoticed. These run the real evaluator
and the real API query functions against a real MySQL 8.

    mysql -uroot -e "create database mh_test; create user 'mh'@'%' identified by 'mh';
                      grant all on mh_test.* to 'mh'@'%'"
    mysql -uroot mh_test < tests/fixtures/alerts_test_schema.sql
    MH_TEST_DB=1 pytest tests/test_alert_lifecycle_integration.py

Skipped automatically unless MH_TEST_DB is set. Env overrides:
MH_TEST_DB_HOST (127.0.0.1) / _PORT (3306) / _USER (mh) / _PASSWORD (mh) / _NAME (mh_test).
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

# real package first, so install_stub("app.db") does not replace `app` itself
import app.alert_rules as alert_rules  # noqa: E402
import app.threshold_defaults  # noqa: E402,F401
from tests.conftest import load_module, install_stub  # noqa: E402

pytestmark = pytest.mark.skipif(not os.getenv("MH_TEST_DB"), reason="set MH_TEST_DB=1 to run real-DB tests")

if os.getenv("MH_TEST_DB"):
    import mysql.connector


def _connect():
    return mysql.connector.connect(
        host=os.getenv("MH_TEST_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("MH_TEST_DB_PORT", 3306)),
        user=os.getenv("MH_TEST_DB_USER", "mh"),
        password=os.getenv("MH_TEST_DB_PASSWORD", "mh"),
        database=os.getenv("MH_TEST_DB_NAME", "mh_test"),
        use_pure=True,
        autocommit=False,
    )


TABLES = ["alerts", "alert_pending", "metrics", "thresholds", "metric_baseline",
          "maintenance_windows", "resource_health", "resources", "metric_catalog",
          "aws_accounts", "resource_relationships", "account_metric_selections"]


def utcnow():
    return datetime.utcnow().replace(microsecond=0)


class DB:
    def __init__(self):
        self.conn = _connect()
        self.cur = self.conn.cursor(dictionary=True)
        self._cat = {}

    def reset(self):
        self.cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in TABLES:
            self.cur.execute(f"TRUNCATE TABLE {t}")
        self.cur.execute("SET FOREIGN_KEY_CHECKS=1")
        self.conn.commit()
        self._cat = {}

    def q(self, sql, params=()):
        self.cur.execute(sql, params)
        rows = self.cur.fetchall()
        self.conn.commit()          # end the read snapshot so later reads see other connections' commits
        return rows

    def one(self, sql, params=()):
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def x(self, sql, params=()):
        self.cur.execute(sql, params)
        self.conn.commit()
        return self.cur.lastrowid

    # -- builders --------------------------------------------------------
    def account(self, aid, name, provider="aws"):
        self.x("INSERT INTO aws_accounts (id, provider, account_name, account_id, role_arn, status) "
               "VALUES (%s,%s,%s,%s,'arn:x','active')", (aid, provider, name, str(1000 + aid)))

    def resource(self, acct, rtype, rid, name=None, tags=None, instance_state="running", seen=None):
        return self.x(
            "INSERT INTO resources (aws_account_id, resource_type, resource_id, name, tags, instance_state, last_seen_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (acct, rtype, rid, name or rid, json.dumps(tags or {}), instance_state, seen or utcnow()))

    def catalog(self, metric, service, unit="Count", category="core"):
        key = (metric, service)
        if key not in self._cat:
            self._cat[key] = self.x(
                "INSERT INTO metric_catalog (service, provider, metric_name, unit, category) "
                "VALUES (%s,'aws',%s,%s,%s)", (service, metric, unit, category))
        return self._cat[key]

    def threshold(self, acct, rtype, metric, w, c, cmp=">", period=5, enabled=1, dynamic=0, unit="Count", k=3.0):
        mid = self.catalog(metric, rtype, unit)
        return self.x(
            "INSERT INTO thresholds (aws_account_id, resource_type, metric_id, warning_value, critical_value, "
            "comparison, evaluation_period, enabled, use_dynamic, dynamic_k) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (acct, rtype, mid, w, c, cmp, period, enabled, dynamic, k))

    def metric(self, res_db_id, name, value, age_min=0):
        self.x("INSERT INTO metrics (resource_id, metric_name, metric_value, metric_timestamp) VALUES (%s,%s,%s,%s) "
               "ON DUPLICATE KEY UPDATE metric_value=VALUES(metric_value), metric_timestamp=VALUES(metric_timestamp)",
               (res_db_id, name, value, utcnow() - timedelta(minutes=age_min)))

    def baseline(self, acct, rid, metric, mean, std, n):
        h, wd = self.one("SELECT HOUR(UTC_TIMESTAMP()) h, WEEKDAY(UTC_TIMESTAMP()) w").values()
        self.x("INSERT INTO metric_baseline (aws_account_id, resource_id, metric_name, hour_of_day, day_of_week, "
               "mean_value, stddev_value, sample_count) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
               (acct, rid, metric, h, wd, mean, std, n))

    def alert(self, acct, rid, metric, sev="CRITICAL", status="active", seen_min_ago=1, triggered_min_ago=60,
              silenced=0, muted_min=None):
        muted = utcnow() + timedelta(minutes=muted_min) if muted_min else None
        return self.x(
            "INSERT INTO alerts (aws_account_id, resource_id, metric_name, severity, status, current_value, threshold, "
            "triggered_at, last_seen_at, healthy_streak, environment, silenced, muted_until, acked) "
            "VALUES (%s,%s,%s,%s,%s,1,1,%s,%s,0,'prod',%s,%s,%s)",
            (acct, rid, metric, sev, status, utcnow() - timedelta(minutes=triggered_min_ago),
             utcnow() - timedelta(minutes=seen_min_ago), silenced, muted, 1 if status == "acknowledged" else 0))

    def get(self, alert_id):
        return self.one("SELECT * FROM alerts WHERE id=%s", (alert_id,))


@pytest.fixture()
def db():
    d = DB()
    d.reset()
    d.account(1, "U4RAD")
    d.account(2, "AuroGov")
    yield d
    d.conn.close()


def run_eval(published=None):
    """Load a fresh evaluator wired to the real test DB and run one cycle."""
    published = published if published is not None else []
    install_stub("app.db", get_connection=_connect)
    install_stub("app.ws.publisher",
                 publish_alert=lambda **k: published.append(("new", k)),
                 publish_alert_resolved=lambda **k: published.append(("resolved", k)))
    install_stub("app.api.live_data", invalidate_accounts_cache=lambda: None)
    install_stub("app.api.alerts", _invalidate_cache=lambda: None)
    mod = load_module("app/collector/alert_evaluator.py")
    mod.evaluate_alerts()
    return mod


# ── evaluator: creation, placeholders, severity ─────────────────────────

def test_real_static_threshold_fires_critical(db):
    r = db.resource(1, "ec2", "i-aaa")
    db.threshold(1, "ec2", "cpuutilization", 70, 90, unit="Percent")
    db.metric(r, "cpuutilization", 95)
    run_eval()
    a = db.one("SELECT * FROM alerts WHERE resource_id='i-aaa'")
    assert a and a["severity"] == "CRITICAL" and a["status"] == "active"
    assert a["aws_account_id"] == 1 and a["group_key"] == "1:ec2:cpuutilization"


def test_placeholder_volume_threshold_never_alerts_on_cold_start(db):
    """712 GB bucket vs the 1M/5M placeholder used to be CRITICAL."""
    r = db.resource(1, "s3", "u4rad-s3-reporting-bot")
    db.threshold(1, "s3", "bucketsizebytes", 1000000, 5000000)
    db.metric(r, "bucketsizebytes", 712199220386, age_min=300)   # daily metric, 5h old
    run_eval()
    assert db.one("SELECT COUNT(*) n FROM alerts")["n"] == 0


def test_edited_volume_threshold_is_a_real_static_threshold(db):
    r = db.resource(1, "s3", "big-bucket")
    db.threshold(1, "s3", "bucketsizebytes", 1000000000, 2000000000)   # human set 1GB / 2GB
    db.metric(r, "bucketsizebytes", 3000000000, age_min=300)
    run_eval()
    a = db.one("SELECT * FROM alerts WHERE resource_id='big-bucket'")
    assert a and a["severity"] == "CRITICAL"          # slow-tier window (26h) saw the 5h-old reading


def test_placeholder_anomaly_needs_confident_baseline_and_is_capped_at_warning(db):
    r = db.resource(1, "ec2", "i-net")
    db.threshold(1, "ec2", "networkin", 1000000, 5000000)
    db.baseline(1, "i-net", "networkin", mean=2_000_000, std=500_000, n=40)
    db.metric(r, "networkin", 30_000_000)                  # far outside normal
    for _ in range(3):                                      # ANOMALY_MIN_CYCLES sustained cycles
        run_eval()
    a = db.one("SELECT * FROM alerts WHERE resource_id='i-net'")
    assert a and a["severity"] == "WARNING"                # never CRITICAL for volume


def test_placeholder_anomaly_not_sustained_does_not_fire(db):
    r = db.resource(1, "ec2", "i-net")
    db.threshold(1, "ec2", "networkin", 1000000, 5000000)
    db.baseline(1, "i-net", "networkin", mean=2_000_000, std=500_000, n=40)
    db.metric(r, "networkin", 30_000_000)
    run_eval()                                              # a single spike
    assert db.one("SELECT COUNT(*) n FROM alerts")["n"] == 0


def test_pre_existing_placeholder_critical_is_retired_with_reason(db):
    """The ~thousands of fake CRITICAL bucket alerts already in prod."""
    r = db.resource(1, "ec2", "i-old")
    db.threshold(1, "ec2", "networkin", 1000000, 5000000)
    db.metric(r, "networkin", 900_000)
    aid = db.alert(1, "i-old", "networkin", "CRITICAL")
    for _ in range(3):
        run_eval()
    a = db.get(aid)
    assert a["status"] == "resolved" and a["resolution_reason"] == "placeholder_threshold"


# ── dynamic thresholds: guard rails ─────────────────────────────────────

def test_dynamic_band_cannot_tighten_below_half_of_static(db):
    """quiet CPU 2% +/- 0.5 -> raw critical 3.5%; guard rail floors at 45%."""
    r = db.resource(1, "ec2", "i-quiet")
    db.threshold(1, "ec2", "cpuutilization", 70, 90, dynamic=1, unit="Percent")
    db.baseline(1, "i-quiet", "cpuutilization", mean=2.0, std=0.5, n=50)
    db.metric(r, "cpuutilization", 10.0)
    run_eval()
    assert db.one("SELECT COUNT(*) n FROM alerts")["n"] == 0


def test_dynamic_band_still_relaxes_static_threshold(db):
    r = db.resource(1, "ec2", "i-busy")
    db.threshold(1, "ec2", "cpuutilization", 70, 90, dynamic=1, unit="Percent")
    db.baseline(1, "i-busy", "cpuutilization", mean=85.0, std=4.0, n=50)   # normally hot
    db.metric(r, "cpuutilization", 90.0)                                   # static warn is 70, dynamic warn 92.9
    run_eval()
    assert db.one("SELECT COUNT(*) n FROM alerts")["n"] == 0


def test_clamp_percent_cap_and_low_direction():
    m = load_module("app/collector/alert_evaluator.py") if False else None  # noqa: F841
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.ws.publisher", publish_alert=lambda **k: None, publish_alert_resolved=lambda **k: None)
    install_stub("app.api.live_data", invalidate_accounts_cache=lambda: None)
    install_stub("app.api.alerts", _invalidate_cache=lambda: None)
    mod = load_module("app/collector/alert_evaluator.py")
    # > : cap at 99.9 for percent metrics so a noisy band can never become unreachable
    w, c = mod.clamp_dynamic_bounds(101, 130, 70, 90, ">", "Percent")
    assert (w, c) == (99.9, 99.9)
    # > : relaxing is unlimited for non-percent, tightening is limited to 50%
    assert mod.clamp_dynamic_bounds(1, 2, 100, 200, ">", "Count") == (50, 100)
    assert mod.clamp_dynamic_bounds(500, 900, 100, 200, ">", "Count") == (500, 900)
    # < (lower is worse): raw band far above the static line is clamped to 2x
    w, c = mod.clamp_dynamic_bounds(50_000, 40_000, 1000, 500, "<", "Count")
    assert (w, c) == (2000, 1000)


# ── lifecycle: acknowledged, escalation, recovery ───────────────────────

def test_acknowledged_alert_is_not_duplicated_while_still_breaching(db):
    r = db.resource(1, "ec2", "i-aaa")
    db.threshold(1, "ec2", "cpuutilization", 70, 90, unit="Percent")
    db.metric(r, "cpuutilization", 95)
    aid = db.alert(1, "i-aaa", "cpuutilization", "CRITICAL", status="acknowledged")
    run_eval()
    assert db.one("SELECT COUNT(*) n FROM alerts")["n"] == 1
    assert db.get(aid)["status"] == "acknowledged"


def test_acknowledged_alert_resolves_on_recovery(db):
    r = db.resource(1, "ec2", "i-aaa")
    db.threshold(1, "ec2", "cpuutilization", 70, 90, unit="Percent")
    db.metric(r, "cpuutilization", 10)
    aid = db.alert(1, "i-aaa", "cpuutilization", "CRITICAL", status="acknowledged")
    run_eval()
    a = db.get(aid)
    assert a["status"] == "resolved" and a["resolution_reason"] == "recovered"


def test_ack_of_warning_is_reopened_when_it_escalates_to_critical(db):
    r = db.resource(1, "ec2", "i-aaa")
    db.threshold(1, "ec2", "cpuutilization", 70, 90, unit="Percent")
    db.metric(r, "cpuutilization", 97)
    aid = db.alert(1, "i-aaa", "cpuutilization", "WARNING", status="acknowledged")
    run_eval()
    a = db.get(aid)
    assert a["severity"] == "CRITICAL" and a["status"] == "active" and a["acked"] == 0


# ── lifecycle: auto-resolve reasons ─────────────────────────────────────

def test_threshold_disabled_resolves_after_grace(db):
    db.resource(1, "ec2", "i-aaa")
    db.threshold(1, "ec2", "cpuutilization", 70, 90, enabled=0, unit="Percent")
    fresh = db.alert(1, "i-aaa", "cpuutilization", seen_min_ago=5)      # inside grace
    old = db.alert(1, "i-aaa", "diskreadbytes", seen_min_ago=120)       # no threshold at all, past grace
    run_eval()
    assert db.get(fresh)["status"] == "active"
    assert db.get(old)["status"] == "resolved" and db.get(old)["resolution_reason"] == "threshold_disabled"


def test_system_metrics_are_never_resolved_as_threshold_disabled(db):
    db.resource(1, "ec2", "i-aaa")
    aid = db.alert(1, "i-aaa", "multivariate_anomaly", "WARNING", seen_min_ago=120)
    run_eval()
    assert db.get(aid)["status"] == "active"


def test_stale_core_alert_expires_after_72h_but_not_before(db):
    db.resource(1, "ec2", "i-aaa")
    db.threshold(1, "ec2", "cpuutilization", 70, 90, unit="Percent")
    young = db.alert(1, "i-aaa", "cpuutilization", seen_min_ago=60 * 24)
    db.threshold(1, "ec2", "networkout", 70, 90)
    old = db.alert(1, "i-aaa", "networkout", seen_min_ago=60 * 24 * 4)
    run_eval()
    assert db.get(young)["status"] == "active"
    o = db.get(old)
    assert o["status"] == "resolved" and o["resolution_reason"] == "no_data_expired"


def test_aws_resource_not_discovered_for_24h_resolves_alert(db):
    db.resource(1, "ebs", "vol-gone", seen=utcnow() - timedelta(hours=30))
    db.threshold(1, "ebs", "burstbalance", 30, 10, "<")
    aid = db.alert(1, "vol-gone", "burstbalance", seen_min_ago=5)
    run_eval()
    a = db.get(aid)
    assert a["status"] == "resolved" and a["resolution_reason"] == "resource_gone"


def test_stopped_instance_resolution_is_account_scoped(db):
    db.resource(1, "ec2", "i-same", instance_state="stopped")
    db.resource(2, "ec2", "i-same", instance_state="running")
    db.threshold(1, "ec2", "cpuutilization", 70, 90, unit="Percent")
    db.threshold(2, "ec2", "cpuutilization", 70, 90, unit="Percent")
    a1 = db.alert(1, "i-same", "cpuutilization")
    a2 = db.alert(2, "i-same", "cpuutilization")
    run_eval()
    assert db.get(a1)["status"] == "resolved" and db.get(a1)["resolution_reason"] == "instance_stopped"
    assert db.get(a2)["status"] == "active"


def test_same_resource_id_in_two_accounts_never_cross_talks(db):
    """The AuroGov/U4RAD 'System' log-group collision class of bug."""
    r1 = db.resource(1, "logs", "System")
    r2 = db.resource(2, "logs", "System")
    db.threshold(1, "logs", "errors", 1, 5)
    db.threshold(2, "logs", "errors", 1, 5)
    db.metric(r1, "errors", 9)      # breaching in account 1
    db.metric(r2, "errors", 0)      # healthy in account 2
    run_eval()
    rows = db.q("SELECT aws_account_id, status FROM alerts WHERE resource_id='System'")
    assert [(x["aws_account_id"], x["status"]) for x in rows] == [(1, "active")]


def test_stale_pending_candidates_cannot_combine_with_a_later_blip(db):
    db.resource(1, "ec2", "i-aaa")
    db.x("INSERT INTO alert_pending (aws_account_id, resource_id, metric_name, severity, environment, "
         "first_breach_at, last_seen_at, breach_cycles, current_value, threshold_value) "
         "VALUES (1,'i-aaa','cpuutilization','CRITICAL','prod',%s,%s,2,95,90)",
         (utcnow() - timedelta(hours=6), utcnow() - timedelta(hours=6)))
    run_eval()
    assert db.one("SELECT COUNT(*) n FROM alert_pending")["n"] == 0


# ── maintenance windows ─────────────────────────────────────────────────

def test_alert_breaching_during_maintenance_is_born_silenced_and_not_published(db):
    r = db.resource(1, "ec2", "i-maint")
    db.threshold(1, "ec2", "cpuutilization", 70, 90, unit="Percent")
    db.metric(r, "cpuutilization", 99)
    db.x("INSERT INTO maintenance_windows (aws_account_id, resource_id, reason, starts_at, ends_at, silence_downstream) "
         "VALUES (1,'i-maint','patching',%s,%s,0)", (utcnow() - timedelta(minutes=5), utcnow() + timedelta(hours=1)))
    published = []
    run_eval(published)
    a = db.one("SELECT * FROM alerts WHERE resource_id='i-maint'")
    assert a["silenced"] == 1 and "patching" in a["silenced_reason"]
    assert not [p for p in published if p[0] == "new"]


def test_maintenance_window_on_one_account_does_not_silence_another(db):
    db.resource(1, "ec2", "i-same")
    db.resource(2, "ec2", "i-same")
    a1 = db.alert(1, "i-same", "cpuutilization")
    a2 = db.alert(2, "i-same", "cpuutilization")
    db.x("INSERT INTO maintenance_windows (aws_account_id, resource_id, reason, starts_at, ends_at, silence_downstream) "
         "VALUES (1,'i-same','patching',%s,%s,0)", (utcnow() - timedelta(minutes=5), utcnow() + timedelta(hours=1)))
    install_stub("app.db", get_connection=_connect)
    mod = load_module("app/collector/maintenance.py")
    mod.sync_maintenance_silencing()
    assert db.get(a1)["silenced"] == 1 and db.get(a2)["silenced"] == 0


# ── canonical states, counts, and cross-screen agreement ────────────────

def _api():
    install_stub("app.db", get_connection=_connect)
    import importlib
    import app.api.alerts as api
    api = importlib.reload(api)
    api.get_connection = _connect
    api.get_accessible_account_ids = lambda user: None
    api._invalidate_cache()
    return api


USER = {"username": "tester", "role": "admin"}


def _seed_mixed(db):
    for rid in ("i-1", "i-2", "i-3", "i-4", "i-5", "i-6"):
        db.resource(1, "ec2", rid)
    db.resource(1, "s3", "bucket-a")
    db.resource(2, "ec2", "i-x")
    db.alert(1, "i-1", "cpuutilization", "CRITICAL")                          # firing critical
    db.alert(1, "i-1", "networkout", "WARNING")                               # firing warning (same resource)
    db.alert(1, "i-2", "cpuutilization", "WARNING")                           # firing warning
    db.alert(1, "i-3", "cpuutilization", "CRITICAL", seen_min_ago=90)         # STALE (core: 20 min)
    db.alert(1, "i-4", "cpuutilization", "CRITICAL", status="acknowledged")   # acknowledged
    db.alert(1, "i-5", "cpuutilization", "CRITICAL", silenced=1)              # maintenance
    db.alert(1, "i-6", "cpuutilization", "CRITICAL", muted_min=30)            # muted
    db.alert(1, "bucket-a", "bucketsizebytes", "CRITICAL", seen_min_ago=600)  # slow tier: 10h old is NOT stale
    db.alert(2, "i-x", "cpuutilization", "CRITICAL")
    db.alert(1, "i-1", "cpuutilization", "CRITICAL", status="resolved")
    db.alert(1, "i-1", "multivariate_anomaly", "WARNING")                     # hidden internal metric


def test_states_are_derived_correctly(db):
    _seed_mixed(db)
    rows = {(r["resource_id"], r["metric_name"], r["state"]) for r in
            alert_rules_rows(db)}
    assert ("i-3", "cpuutilization", "stale") in rows
    assert ("i-4", "cpuutilization", "acknowledged") in rows
    assert ("i-5", "cpuutilization", "suppressed") in rows
    assert ("i-6", "cpuutilization", "suppressed") in rows
    assert ("bucket-a", "bucketsizebytes", "firing") in rows       # daily metric not falsely stale
    assert ("i-1", "multivariate_anomaly", "firing") not in rows   # hidden metric excluded


def alert_rules_rows(db):
    conn = _connect()
    cur = conn.cursor(dictionary=True)
    try:
        return alert_rules.fetch_open_alert_rows(cur, None)
    finally:
        cur.close()
        conn.close()


def test_every_screen_agrees_on_the_numbers(db):
    """Overview banner == Alerts 'Critical' tab == Services tiles == list rows."""
    _seed_mixed(db)
    api = _api()

    counts = api.alert_counts(account_id=None, current_user=USER)
    summary = api.alert_summary(account_id=None, current_user=USER)
    live_counts = _live_counts(db)

    firing_critical = 3   # i-1 cpu, bucket-a (10h old slow tier), i-x (account 2)
    firing_warning = 2    # i-1 networkout, i-2 cpu
    assert counts["critical"] == summary["totals"]["critical"] == firing_critical
    assert summary["totals"]["warning"] == firing_warning
    assert counts["active"] == firing_critical + firing_warning
    assert counts["stale"] == summary["totals"]["stale"] == 1
    assert counts["acknowledged"] == summary["totals"]["acknowledged"] == 1
    assert counts["suppressed"] == summary["totals"]["suppressed"] == 2
    assert counts["resolved"] == 1
    # Overview account cards are the same rollup
    assert sum(v["critical"] for v in live_counts.values()) == firing_critical
    assert sum(v["warning"] for v in live_counts.values()) == firing_warning

    # each tab's LIST length equals its badge (no more silent 500-row truncation)
    for tab, key in (("active", "active"), ("stale", "stale"), ("critical", "critical"),
                     ("acknowledged", "acknowledged"), ("resolved", "resolved"), ("suppressed", "suppressed")):
        rows, total = api._fetch_alerts_from_db(USER, tab, limit=1000)
        assert total == len(rows) == counts[key], tab


def _live_counts(db):
    install_stub("app.db", get_connection=_connect)
    import importlib
    import app.api.live_data as ld
    ld = importlib.reload(ld) if "app.api.live_data" in sys.modules and hasattr(sys.modules["app.api.live_data"], "router") else ld
    ld.get_connection = _connect
    return ld._get_active_alert_counts_by_account()


def test_by_resource_reports_worst_severity_not_first_found(db):
    _seed_mixed(db)
    api = _api()
    out = api.alerts_by_resource(account_id=1, service=None, current_user=USER)
    assert out["i-1"]["worst"] == "CRITICAL" and out["i-1"]["critical"] == 1 and out["i-1"]["warning"] == 1
    assert out["i-2"]["worst"] == "WARNING"
    assert out["i-3"]["worst"] is None and out["i-3"]["stale"] == 1        # stale never paints red
    assert "i-x" not in out                                                # other account never leaks in


def test_service_summary_covers_extended_and_directory_services(db):
    db.resource(1, "sns", "topic-1")
    db.resource(1, "acm", "cert-1")
    db.alert(1, "topic-1", "numberofnotificationsfailed", "CRITICAL")
    db.alert(1, "cert-1", "daystoexpiry", "WARNING")
    api = _api()
    svcs = api.alert_summary(account_id=1, current_user=USER)["accounts"]["1"]["services"]
    assert svcs["sns"]["critical"] == 1 and svcs["acm"]["warning"] == 1


def test_elb_alerts_roll_up_under_alb_and_nlb_keys(db):
    db.resource(1, "elb", "arn:aws:elasticloadbalancing:ap-south-1:1:loadbalancer/app/x/1")
    db.resource(1, "elb", "arn:aws:elasticloadbalancing:ap-south-1:1:loadbalancer/net/y/2")
    db.alert(1, "arn:aws:elasticloadbalancing:ap-south-1:1:loadbalancer/app/x/1", "errors5xx", "CRITICAL")
    db.alert(1, "arn:aws:elasticloadbalancing:ap-south-1:1:loadbalancer/net/y/2", "unhealthyhosts_describe", "WARNING")
    api = _api()
    svcs = api.alert_summary(account_id=1, current_user=USER)["accounts"]["1"]["services"]
    assert svcs["alb"]["critical"] == 1 and svcs["nlb"]["warning"] == 1


def test_scoped_user_only_sees_their_accounts(db):
    _seed_mixed(db)
    api = _api()
    api.get_accessible_account_ids = lambda user: {2}
    c = api.alert_counts(account_id=None, current_user=USER)
    assert c["critical"] == 1 and c["all"] == 1
    rows, total = api._fetch_alerts_from_db(USER, "all")
    assert total == 1 and rows[0]["resource"] == "i-x"
    with pytest.raises(Exception):
        api.alerts_by_resource(account_id=1, service=None, current_user=USER)


# ── actions ─────────────────────────────────────────────────────────────

def _act(api):
    audits = []
    api.write_audit = lambda *a, **k: audits.append(a)
    api.publish_alert_resolved = lambda **k: None
    api.invalidate_accounts_cache = lambda: None
    return audits


def test_ack_only_works_on_active_and_records_who(db):
    db.resource(1, "ec2", "i-1")
    a = db.alert(1, "i-1", "cpuutilization")
    r = db.alert(1, "i-1", "networkout", status="resolved")
    api = _api()
    audits = _act(api)
    api.ack_alert(a, current_user=USER)
    row = db.get(a)
    assert row["status"] == "acknowledged" and row["acked_by"] == "tester" and row["acked_at"]
    assert api.ack_alert(a, current_user=USER)["changed"] is False          # idempotent
    with pytest.raises(Exception) as e:
        api.ack_alert(r, current_user=USER)                                 # cannot resurrect a resolved alert
    assert "409" in str(getattr(e.value, "status_code", "")) or getattr(e.value, "status_code", 0) == 409
    assert db.get(r)["status"] == "resolved"
    assert audits and audits[0][1] == "Alert acknowledged"


def test_resolve_records_reason_and_is_idempotent(db):
    db.resource(1, "ec2", "i-1")
    a = db.alert(1, "i-1", "cpuutilization")
    api = _api()
    _act(api)
    api.resolve_alert(a, current_user=USER)
    row = db.get(a)
    assert row["status"] == "resolved" and row["resolution_reason"] == "manual" and row["resolved_by"] == "tester"
    first = row["resolved_at"]
    api.resolve_alert(a, current_user=USER)
    assert db.get(a)["resolved_at"] == first


def test_mute_is_honoured_everywhere_and_expires(db):
    db.resource(1, "ec2", "i-1")
    a = db.alert(1, "i-1", "cpuutilization", "CRITICAL")
    api = _api()
    _act(api)
    assert api.alert_counts(account_id=None, current_user=USER)["critical"] == 1
    api.mute_alert(a, minutes=30, current_user=USER)
    api._invalidate_cache()
    c = api.alert_counts(account_id=None, current_user=USER)
    assert c["critical"] == 0 and c["suppressed"] == 1
    db.x("UPDATE alerts SET muted_until = UTC_TIMESTAMP() - INTERVAL 1 MINUTE WHERE id=%s", (a,))
    api._invalidate_cache()
    assert api.alert_counts(account_id=None, current_user=USER)["critical"] == 1            # mute lapsed -> counts again


def test_clear_resolves_instead_of_deleting(db):
    db.resource(1, "ec2", "i-1")
    a = db.alert(1, "i-1", "cpuutilization")
    api = _api()
    _act(api)
    assert api.clear_alerts(current_user=USER)["count"] == 1
    row = db.get(a)
    assert row and row["status"] == "resolved" and row["resolution_reason"] == "bulk_clear"


def test_null_account_alerts_are_backfilled_only_when_unambiguous(db):
    db.resource(1, "ec2", "i-unique")
    db.resource(1, "logs", "System")
    db.resource(2, "logs", "System")
    u = db.x("INSERT INTO alerts (resource_id, metric_name, severity, status, current_value, threshold, triggered_at) "
             "VALUES ('i-unique','x','WARNING','active',1,1,UTC_TIMESTAMP())")
    amb = db.x("INSERT INTO alerts (resource_id, metric_name, severity, status, current_value, threshold, triggered_at) "
               "VALUES ('System','x','WARNING','active',1,1,UTC_TIMESTAMP())")
    here = os.path.dirname(os.path.abspath(__file__))
    sql = open(os.path.join(here, "..", "db", "migrations", "051_alert_lifecycle_hardening.sql")).read()
    import subprocess
    subprocess.run(
        ["mysql", f"-h{os.getenv('MH_TEST_DB_HOST', '127.0.0.1')}", f"-u{os.getenv('MH_TEST_DB_USER', 'mh')}",
         f"-p{os.getenv('MH_TEST_DB_PASSWORD', 'mh')}", os.getenv("MH_TEST_DB_NAME", "mh_test")],
        input=sql.encode(), check=True, capture_output=True)
    assert db.get(u)["aws_account_id"] == 1
    assert db.get(amb)["aws_account_id"] is None      # never guessed


# ── downstream consumers ────────────────────────────────────────────────

def test_status_page_ignores_stale_muted_silenced_and_other_accounts(db):
    db.resource(1, "ec2", "i-web")
    db.resource(2, "ec2", "i-web")
    install_stub("app.db", get_connection=_connect)
    install_stub("app.auth.deps", get_current_user=lambda: None)
    install_stub("app.auth.permissions", require_permission=lambda *a, **k: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda u: None)
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    sp = load_module("app/api/status_page.py")
    conn = _connect()
    conn.autocommit = True          # see rows committed by the fixture connection
    cur = conn.cursor(dictionary=True)

    assert sp._component_status(cur, ["i-web"], 1) == "operational"
    db.alert(1, "i-web", "cpuutilization", "CRITICAL", seen_min_ago=90)        # stale
    db.alert(1, "i-web", "networkout", "CRITICAL", silenced=1)                 # planned maintenance
    db.alert(1, "i-web", "diskreadbytes", "CRITICAL", muted_min=30)            # muted
    db.alert(2, "i-web", "cpuutilization", "CRITICAL")                         # someone else's account
    assert sp._component_status(cur, ["i-web"], 1) == "operational"
    db.alert(1, "i-web", "statuscheckfailed", "WARNING")
    assert sp._component_status(cur, ["i-web"], 1) == "degraded"
    db.alert(1, "i-web", "diskwritebytes", "CRITICAL")
    assert sp._component_status(cur, ["i-web"], 1) == "outage"
    cur.close()
    conn.close()


def test_health_score_counts_only_firing_alerts_per_account(db):
    db.resource(1, "ec2", "i-1")
    db.resource(2, "ec2", "i-1")
    db.alert(1, "i-1", "cpuutilization", "CRITICAL")                    # firing -> -40
    db.alert(1, "i-1", "networkout", "CRITICAL", seen_min_ago=90)       # stale -> ignored
    db.alert(1, "i-1", "multivariate_anomaly", "WARNING")               # hidden -> ignored
    db.alert(2, "i-1", "cpuutilization", "WARNING", status="acknowledged")
    install_stub("app.db", get_connection=_connect)
    hs = load_module("app/collector/health_score.py")
    hs.recompute_health_scores()
    rows = {(r["aws_account_id"], r["resource_id"]): r["health_score"] for r in db.q("SELECT * FROM resource_health")}
    assert rows == {(1, "i-1"): 60}      # account 2 has no firing alert -> no row (healthy); no cross-account overwrite
    # recovery removes the row
    db.x("UPDATE alerts SET status='resolved' WHERE aws_account_id=1 AND metric_name='cpuutilization'")
    hs.recompute_health_scores()
    assert db.q("SELECT * FROM resource_health") == []


def test_synthetic_alert_is_written_with_its_account_and_is_visible(db):
    db.resource(1, "synthetic_check", "synthetic-7")
    install_stub("app.db", get_connection=_connect)
    syn = load_module("app/collector/synthetic.py")
    check = {"id": 7, "name": "homepage", "aws_account_id": 1, "environment": "prod",
             "consecutive_failures": 3, "consecutive_failure_threshold": 3}
    conn = _connect()
    cur = conn.cursor(dictionary=True)
    syn._write_or_update_alert(cur, "synthetic-7", check, "timeout")
    syn._write_or_update_alert(cur, "synthetic-7", check, "timeout")   # second failure updates, not duplicates
    conn.commit()
    assert db.one("SELECT COUNT(*) n FROM alerts WHERE resource_id='synthetic-7'")["n"] == 1
    assert db.one("SELECT aws_account_id a FROM alerts WHERE resource_id='synthetic-7'")["a"] == 1
    api = _api()
    rows, total = api._fetch_alerts_from_db(USER, "critical")
    assert total == 1 and rows[0]["resource"] == "synthetic-7"
    syn._resolve_alert(cur, "synthetic-7", 1)
    conn.commit()
    assert db.one("SELECT status, resolution_reason FROM alerts WHERE resource_id='synthetic-7'") == \
        {"status": "resolved", "resolution_reason": "recovered"}
    cur.close()
    conn.close()


def test_multivariate_writer_sets_account_and_scopes_lookup(db):
    db.resource(1, "ec2", "i-same")
    db.resource(2, "ec2", "i-same")
    install_stub("app.db", get_connection=_connect)
    mv = load_module("app/collector/multivariate_anomaly.py")
    conn = _connect()
    cur = conn.cursor(dictionary=True)
    mv._upsert_anomaly_alert(cur, "i-same", 1, -0.2, ["cpu"])
    mv._upsert_anomaly_alert(cur, "i-same", 2, -0.3, ["cpu"])
    conn.commit()
    rows = db.q("SELECT aws_account_id FROM alerts WHERE metric_name='multivariate_anomaly' ORDER BY aws_account_id")
    assert [r["aws_account_id"] for r in rows] == [1, 2]                  # one per account, none NULL
    # only account 1 recovers
    assert mv._resolve_cleared_anomalies(cur, {(2, "i-same")}) == 1
    conn.commit()
    assert db.one("SELECT status FROM alerts WHERE aws_account_id=1")["status"] == "resolved"
    assert db.one("SELECT status FROM alerts WHERE aws_account_id=2")["status"] == "active"
    cur.close()
    conn.close()
