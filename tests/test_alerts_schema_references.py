# tests/test_alerts_schema_references.py
"""
Regression guard for a real bug found in production (2026-09-14): the
AIOps Phase 1 modules were written against alerts.created_at and
alerts.value, columns that don't exist -- the actual evolved schema
(see app/collector/alert_evaluator.py's own INSERT INTO alerts) uses
triggered_at and current_value. Because every caller of these modules
in scheduler.py wraps them in try/except (by design, so one failing
job doesn't block the others), this was NOT surfaced as a crash --
correlate.py/rca.py silently failed every single "low" tier cycle from
deployment until this was caught by manual review of the migration
diff, with only a WARNING-level op_event as the trail.

This test is deliberately a source-text scan, not a live-DB query
(this suite has no DB to query against -- see conftest.py's own
docstring on why every other test here mocks the cursor instead). It
catches the SPECIFIC regression class that actually happened --
referencing alerts.created_at/alerts.value without going through the
`AS created_at` / `AS value` aliases the rest of this codebase already
uses to bridge the real column names (triggered_at, current_value) to
the field names the Python/frontend code expects -- not a general
SQL-correctness checker.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FILES_THAT_QUERY_ALERTS = [
    "app/collector/correlate.py",
    "app/collector/rca.py",
    "app/api/incidents.py",
]

# A raw "a.created_at" or "a2.created_at" (or "alerts.created_at") NOT
# immediately preceded by "triggered_at AS " is the exact bug that
# shipped -- alerts has no created_at column at all, only
# triggered_at. Similarly for .value vs current_value.
BROKEN_CREATED_AT = re.compile(r"\b(?:a|a2|alerts)\.created_at\b")
BROKEN_VALUE = re.compile(r"\b(?:a|a2|alerts)\.value\b")
VALID_ALIAS_CREATED_AT = re.compile(r"triggered_at\s+AS\s+created_at", re.IGNORECASE)
VALID_ALIAS_VALUE = re.compile(r"current_value\s+AS\s+value", re.IGNORECASE)


def test_no_raw_references_to_nonexistent_alerts_created_at_column():
    for rel_path in FILES_THAT_QUERY_ALERTS:
        source = (REPO_ROOT / rel_path).read_text()
        # Strip out the valid alias occurrences first so we're only
        # left checking for a BARE a.created_at reference, which would
        # be a real "column doesn't exist" SQL error at runtime.
        stripped = VALID_ALIAS_CREATED_AT.sub("", source)
        assert not BROKEN_CREATED_AT.search(stripped), (
            f"{rel_path} references alerts.created_at directly -- that column "
            f"does not exist (the real column is triggered_at). Alias it as "
            f"'a.triggered_at AS created_at' the way the rest of this codebase does."
        )


def test_no_raw_references_to_nonexistent_alerts_value_column():
    for rel_path in FILES_THAT_QUERY_ALERTS:
        source = (REPO_ROOT / rel_path).read_text()
        stripped = VALID_ALIAS_VALUE.sub("", source)
        assert not BROKEN_VALUE.search(stripped), (
            f"{rel_path} references alerts.value directly -- that column does "
            f"not exist (the real column is current_value). Alias it as "
            f"'a.current_value AS value' the way the rest of this codebase does."
        )


def test_correlate_and_rca_actually_use_triggered_at():
    """Positive check, not just an absence check -- confirms the fix is
    actually present, not just that the old bug-text is gone."""
    for rel_path in ["app/collector/correlate.py", "app/collector/rca.py"]:
        source = (REPO_ROOT / rel_path).read_text()
        assert "triggered_at" in source, f"{rel_path} should reference alerts.triggered_at somewhere"
