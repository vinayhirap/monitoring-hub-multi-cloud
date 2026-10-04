# tests/test_audit_p7_account_removal_safeguards.py
"""Audit C7/D5: say what removal destroyed, and let the operator download the history first."""
import datetime
import json
import sys
from pathlib import Path

import app          # noqa: F401

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from app import account_data as ad  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class _Cur:
    def __init__(self, counts=None, fail_on=(), account=None, tables=None):
        self.counts, self.fail_on, self.account, self.tables = counts or {}, set(fail_on), account, tables or {}
        self._rows = []; self._one = None; self.sql = []
    def execute(self, sql, params=None):
        self.sql.append(sql)
        self._rows, self._one = [], None
        if sql.startswith("SELECT COUNT(*)"):
            table = sql.split("FROM ")[1].split(" ")[0]
            if table in self.fail_on:
                raise RuntimeError("no such table")
            self._one = {"n": self.counts.get(table, 0)}
        elif "FROM aws_accounts" in sql:
            self._one = self.account
        else:
            for t, rows in self.tables.items():
                if f"FROM {t} " in sql:
                    n = int(sql.rsplit("LIMIT ", 1)[1])
                    self._rows = rows[:n]
    def fetchone(self):
        return self._one
    def fetchall(self):
        return self._rows


def test_counts_cover_the_destroyed_tables_and_skip_broken_ones():
    cur = _Cur(counts={"alerts": 312, "resources": 15, "incidents": 4}, fail_on={"metric_baseline"})
    out = ad.count_account_data(cur, 10)
    assert out["alerts"] == 312 and out["resources"] == 15 and "metric_baseline" not in out
    assert set(ad.DATA_TABLES) >= {"alerts", "resources", "incidents", "security_findings"}


def test_format_counts_omits_zeros_and_handles_empty():
    assert ad.format_counts({"alerts": 3, "resources": 0, "incidents": 2}) == "alerts=3, incidents=2"
    assert ad.format_counts({"alerts": 0}) == "no stored data"


def test_export_contains_history_but_no_credentials():
    acct = {"id": 10, "account_name": "U4RAD", "account_id": "123456789012", "provider": "aws",
            "default_region": "ap-south-1", "status": "active"}
    t = datetime.datetime(2026, 10, 1, 12, 0, 0)
    cur = _Cur(account=acct, tables={
        "alerts": [{"id": 1, "resource_id": "i-1", "metric_name": "CPUUtilization", "triggered_at": t}],
        "incidents": [{"id": 5, "title": "x", "started_at": t}],
        "resources": [{"resource_type": "ec2", "resource_id": "i-1", "name": "web", "last_seen_at": t}],
    })
    data = ad.build_export(cur, 10)
    text = json.dumps(data)                                 # fully serialisable (datetimes converted)
    assert data["account"]["account_name"] == "U4RAD"
    assert data["alerts"][0]["triggered_at"] == "2026-10-01T12:00:00"
    assert data["truncated"] == {"alerts": False, "incidents": False, "resources": False}
    for secret in ("role_arn", "external_id", "secret", "password", "access_key"):
        assert secret not in text.lower().replace("no credentials", "")
    first_select = [s for s in cur.sql if "FROM aws_accounts" in s][0]
    assert "role_arn" not in first_select and "external_id" not in first_select


def test_export_reports_truncation_and_unknown_account():
    rows = [{"id": i, "resource_id": "r"} for i in range(ad.EXPORT_ALERT_LIMIT + 50)]
    cur = _Cur(account={"id": 1, "account_name": "A", "account_id": "1", "provider": "aws",
                        "default_region": "r", "status": "active"}, tables={"alerts": rows})
    data = ad.build_export(cur, 1)
    assert data["truncated"]["alerts"] is True and len(data["alerts"]) == ad.EXPORT_ALERT_LIMIT
    assert ad.build_export(_Cur(account=None), 99) == {}


def test_wiring_endpoint_permission_audit_and_ui():
    api = (ROOT / "app/api/admin/accounts.py").read_text()
    assert '@router.get("/{account_id}/export")' in api
    exp = api.split('def export_account_history')[1].split("@router.delete")[0]
    assert 'require_permission("accounts.delete")' in exp and "Account history exported" in exp
    assert exp.index("get_accessible_account_ids(current_user)") < exp.index("build_export(cursor")   # scope check first
    dele = api.split("def delete_account")[1].split("@router.post")[0]
    assert dele.index("count_account_data") < dele.index("UPDATE aws_accounts SET status = 'inactive'")
    assert "deleted: {format_counts(destroyed)}" in dele
    ui = (ROOT / "frontend/src/components/AccountsRegions.jsx").read_text()
    assert "Download history first" in ui and "downloadAccountHistory" in ui


def test_inverted_disk_chart_explains_free_vs_used_scale():
    """Audit A4: the chart shows Windows free %, alerts quote used %. Say so on the chart."""
    jsx = (ROOT / "frontend/src/components/MetricChartCard.jsx").read_text()
    assert "{invert && <span" in jsx and "alerts use used %" in jsx
