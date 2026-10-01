"""build_metric_meta: no DB needed -- app.db and the alert fetch are stubbed."""
import contextlib
import sys
import types

fake_db = types.ModuleType("app.db")
fake_db.get_connection = lambda: None


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._rows


def _install(rows, alerts):
    @contextlib.contextmanager
    def cur(dictionary=False, commit=True):
        yield None, _Cur(rows)
    fake_db.get_db_cursor = cur
    sys.modules["app.db"] = fake_db
    for m in ("app.metric_meta", "app.alert_rules", "app.alert_visibility"):
        sys.modules.pop(m, None)
    import app.alert_rules as ar
    ar.fetch_open_alert_rows = lambda c, ids: alerts
    import app.metric_meta as mm
    return mm


def _row(**kw):
    base = dict(metric_name="CPUUtilization", service="ec2", unit="Percent", statistic="Average",
                description="", resource_type="ec2", warning_value=70, critical_value=90,
                comparison=">", enabled=1, use_dynamic=0, dynamic_k=3.0)
    base.update(kw)
    return base


def test_alert_threshold_polling_for_ec2_cpu():
    mm = _install([_row()], [{"resource_id": "i-1", "metric_name": "cpuutilization",
                              "severity": "CRITICAL", "state": "firing"}])
    out = mm.build_metric_meta(1, "aws", "ec2", ["i-1"])
    m = out["metrics"]["CPUUtilization"]
    assert m["title"] == "CPU utilization (%)"
    assert m["alert"] == {"severity": "CRITICAL", "state": "firing"}
    assert m["threshold"]["warning"] == 70 and m["threshold"]["mode"] == "static"
    assert m["poll_label"] == "5 min" and m["period_seconds"] == 300
    assert "Sum" not in m["stats"] and out["alerts_unmatched"] == []


def test_ebs_rate_threshold_scaled_with_data():
    mm = _install([_row(metric_name="VolumeReadOps", service="ebs", resource_type="ebs", unit="Count",
                        statistic="Sum", warning_value=3000, critical_value=6000)], [])
    m = mm.build_metric_meta(1, "aws", "ebs", ["vol-1"])["metrics"]["VolumeReadOps"]
    assert m["unit"] == "Count/Second" and m["threshold"]["critical"] == 20.0   # 6000 / 300 s


def test_unknown_service_and_unmatched_alert_never_raise():
    mm = _install([_row(metric_name="Weird", service="zzz", resource_type="zzz", unit=None, statistic=None)],
                  [{"resource_id": "r", "metric_name": "something_else", "severity": "WARNING", "state": "firing"}])
    out = mm.build_metric_meta(1, "gcp", "zzz", ["r"])
    assert out["metrics"]["Weird"]["title"] == "Weird"
    assert out["alerts_unmatched"][0]["metric"] == "something_else"
