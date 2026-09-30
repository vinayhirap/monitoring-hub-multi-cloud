# tests/test_alert_size_floor_all_modes.py
"""2026-09-30: the minimum-size floor now covers Network In/Out (1 MB/min) and WAF Blocked Requests (10),
in every threshold mode. Prod screenshots that motivated it: JumpServer-POC "Network Out 19.5K / 18.4K"
(0.3 KB/s), "Network In 22.4K / 16.3K", and "Blocked Requests 2 / 1".

Network In/Out use the placeholder (anomaly-only) mode; Blocked Requests is a plain STATIC rule (warn 1,
critical 5), so the floor is applied in _evaluate_row itself. These tests drive the real _evaluate_row."""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app.alert_rules  # noqa: F401,E402
import app.aws.metric_catalog_data  # noqa: F401,E402
import app.collector.polling_model  # noqa: F401,E402
from datetime import datetime  # noqa: E402
from tests.conftest import FakeCursor  # noqa: E402
from tests.test_baseline_and_dynamic_bounds import _load_alert_evaluator, _baseline_cursor  # noqa: E402


class _Rec(FakeCursor):
    """Records every statement; answers the lookups _evaluate_row makes."""

    def __init__(self, existing=None, baseline=None, cycles=1):
        super().__init__([])
        self.cycles, self.pending = cycles, None
        self.sql, self.params_by_sql = [], []
        self.existing, self.baseline = existing, baseline
        self.lastrowid, self.rowcount = 101, 1

    def execute(self, sql, params=None):
        n = " ".join(sql.split())
        self.sql.append(n)
        self.params_by_sql.append(tuple(params or ()))
        if n.startswith("SELECT id, severity, status FROM alerts"):
            self._pending = [self.existing] if self.existing else []
        elif n.startswith("INSERT INTO alert_pending"):
            self.pending = tuple(params or ())           # (acct, resource, metric, severity, env, value, threshold)
            self._pending = []
        elif n.startswith("SELECT breach_cycles, severity, first_breach_at FROM alert_pending"):
            sev = self.pending[3] if self.pending else "WARNING"      # what the evaluator computed, as the real table would hold it
            self._pending = [{"breach_cycles": self.cycles, "severity": sev, "first_breach_at": datetime(2026, 9, 30, 10, 0)}]
        elif "FROM metric_baseline" in n:
            self._pending = [self.baseline] if self.baseline else []
        elif n.startswith("SELECT healthy_streak FROM alerts"):
            self._pending = [{"healthy_streak": 0}]
        else:
            self._pending = []

    def inserted_alert(self):
        for n, p in zip(self.sql, self.params_by_sql):
            if n.startswith("INSERT INTO alerts"):
                return p
        return None

    def ran(self, fragment):
        return any(fragment in n for n in self.sql)


def _row(metric, value, warn, crit, cmp_=">", use_dynamic=0):
    return {"aws_resource_id": "res-1", "metric_name": metric, "metric_value": value, "aws_account_id": 7,
            "cadence": "core", "evaluation_period": 5, "tags": '{"Environment": "prod"}', "unit": "Count",
            "warning_value": warn, "critical_value": crit, "comparison": cmp_, "use_dynamic": use_dynamic,
            "dynamic_k": 3.0, "resource_type": "wafv2", "resource_name": "acl", "service": "wafv2",
            "region": "ap-south-1", "default_region": "ap-south-1", "account_name": "acct",
            "resource_db_id": 1, "silenced": 0}


def _eval(row, cur):
    ev = _load_alert_evaluator()
    stats = {"new": 0, "resolved": 0, "already_open": 0, "pending_touched": 0, "reopened": 0, "failed": 0}
    ev._evaluate_row(cur, row, {}, stats)
    return stats


# ── static mode: Blocked Requests (warn 1, critical 5) ───────────────

def test_blocked_requests_below_the_floor_never_alert():
    for value in (2, 8, 10):                                    # the screenshot's 2/1, and up to the floor itself
        cur = _Rec()
        _eval(_row("blockedrequests", value, 1.0, 5.0), cur)
        assert cur.inserted_alert() is None, value
        assert not cur.ran("INSERT INTO alert_pending"), "not even a pending candidate below the floor"


def test_blocked_requests_above_the_floor_alert_with_configured_severity_and_a_floor_threshold():
    cur = _Rec()
    _eval(_row("blockedrequests", 12, 1.0, 5.0), cur)
    p = cur.inserted_alert()
    assert p is not None
    assert "CRITICAL" in p                                       # 12 > configured critical 5: severity unchanged
    assert 10.0 in p and 1.0 not in p                            # shown threshold lifted to the floor, not "12 / 1"


def test_a_metric_without_a_floor_is_untouched():
    cur = _Rec()
    _eval(_row("allowedrequests", 2, 1.0, 5.0), cur)             # same numbers, no floor -> still alerts
    assert cur.inserted_alert() is not None


def test_below_floor_reading_resolves_an_open_alert_instead_of_updating_it_as_a_breach():
    cur = _Rec(existing={"id": 5, "severity": "WARNING", "status": "active"})
    _eval(_row("blockedrequests", 2, 1.0, 5.0), cur)
    assert cur.ran("healthy_streak = IF(")                       # healthy branch
    assert not any("healthy_streak = 0" in n or "healthy_streak = 0," in n for n in cur.sql)


# ── anomaly-only mode: Network In/Out on an idle jump server ─────────

def test_idle_jump_server_network_no_longer_alerts_but_real_traffic_still_does():
    placeholder = (1000000.0, 5000000.0)
    for metric, value in (("networkout", 19_500.0), ("networkin", 22_400.0)):      # bytes/minute, the screenshot values
        cur = _Rec(baseline={"mean_value": 3000.0, "stddev_value": 5000.0, "sample_count": 100})
        _eval(_row(metric, value, *placeholder), cur)
        assert cur.inserted_alert() is None, metric
    # anomaly-only rules need ANOMALY_MIN_CYCLES (3) counted breaches on the core cadence
    cur = _Rec(baseline={"mean_value": 3000.0, "stddev_value": 5000.0, "sample_count": 100}, cycles=3)
    _eval(_row("networkin", 8_000_000.0, *placeholder), cur)                       # 8 MB/min ~ 130 KB/s: real traffic
    p = cur.inserted_alert()
    assert p is not None and 1_000_000.0 in p                                      # line = the floor


# ── direction matters ────────────────────────────────────────────────

def test_the_floor_only_applies_to_greater_than_rules():
    cur = _Rec()
    _eval(_row("blockedrequests", 4, 5.0, 2.0, cmp_="<"), cur)   # a "<" rule: 4 < 5 breaches; floor must not hide it
    assert cur.inserted_alert() is not None


def test_none_values_do_not_crash_the_guard():
    cur = _Rec()
    _eval(_row("blockedrequests", None, 1.0, 5.0), cur)
    assert cur.inserted_alert() is None
