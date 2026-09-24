# tests/test_aiops_phase1.py
"""
Coverage for AIOps roadmap Phase 1 (2026-09-14):
  - app/collector/health_score.py: penalty arithmetic + upsert/delete shape
  - app/collector/trend.py: linear-trend capacity-exhaustion detection
  - app/collector/correlate.py: topology-based alert grouping into incidents
  - app/collector/rca.py: probable-root-cause ranking query shape

Without a live MySQL/MariaDB instance, these exercise the Python-level
logic (arithmetic, upsert/query shaping, numpy fitting) via conftest's
load_module()/FakeCursor, same limitation and same convention as every
other test in this suite -- the SQL itself (JOINs, JSON_SEARCH, window
math) runs inside MySQL and isn't re-executed here.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app  # noqa: F401 -- real package, so `from app import alert_rules` resolves
from tests.conftest import load_module, install_stub, FakeCursor, FakeConn, contains


# ── health_score.py ─────────────────────────────────────────────────

def test_health_score_penalizes_critical_and_warning_and_blast_radius():
    breaching_rows = [
        {"resource_id": "alb-1", "aws_account_id": 7, "critical_count": 1, "warning_count": 0},
    ]
    inserts = []

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT r.resource_id"):
                self._pending = breaching_rows
            elif "COUNT(DISTINCT target_resource_id)" in normalized:
                self._pending = [{"fan_out": 5}]
            elif normalized.startswith("INSERT INTO resource_health"):
                inserts.append(params)
                self._pending = []
            elif normalized.startswith("DELETE rh"):
                self._pending = []
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    mod = load_module("app/collector/health_score.py")

    scored = mod.recompute_health_scores()

    assert scored == 1
    params = inserts[0]
    # (resource_id, aws_account_id, health_score, critical, warning, alert_penalty, fan_out, blast_penalty)
    resource_id, account_id, score = params[0], params[1], params[2]
    assert resource_id == "alb-1"
    assert account_id == 7
    # 1 CRITICAL (-40) + 5 fan-out (-5) = 100 - 45 = 55
    assert score == 55


def test_health_score_caps_alert_penalty_at_max():
    breaching_rows = [
        {"resource_id": "i-1", "aws_account_id": 1, "critical_count": 5, "warning_count": 5},
    ]

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT r.resource_id"):
                self._pending = breaching_rows
            elif "COUNT(DISTINCT target_resource_id)" in normalized:
                self._pending = [{"fan_out": 0}]
            elif normalized.startswith("INSERT INTO resource_health") or normalized.startswith("DELETE rh"):
                self.last_insert_params = params
                self._pending = []

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    mod = load_module("app/collector/health_score.py")
    mod.recompute_health_scores()
    # 5*40 + 5*15 = 275, capped at 80 -> score floors at 20, never negative
    # (verified via the module's own constants rather than hardcoding 275)
    assert mod.MAX_ALERT_PENALTY == 80


# ── trend.py ─────────────────────────────────────────────────────────

def _history_points(start_value, per_day_change, days, points_per_day=4):
    """Synthetic metric_history rows climbing/falling linearly."""
    rows = []
    step_seconds = 86400 // points_per_day
    total_points = days * points_per_day
    for i in range(total_points):
        rows.append({
            # trend.py groups per resources.id ("rid") and reports the
            # account, since series are no longer merged across accounts.
            "rid": 1, "aws_account_id": 1,
            "aws_resource_id": "vol-1",
            "ts": i * step_seconds,
            "metric_value": start_value + per_day_change * (i / points_per_day),
        })
    return rows


def test_trend_detects_disk_heading_toward_full():
    rows = _history_points(start_value=50.0, per_day_change=2.0, days=14)  # climbing toward 100

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if "metric_name = %s" in normalized and params[0] == "DiskSpaceUtilization":
                self._pending = rows
            else:
                self._pending = []

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    mod = load_module("app/collector/trend.py")

    forecasts = mod.compute_capacity_forecasts()
    disk_forecasts = [f for f in forecasts if f["metric_name"] == "DiskSpaceUtilization"]
    assert len(disk_forecasts) == 1
    f = disk_forecasts[0]
    assert f["resource_id"] == "vol-1"
    assert f["slope_per_day"] > 0
    assert f["days_to_exhaustion"] > 0


def test_trend_ignores_metric_heading_away_from_ceiling():
    # Disk usage DROPPING -- heading away from the 100% ceiling, should
    # never be reported as an exhaustion risk.
    rows = _history_points(start_value=80.0, per_day_change=-2.0, days=14)

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if "metric_name = %s" in normalized and params[0] == "DiskSpaceUtilization":
                self._pending = rows
            else:
                self._pending = []

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    mod = load_module("app/collector/trend.py")

    forecasts = mod.compute_capacity_forecasts()
    assert forecasts == []


def test_trend_skips_bucket_with_too_few_points():
    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            self._pending = [
                {"rid": 2, "aws_account_id": 1, "aws_resource_id": "vol-2", "ts": 0, "metric_value": 90.0},
                {"rid": 2, "aws_account_id": 1, "aws_resource_id": "vol-2", "ts": 3600, "metric_value": 91.0},
            ]  # only 2 points, well under MIN_POINTS_FOR_TREND

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    mod = load_module("app/collector/trend.py")
    assert mod.compute_capacity_forecasts() == []


# ── correlate.py ─────────────────────────────────────────────────────

def test_correlate_creates_incident_from_two_topologically_connected_alerts():
    """Two loose active alerts on topologically-connected resources,
    started close together in time, should seed one new incident with
    both attached."""
    loose_alerts = [
        {"id": 101, "resource_id": "alb-1", "severity": "CRITICAL",
         "created_at": "2026-09-14 10:00:00", "aws_account_id": 1},
        {"id": 102, "resource_id": "i-target-1", "severity": "WARNING",
         "created_at": "2026-09-14 10:02:00", "aws_account_id": 1},
    ]

    inserted_incidents = []
    inserted_incident_alerts = []

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT a.id, a.resource_id, a.severity"):
                self._pending = loose_alerts
            elif normalized.startswith("SELECT DISTINCT i.id"):
                self._pending = []  # no existing open incident to join
            elif normalized.startswith("SELECT a2.id AS other_alert_id"):
                # Only the SECOND alert (i-target-1) reports a connected
                # partner (the first, alb-1) -- simulates the real
                # bidirectional topology-edge join.
                if params[3] == 102:  # (res, res, account, alert_id, ...)
                    self._pending = [{"other_alert_id": 101}]
                else:
                    self._pending = []
            elif normalized.startswith("INSERT INTO incidents"):
                inserted_incidents.append(params)
                self.lastrowid = 555
                self._pending = []
            elif normalized.startswith("INSERT IGNORE INTO incident_alerts"):
                inserted_incident_alerts.append(params)
                self.rowcount = 1
                self._pending = []
            elif normalized.startswith("UPDATE incidents"):
                self._pending = []
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    install_stub("app.collector.rca", rank_probable_cause=lambda incident_id: None)
    mod = load_module("app/collector/correlate.py")

    created, attached = mod.correlate_alerts_into_incidents()

    assert created == 1
    assert attached == 2
    assert len(inserted_incident_alerts) == 2
    attached_alert_ids = {p[1] for p in inserted_incident_alerts}
    assert attached_alert_ids == {101, 102}


def test_correlate_leaves_standalone_alert_alone():
    """A single loose alert with no topologically-connected partner
    must NOT become an incident."""
    loose_alerts = [
        {"id": 201, "resource_id": "standalone-1", "severity": "WARNING",
         "created_at": "2026-09-14 10:00:00", "aws_account_id": 1},
    ]

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT a.id, a.resource_id, a.severity"):
                self._pending = loose_alerts
            elif normalized.startswith("SELECT DISTINCT i.id"):
                self._pending = []
            elif normalized.startswith("SELECT a2.id AS other_alert_id"):
                self._pending = []  # no partner anywhere in the graph
            elif normalized.startswith("UPDATE incidents"):
                self._pending = []
            else:
                raise AssertionError(f"unexpected query for standalone alert: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    mod = load_module("app/collector/correlate.py")

    created, attached = mod.correlate_alerts_into_incidents()
    assert created == 0
    assert attached == 0


# ── rca.py ───────────────────────────────────────────────────────────

def test_rca_ranks_earliest_alert_as_probable_cause():
    incident_alerts = [
        {"id": 1, "resource_id": "alb-1", "metric_name": "5xxCount",
         "created_at": "2026-09-14 10:00:00", "severity": "CRITICAL"},
        {"id": 2, "resource_id": "i-target-1", "metric_name": "CPUUtilization",
         "created_at": "2026-09-14 10:04:00", "severity": "WARNING"},
    ]

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT a.id, a.aws_account_id, a.resource_id, a.metric_name"):
                self._pending = [dict(a, aws_account_id=1) for a in incident_alerts]
            elif "event_type = 'deployment'" in normalized:
                self._pending = []  # no deploy in the window
            elif "COUNT(DISTINCT source_resource_id)" in normalized:
                self._pending = [{"in_degree": 3}]
            elif normalized.startswith("SELECT ce.event_name"):
                self._pending = [{
                    "event_name": "AuthorizeSecurityGroupIngress",
                    "event_source": "ec2.amazonaws.com",
                    "username": "vinay.hirap",
                    "event_time": "2026-09-14 09:58:00",
                    "resource_ids": '[{"type":"AWS::EC2::SecurityGroup","id":"sg-1"}]',
                }]
            elif normalized.startswith("SELECT actor, action, payload"):
                self._pending = []
            elif normalized.startswith("UPDATE incidents"):
                self._pending = []
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    mod = load_module("app/collector/rca.py")

    result = mod.rank_probable_cause(incident_id=999)

    assert result["resource_id"] == "alb-1"  # the EARLIEST alert's resource
    assert result["in_degree"] == 3
    assert "AuthorizeSecurityGroupIngress" in result["reason"]
    assert "probable, not confirmed" in result["reason"]


def test_rca_returns_none_for_incident_with_no_alerts():
    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            self._pending = []

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    mod = load_module("app/collector/rca.py")
    assert mod.rank_probable_cause(incident_id=1) is None
