"""Phase 4b patch 1: priority rule, flags, and the set-based refresh (fake cursor, no database)."""
import importlib.util
import os
import pytest

_spec = importlib.util.spec_from_file_location(
    "priority_under_test", os.path.join(os.path.dirname(__file__), "..", "app", "priority.py"))
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)


@pytest.mark.parametrize("env,sev,expected", [
    ("prod", "CRITICAL", 1), ("prod", "WARNING", 2),
    ("uat", "CRITICAL", 2), ("uat", "INFO", 3), ("uat", "WARNING", 3),
    ("dev", "CRITICAL", 3), ("dev", "INFO", 4),
])
def test_matrix(env, sev, expected):
    assert pr.compute_priority(env, sev) == expected


@pytest.mark.parametrize("env", ["unknown", "ignore", "", None, "staging", "Production", "PROD"])
def test_unrecognised_environment_is_ranked_like_prod(env):
    assert pr.compute_priority(env, "CRITICAL") == 1
    assert pr.compute_priority(env, "WARNING") == 2


def test_severity_is_case_insensitive_and_missing_is_not_critical():
    assert pr.compute_priority("prod", "critical") == 1
    assert pr.compute_priority("prod", None) == 2


def test_criticality_tag_moves_up_one_level_but_not_above_p1():
    assert pr.compute_priority("dev", "INFO", {"Criticality": "critical"}) == 3
    assert pr.compute_priority("uat", "CRITICAL", '{"criticality": " Critical "}') == 1
    assert pr.compute_priority("prod", "CRITICAL", {"Criticality": "critical"}) == 1


@pytest.mark.parametrize("tags", [None, "", "null", "[]", "{bad", 5, {"Criticality": "low"}, {"Criticality": None}])
def test_bad_or_other_tags_never_raise_or_bump(tags):
    assert pr.compute_priority("dev", "INFO", tags) == 4


def test_flags(monkeypatch):
    monkeypatch.delenv("PRIORITY_ENABLED", raising=False)
    monkeypatch.delenv("PRIORITY_ROUTING_ENABLED", raising=False)
    assert not pr.enabled() and not pr.routing_enabled()
    monkeypatch.setenv("PRIORITY_ROUTING_ENABLED", "true")
    assert not pr.routing_enabled()            # routing alone is not enough
    monkeypatch.setenv("PRIORITY_ENABLED", "true")
    assert pr.enabled() and pr.routing_enabled()


class FakeCursor:
    def __init__(self, rows):
        self.rows, self.updates, self.sql = rows, [], None
        self.select_sql = None

    def execute(self, sql, params=None):
        self.sql = sql
        if sql.strip().upper().startswith("UPDATE"):
            self.updates.append(params)
        else:
            self.select_sql = sql

    def fetchall(self):
        return self.rows


def _rows():
    return [
        {"id": 1, "severity": "CRITICAL", "environment": "prod", "priority": None, "tags": None},
        {"id": 2, "severity": "INFO", "environment": "dev", "priority": 4, "tags": "{}"},          # already right
        {"id": 3, "severity": "INFO", "environment": "dev", "priority": 4, "tags": '{"Criticality":"critical"}'},
    ]


def test_refresh_writes_only_what_differs():
    cur = FakeCursor(_rows())
    res = pr.refresh_open_alert_priorities(cur)
    assert res["checked"] == 3 and res["changed"] == 2
    assert sorted(cur.updates) == [(1, 1), (3, 3)]
    assert res["by_priority"] == {1: 1, 2: 0, 3: 1, 4: 1}
    assert "env_resolved" not in cur.select_sql


def test_dry_run_writes_nothing():
    cur = FakeCursor(_rows())
    res = pr.refresh_open_alert_priorities(cur, apply=False)
    assert res["changed"] == 2 and cur.updates == []


def test_reclassified_resource_moves_its_alert():
    rows = [{"id": 9, "severity": "CRITICAL", "environment": "prod", "priority": 1,
             "tags": None, "env_resolved": "dev"}]
    cur = FakeCursor(rows)
    res = pr.refresh_open_alert_priorities(cur, use_resolved_env=True)
    assert res["changes"] == [(9, 1, 3)] and "env_resolved" in cur.select_sql
