# tests/test_audit_p8_threshold_coverage.py
"""Audit F1: blank (placeholder) thresholds are surfaced and fixable without overwriting a human's value."""
import sys
from pathlib import Path

import app          # noqa: F401

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from app import threshold_coverage as tc  # noqa: E402
from app import threshold_defaults as td  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PH = (1000000.0, 5000000.0, ">")


def _row(i, metric, w, c, comp=">", enabled=1, dyn=0, svc="acm"):
    return {"id": i, "resource_type": svc, "warning_value": w, "critical_value": c, "comparison": comp,
            "enabled": enabled, "use_dynamic": dyn, "metric_name": metric, "service": svc}


class _Cur:
    def __init__(self, rows):
        self.rows, self.updates = rows, []
        self._r = []
        self.rowcount = 0
    def execute(self, sql, params=None):
        if sql.lstrip().startswith("SELECT"):
            self._r = list(self.rows)
        elif sql.lstrip().startswith("UPDATE thresholds"):
            tid = params[3]
            row = next(r for r in self.rows if r["id"] == tid)
            still_placeholder = (row["warning_value"], row["critical_value"], row["comparison"]) == (params[5], params[6], params[7])
            self.rowcount = 1 if still_placeholder else 0
            if still_placeholder:
                self.updates.append((tid, params[0], params[1], params[2]))
                row["warning_value"], row["critical_value"], row["comparison"] = params[0], params[1], params[2]
    def fetchall(self):
        return self._r


def test_classification():
    assert tc.classify(_row(1, "DaysToExpiry", *PH)) == "upgradable"             # default (30,7,'<') exists
    assert tc.classify(_row(2, "DaysToExpiry", 45, 14, "<")) == "alerting"         # a person set it
    assert tc.classify(_row(3, "RequestCount", *PH)) == "collect_only"            # volume metric, no honest line
    assert tc.classify(_row(4, "DaysToExpiry", *PH, enabled=0)) == "disabled"
    assert tc.classify(_row(5, "CPUUtilization", *PH, dyn=1)) == "alerting"        # dynamic band counts


def test_coverage_numbers_and_service_breakdown():
    rows = [_row(1, "DaysToExpiry", *PH), _row(2, "CPUUtilization", 70, 90, svc="ec2"),
            _row(3, "RequestCount", *PH, svc="alb"), _row(4, "NumberOfBackupJobsFailed", *PH, svc="backup")]
    cov = tc.coverage(_Cur(rows), 10)
    assert cov["enabled_metrics"] == 4 and cov["alerting"] == 1 and cov["coverage_percent"] == 25.0
    assert cov["totals"] == {"alerting": 1, "upgradable": 2, "collect_only": 1, "disabled": 0}
    assert {u["metric"] for u in cov["upgradable"]} == {"DaysToExpiry", "NumberOfBackupJobsFailed"}
    assert cov["by_service"]["alb"]["collect_only"] == 1


def test_dry_run_changes_nothing():
    cur = _Cur([_row(1, "DaysToExpiry", *PH)])
    out = tc.upgrade_placeholders(cur, 10)                      # default is dry_run=True
    assert out["dry_run"] is True and out["candidates"] == 1 and out["applied"] == 0 and cur.updates == []


def test_apply_sets_the_shipped_default_and_never_overwrites_human_values():
    rows = [_row(1, "DaysToExpiry", *PH), _row(2, "NumberOfBackupJobsFailed", 3, 9, ">", svc="backup")]
    cur = _Cur(rows)
    out = tc.upgrade_placeholders(cur, 10, dry_run=False)
    assert out["applied"] == 1 and cur.updates == [(1, 30, 7, "<")]
    assert (rows[1]["warning_value"], rows[1]["critical_value"]) == (3, 9)      # untouched


def test_row_edited_between_read_and_write_is_not_clobbered():
    rows = [_row(1, "DaysToExpiry", *PH)]
    cur = _Cur(rows)
    real_exec = cur.execute
    def racing(sql, params=None):
        if sql.lstrip().startswith("UPDATE"):
            rows[0]["warning_value"], rows[0]["critical_value"] = 60, 20     # someone saved a value just now
        return real_exec(sql, params)
    cur.execute = racing
    out = tc.upgrade_placeholders(cur, 10, dry_run=False)
    assert out["applied"] == 0 and cur.updates == [] and rows[0]["warning_value"] == 60


def test_newly_defaulted_metrics_are_real_lines():
    for m in ("Evictions", "FaultRequestCount", "ReadProvisionedThroughputExceeded", "WriteProvisionedThroughputExceeded"):
        assert not td.is_placeholder_threshold(*td.DEFAULT_THRESHOLDS[m]), m


def test_api_and_ui_wiring():
    api = (ROOT / "app/api/settings.py").read_text()
    assert '@router.get("/thresholds/coverage")' in api and '@router.post("/thresholds/apply-defaults")' in api
    apply_fn = api.split("def apply_recommended_defaults")[1].split("@router.get")[0]
    assert 'require_permission("alerts.configure")' in apply_fn and "dry_run: bool = Query(True" in apply_fn
    assert "_require_account_access(account_id, current_user)" in apply_fn
    assert apply_fn.index("_require_account_access") < apply_fn.index("upgrade_placeholders")
    ui = (ROOT / "frontend/src/pages/Settings.jsx").read_text()
    assert "<ThresholdCoverage" in ui


# ── per-mount disk metrics (found on prod: disk_used_percent__boot was blank and could never alert) ──

def test_real_mounts_get_the_base_disk_default_but_pseudo_mounts_do_not():
    assert tc.recommended("disk_used_percent__boot") == (80, 90, ">")
    assert tc.recommended("disk_used_percent__data") == (80, 90, ">")
    assert tc.recommended("mem_used_percent__x") == (80, 90, ">")
    assert tc.recommended("disk_used_percent__snap_core20_1822") is None      # squashfs loopback: 100% by design
    assert tc.recommended("disk_used_percent__run_lock") is None
    assert tc.recommended("disk_used_percent__loop3") is None
    assert tc.recommended("CPUCreditUsage") is None                           # unrelated volume metric unchanged


def test_blank_mount_row_is_upgradable_and_a_set_one_is_left_alone():
    assert tc.classify(_row(1, "disk_used_percent__boot", *PH, svc="ec2")) == "upgradable"
    assert tc.classify(_row(2, "disk_used_percent__boot", 85, 95, svc="ec2")) == "alerting"
    assert tc.classify(_row(3, "disk_used_percent__snap_core", *PH, svc="ec2")) == "collect_only"
    cur = _Cur([_row(1, "disk_used_percent__boot", *PH, svc="ec2")])
    out = tc.upgrade_placeholders(cur, 10, dry_run=False)
    assert out["applied"] == 1 and cur.updates == [(1, 80, 90, ">")]


def test_new_mounts_are_never_registered_blank():
    src = (ROOT / "app/collector/disk_mounts.py").read_text()
    assert "AND warning_value = 1000000 AND critical_value = 5000000 AND comparison = '>'" in src
    assert src.index("if cloned:") < src.index("if not cloned:")
