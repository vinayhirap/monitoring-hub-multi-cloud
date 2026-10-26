"""Cost-jump alert settings: validation, resolution order, save SQL, and the API helpers. No database, no network."""
import math

import pytest

import app  # noqa: F401
from app import cost_jump_settings as cjs


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("COST_JUMP_ALERT_ENABLED", "COST_JUMP_THRESHOLD_PCT", "COST_JUMP_MIN_INCREASE_USD"):
        monkeypatch.delenv(v, raising=False)


# ── validation ──────────────────────────────────────────────────────────────────
def test_valid_values_are_cleaned_and_null_means_inherit():
    assert cjs.validate({"enabled": True, "threshold_pct": 45, "min_increase_usd": 0.5}) == {"enabled": True, "threshold_pct": 45.0, "min_increase_usd": 0.5}
    assert cjs.validate({"enabled": None, "threshold_pct": None}) == {"enabled": None, "threshold_pct": None}


@pytest.mark.parametrize("bad", [
    {}, {"enabled": "yes"}, {"enabled": 1}, {"threshold_pct": "30"}, {"threshold_pct": True}, {"threshold_pct": 0},
    {"threshold_pct": 5000}, {"threshold_pct": math.nan}, {"min_increase_usd": -1}, {"min_increase_usd": math.inf}, [], "x",
])
def test_bad_values_are_rejected_with_a_message(bad):
    with pytest.raises(ValueError) as e:
        cjs.validate(bad)
    assert str(e.value)


def test_unknown_fields_are_ignored_not_saved():
    assert cjs.validate({"threshold_pct": 40, "scope_key": 7, "drop": "table"}) == {"threshold_pct": 40.0}


# ── resolution order ────────────────────────────────────────────────────────────
def test_builtin_default_is_off_30_and_ten_cents():
    assert cjs.effective({}) == {"enabled": False, "threshold_pct": 30.0, "min_increase_usd": 0.10}
    assert {f: s for f, (_v, s) in cjs.resolve({}).items()} == {f: "default" for f in cjs.FIELDS}


def test_environment_beats_builtin_and_bad_environment_values_are_ignored(monkeypatch):
    monkeypatch.setenv("COST_JUMP_ALERT_ENABLED", "true")
    monkeypatch.setenv("COST_JUMP_THRESHOLD_PCT", "banana")
    monkeypatch.setenv("COST_JUMP_MIN_INCREASE_USD", "-3")
    r = cjs.resolve({})
    assert r["enabled"] == (True, "environment") and r["threshold_pct"] == (30.0, "default") and r["min_increase_usd"] == (0.10, "default")


def test_all_accounts_beats_environment_and_account_beats_all_accounts(monkeypatch):
    monkeypatch.setenv("COST_JUMP_THRESHOLD_PCT", "40")
    st = {"global": {"enabled": True, "threshold_pct": 50.0, "min_increase_usd": None},
          "accounts": {7: {"enabled": False, "threshold_pct": None, "min_increase_usd": 2.0}}}
    assert cjs.resolve(st) ["threshold_pct"] == (50.0, "all accounts")
    r7 = cjs.resolve(st, 7)
    assert r7["enabled"] == (False, "account") and r7["threshold_pct"] == (50.0, "all accounts") and r7["min_increase_usd"] == (2.0, "account")
    assert cjs.resolve(st, 8)["enabled"] == (True, "all accounts")           # an account with no row inherits
    assert cjs.resolve(st, 7)["min_increase_usd"][1] == "account"


def test_an_explicit_off_is_not_confused_with_not_set():
    st = {"global": {"enabled": True}, "accounts": {7: {"enabled": False}}}
    assert cjs.effective(st, 7)["enabled"] is False


# ── storage ─────────────────────────────────────────────────────────────────────
class Cur:
    def __init__(self, rows=None, fail=None):
        self.log, self._rows, self._fail = [], rows or [], fail

    def execute(self, sql, params=()):
        if self._fail:
            raise Exception(self._fail)
        self.log.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._rows


def test_save_writes_only_the_fields_given_and_records_who():
    c = Cur()
    cjs.save(c, 7, {"threshold_pct": 45.0}, "vinay")
    sql, params = c.log[0]
    assert "threshold_pct = VALUES(threshold_pct)" in sql and "enabled" not in sql and "min_increase_usd" not in sql
    assert params == (7, 45.0, "vinay")


def test_an_account_row_with_nothing_set_is_removed_but_the_all_accounts_row_is_kept():
    c = Cur()
    cjs.save(c, 7, {"enabled": None}, "v")
    assert any("DELETE FROM cost_jump_settings WHERE scope_key" in s for s, _ in c.log)
    g = Cur()
    cjs.save(g, 0, {"enabled": None}, "v")
    assert not any("DELETE" in s for s, _ in g.log)


def test_save_with_no_fields_does_nothing():
    c = Cur()
    cjs.save(c, 7, {}, "v")
    assert c.log == []


def test_load_splits_all_accounts_from_per_account_and_tolerates_a_missing_table():
    rows = [{"scope_key": 0, "enabled": 1, "threshold_pct": 35, "min_increase_usd": None, "updated_by": "a", "updated_at": None},
            {"scope_key": 7, "enabled": 0, "threshold_pct": None, "min_increase_usd": 2, "updated_by": "b", "updated_at": None}]
    st = cjs.load(Cur(rows))
    assert st["global"]["enabled"] is True and st["global"]["threshold_pct"] == 35.0
    assert st["accounts"][7]["enabled"] is False and st["accounts"][7]["min_increase_usd"] == 2.0 and st["table_missing"] is False
    missing = cjs.load(Cur(fail="1146 (42S02): Table 'x.cost_jump_settings' doesn't exist"))
    assert missing == {"global": {}, "accounts": {}, "table_missing": True, "recipients_missing": True}
    with pytest.raises(Exception):
        cjs.load(Cur(fail="connection lost"))                                # anything else is a real error, not hidden


# ── API helpers ─────────────────────────────────────────────────────────────────
@pytest.fixture
def api(monkeypatch):
    import sys
    import types
    from tests.conftest import load_module

    def stub(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, m)

    stub("app.db", get_connection=lambda: None)
    stub("app.audit", write_audit=lambda *a, **k: None)
    stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    stub("app.auth.authorization", get_accessible_account_ids=lambda u: None)
    return load_module("app/api/cost_alert_settings.py")


def test_plan_targets_all_or_a_checked_list(api):
    assert api.plan_targets("all", [3, 1, 2]) == [1, 2, 3]
    assert api.plan_targets([2, 2, 1], [1, 2, 3]) == [1, 2]
    for bad in ([], None, "everything", [1.5], ["1"], [True]):
        with pytest.raises(ValueError):
            api.plan_targets(bad, [1, 2, 3])


def test_plan_targets_refuses_accounts_the_caller_cannot_see(api):
    with pytest.raises(ValueError) as e:
        api.plan_targets([1, 99], [1, 2])
    assert "99" in str(e.value)


def test_view_shows_set_and_effective_values_and_where_each_comes_from(api):
    st = {"global": {"enabled": True, "threshold_pct": 40.0, "min_increase_usd": None, "updated_by": "v", "updated_at": None},
          "accounts": {7: {"enabled": False, "threshold_pct": None, "min_increase_usd": None, "updated_by": "w", "updated_at": None}},
          "table_missing": False}
    v = api.build_view(st, [{"id": 7, "account_name": "Prod", "provider": "aws"}, {"id": 8, "account_name": "Dev", "provider": "aws"}], {8})
    a7, a8 = v["accounts"]
    assert a7["has_override"] and a7["effective"]["enabled"] is False and a7["source"]["enabled"] == "account" and a7["source"]["threshold_pct"] == "all accounts"
    assert not a8["has_override"] and a8["effective"]["enabled"] is True and a8["open_alert"] is True and a7["open_alert"] is False
    assert v["all_accounts"]["set"]["threshold_pct"] == 40.0 and v["limits"]["threshold_pct"] == [1.0, 1000.0]


def test_the_settings_routes_all_require_usage_manage():
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "api", "cost_alert_settings.py")).read()
    assert src.count('Depends(require_permission("usage.manage"))') == 4
    assert src.count("@router.") == 4


# ── recipients ──────────────────────────────────────────────────────────────────
def test_recipient_validation_cleans_dedupes_and_lowercases():
    v = cjs.validate({"notify_user_ids": [5, 3, 5], "notify_emails": " Ops@Corp.com, ops@corp.com ;lead@corp.com\nx@y.io "})
    assert v == {"notify_user_ids": [3, 5], "notify_emails": ["ops@corp.com", "lead@corp.com", "x@y.io"]}
    assert cjs.validate({"notify_user_ids": [], "notify_emails": ""}) == {"notify_user_ids": None, "notify_emails": None}
    assert cjs.validate({"notify_emails": ["a@b.co"]}) == {"notify_emails": ["a@b.co"]}


@pytest.mark.parametrize("bad", [
    {"notify_user_ids": ["3"]}, {"notify_user_ids": [True]}, {"notify_user_ids": [0]}, {"notify_user_ids": "3"},
    {"notify_user_ids": list(range(1, 60))}, {"notify_emails": "not-an-email"}, {"notify_emails": ["a@b.co", "bad"]},
    {"notify_emails": ["a@b.co\r\nBcc: evil@x.io"]}, {"notify_emails": [f"u{i}@corp.com" for i in range(25)]}, {"notify_emails": [5]},
])
def test_bad_recipients_are_rejected(bad):
    with pytest.raises(ValueError):
        cjs.validate(bad)


def test_recipients_are_added_together_not_inherited():
    st = {"global": {"notify_user_ids": [1], "notify_emails": ["all@corp.com"]},
          "accounts": {7: {"notify_user_ids": [2], "notify_emails": ["acct@corp.com", "ALL@corp.com"]}}}
    assert cjs.recipients(st, 7) == ([1, 2], ["all@corp.com", "acct@corp.com"])
    assert cjs.recipients(st, 8) == ([1], ["all@corp.com"])                  # an account with no row still gets the all-accounts ones
    assert cjs.recipients({}, 7) == ([], [])


def test_recipients_are_stored_as_json_and_text_and_clearing_one_field_keeps_the_row():
    c = Cur()
    cjs.save(c, 7, {"notify_user_ids": [3, 5], "notify_emails": ["a@b.co", "c@d.io"]}, "v")
    assert c.log[0][1] == (7, "[3, 5]", "a@b.co, c@d.io", "v")
    c2 = Cur()
    cjs.save(c2, 7, {"enabled": None}, "v")
    delete_sql = [s for s, _ in c2.log if s.startswith("DELETE")][0]
    assert "notify_user_ids IS NULL" in delete_sql and "notify_emails IS NULL" in delete_sql     # recipients keep the row alive


def test_before_migration_133_nothing_references_the_recipient_columns():
    c = Cur()
    cjs.save(c, 7, {"enabled": None}, "v", recipients_supported=False)
    assert not any("notify_" in s for s, _ in c.log)
    with pytest.raises(ValueError):
        cjs.save(Cur(), 7, {"notify_emails": ["a@b.co"]}, "v", recipients_supported=False)


class FallbackCur(Cur):
    """Database still on migration 132: the first SELECT (with recipient columns) fails with 1054, the older one works."""
    def __init__(self):
        super().__init__([{"scope_key": 0, "enabled": 1, "threshold_pct": None, "min_increase_usd": None, "updated_by": "a", "updated_at": None}])

    def execute(self, sql, params=()):
        if "notify_user_ids" in sql:
            raise Exception("1054 (42S22): Unknown column 'notify_user_ids' in 'field list'")
        super().execute(sql, params)


def test_load_works_before_migration_133_and_says_so():
    st = cjs.load(FallbackCur())
    assert st["recipients_missing"] is True and st["global"]["enabled"] is True and st["global"]["notify_emails"] == []


def test_load_reads_recipient_columns_when_present():
    st = cjs.load(Cur([{"scope_key": 0, "enabled": None, "threshold_pct": None, "min_increase_usd": None,
                        "notify_user_ids": "[2, 9]", "notify_emails": "a@b.co, c@d.io", "updated_by": "a", "updated_at": None}]))
    assert st["global"]["notify_user_ids"] == [2, 9] and st["global"]["notify_emails"] == ["a@b.co", "c@d.io"]
    assert st["recipients_missing"] is False
    bad = cjs.load(Cur([{"scope_key": 0, "notify_user_ids": "{broken", "notify_emails": None, "updated_by": None, "updated_at": None}]))
    assert bad["global"]["notify_user_ids"] == []                            # a corrupt value never breaks the alert run


def test_view_lists_users_marks_those_without_email_and_counts_recipients(api):
    st = {"global": {"notify_user_ids": [1], "notify_emails": ["all@corp.com"]},
          "accounts": {7: {"notify_user_ids": [], "notify_emails": ["a@corp.com"]}}, "table_missing": False, "recipients_missing": False}
    users = [{"id": 1, "username": "asha", "email": "asha@corp.com"}, {"id": 2, "username": "ravi", "email": None}]
    v = api.build_view(st, [{"id": 7, "account_name": "Prod", "provider": "aws"}, {"id": 8, "account_name": "Dev", "provider": "aws"}], set(), users, True)
    assert [(u["username"], u["has_email"]) for u in v["users"]] == [("asha", True), ("ravi", False)]
    a7, a8 = v["accounts"]
    assert a7["recipient_count"] == 3 and a8["recipient_count"] == 2 and a7["has_override"] is True and a8["has_override"] is False
    assert v["smtp_configured"] is True and v["all_accounts"]["set"]["notify_emails"] == ["all@corp.com"]


def test_choosing_a_user_without_email_is_refused_with_the_way_out(api):
    from fastapi import HTTPException

    class C:
        def __init__(self, rows): self.rows = rows
        def execute(self, sql, params=()): pass
        def fetchall(self): return self.rows

    api._check_users(C([{"id": 1, "username": "asha", "email": "asha@corp.com", "active": 1}]), [1])
    for rows, text in (([{"id": 1, "username": "ravi", "email": "", "active": 1}], "no email on file"),
                       ([{"id": 1, "username": "ravi", "email": "r@c.com", "active": 0}], "inactive"), ([], "does not exist")):
        with pytest.raises(HTTPException) as e:
            api._check_users(C(rows), [1])
        assert text in e.value.detail
    with pytest.raises(HTTPException) as e:
        api._check_users(C([{"id": 1, "username": "ravi", "email": " ", "active": 1}]), [1])
    assert "type the address in directly" in e.value.detail
