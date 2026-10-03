# tests/test_audit_p1_breach_snapshot.py
"""
Audit A3: an alert's displayed "value vs threshold" must be the breaching
reading, not the live value overwritten by healthy cycles.
Source-level guards (this suite has no DB; same convention as
test_alerts_schema_references.py).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text()


def test_migration_077_adds_both_columns_idempotently():
    sql = _read("db/migrations/077_alert_breach_snapshot.sql")
    for col in ("breach_value", "breach_threshold"):
        assert f"column_name = '{col}'" in sql
        assert f"ADD COLUMN {col} DOUBLE NULL" in sql
    assert "information_schema.columns" in sql


def test_evaluator_writes_snapshot_on_insert_and_on_breach_update_only():
    src = _read("app/collector/alert_evaluator.py")
    # new alert row
    assert "current_value, threshold, breach_value, breach_threshold," in src
    # open alert that is still breaching
    assert '"breach_value = %s", "breach_threshold = %s"' in src
    # the recovery / healthy-cycle UPDATEs must NOT touch the snapshot
    start = src.index("if not is_breaching:")
    end = src.index("# `existing` may be ACKNOWLEDGED", start)
    assert "breach_value" not in src[start:end]


def test_alerts_api_exposes_snapshot():
    src = _read("app/api/alerts.py")
    assert "a.breach_value" in src and "a.breach_threshold" in src


def test_timeline_uses_snapshot_not_live_value():
    jsx = _read("frontend/src/components/ResourceEvidence.jsx")
    assert "it.alert.breach_value" in jsx
    assert "it.alert.current_value ?? it.alert.value" not in jsx
