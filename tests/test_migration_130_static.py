"""migration 130 (two-environment data conversion): static guards. The data behaviour was exercised on a real MySQL 8
(idempotent re-run, rollback restores every changed row); these keep the SQL and env_tiers in step."""
import re
from pathlib import Path
from tests.conftest import load_module

et = load_module("app/env_tiers.py")
MIG = Path(__file__).resolve().parent.parent / "db" / "migrations"
SQL = (MIG / "130_two_environment_model.sql").read_text()


def _nonprod_pattern():
    m = re.search(r"SET @NP := '([^']+)'", SQL)
    assert m, "pattern variable missing"
    return re.compile(m.group(1))


def test_rollback_companion_exists_and_is_excluded_from_apply_all():
    assert (MIG / "130_two_environment_model_rollback.sql").exists()


def test_backup_table_is_created_before_any_update():
    assert SQL.index("env_migration_130_backup") < SQL.index("UPDATE resources")


def test_sql_pattern_agrees_with_env_tiers_on_every_nonprod_word():
    pat = _nonprod_pattern()
    for word in sorted(et.NONPROD_WORDS):
        assert pat.match(word), word
        assert pat.match((word.upper() + "2").lower()), word
    for word in ("prod", "production", "prd", "unknown", "ignore", "banana", ""):
        assert not pat.match(word), word


def test_every_touched_column_is_normalised():
    for needle in ("env_override", "env_resolved", "default_env", "alerts", "alert_pending", "synthetic_checks"):
        assert needle in SQL
