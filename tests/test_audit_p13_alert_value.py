# tests/test_audit_p13_alert_value.py
"""Audit D4: one column for the alert reading (current_value); the legacy `value` is no longer served."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_api_no_longer_returns_the_legacy_value_column():
    src = (ROOT / "app/api/alerts.py").read_text()
    select = src[src.index("SELECT\n                a.id,"):src.index("FROM", src.index("SELECT\n                a.id,") + 200)]
    assert "a.current_value" in select and "a.breach_value" in select
    assert not re.search(r"^\s*a\.value,?\s*$", select, re.M)


def test_every_alert_writer_sets_current_value_never_the_legacy_column():
    for rel in ("app/collector/alert_evaluator.py", "app/collector/synthetic.py", "app/collector/multivariate_anomaly.py"):
        for stmt in re.findall(r"INSERT INTO alerts\s*\((.*?)\)", (ROOT / rel).read_text(), re.S):
            cols = [c.strip() for c in stmt.split(",")]
            assert "current_value" in cols and "value" not in cols, rel


def test_drawer_reads_only_current_value():
    jsx = (ROOT / "frontend/src/components/AlertInvestigation.jsx").read_text()
    assert "a.current_value ?? a.value" not in jsx and "formatMetricValue(a.metric_name, a.current_value)" in jsx


def test_migration_081_backfills_without_destroying_anything():
    sql = (ROOT / "db/migrations/081_alerts_value_into_current_value.sql").read_text()
    body = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    assert "SET current_value = value" in body and "WHERE current_value IS NULL" in body
    assert not re.search(r"\bDROP\b|\bDELETE\b|\bTRUNCATE\b", body, re.I)
