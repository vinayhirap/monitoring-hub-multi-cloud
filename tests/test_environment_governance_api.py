# tests/test_environment_governance_api.py
"""Workstream C routes: permission codes, account scope, validation, CSV safety. Database access is stubbed; the
real-database behaviour is in test_environment_governance_integration.py."""
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app  # noqa: F401
import app.auth  # noqa: F401
from tests.conftest import load_module, install_stub

EDITOR_ONE = {"id": 7, "username": "ed", "role": "editor"}


def _api(accessible=None):
    """accessible=None means every account; a set limits the caller to those accounts."""
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.audit", write_audit=lambda *a, **k: None)

    def require(code):
        def dep():
            return None
        dep.code = code
        return dep
    install_stub("app.auth.permissions", require_permission=require)
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: accessible)
    install_stub("app.collector.env_sync", NON_ALERTABLE_TYPES=("eni",), STALE_RESOURCE_HOURS=48,
                 sync_resource_environments=lambda **kw: {"applied": 0, "by_env": {}})
    install_stub("app.collector.tag_plan", DEFAULT_SOURCES=("manual", "name", "related"), build_tag_plan=lambda *a, **k: {})
    return load_module("app/api/environments.py")


class _Gov:
    """Records what the API asks the governance layer for, and fails the test if it is reached when it should not be."""
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def fn(*a, **k):
            self.calls.append((name, a, k))
            return self.returns.get(name)
        return fn

    returns = {}


def _with_gov(api, **returns):
    g = _Gov()
    g.returns = returns
    g.GAP_HEADER = ["account", "name"]
    g.RECENT_ONBOARD_DAYS = 30
    api.gov = g
    return g


def _http(fn, *a, **k):
    with pytest.raises(HTTPException) as e:
        fn(*a, **k)
    return e.value


# ---- permissions --------------------------------------------------------------------------------------

def test_every_environments_route_has_a_permission_and_reads_vs_writes_are_right():
    api = _api()
    codes = {}
    for route in api.router.routes:
        deps = [d.call for d in route.dependant.dependencies if hasattr(d.call, "code")]
        assert len(deps) == 1, f"{route.path} has no permission dependency"
        for method in route.methods:
            codes[(method, route.path)] = deps[0].code
    pre = "/api/environments"
    expect_view = [("GET", "/summary"), ("GET", "/resources"), ("GET", "/accounts/{account_id}/default/preview"),
                   ("GET", "/resources/{resource_pk}/history"), ("GET", "/compliance"), ("GET", "/compliance/trend"),
                   ("GET", "/compliance.csv"), ("GET", "/tagging-gaps.csv")]
    expect_manage = [("GET", "/tag-plan"), ("POST", "/classify"), ("PUT", "/accounts/{account_id}/default"),
                     ("GET", "/digest/preview")]
    for m, p in expect_view:
        assert codes[(m, pre + p)] == "environments.view", p
    for m, p in expect_manage:
        assert codes[(m, pre + p)] == "environments.manage", p


# ---- account default preview --------------------------------------------------------------------------

def test_an_editor_limited_to_one_account_cannot_preview_another_accounts_default():
    api = _api(accessible={1})
    g = _with_gov(api, preview_default={"x": 1})
    err = _http(api.preview_account_default, 2, env="prod", current_user=EDITOR_ONE)
    assert err.status_code == 403 and g.calls == []                       # refused before any query ran


def test_the_same_editor_cannot_change_another_accounts_default_either():
    api = _api(accessible={1})
    err = _http(api.set_account_default, 2, request=None, payload={"env": "prod"}, current_user=EDITOR_ONE)
    assert err.status_code == 403                                         # existing behaviour, pinned here


def test_preview_of_an_allowed_account_calls_the_dry_run_and_returns_it():
    api = _api(accessible={1})
    g = _with_gov(api, preview_default={"rows_rewritten": 4})
    assert api.preview_account_default(1, env="nonprod", current_user=EDITOR_ONE) == {"rows_rewritten": 4}
    assert g.calls == [("preview_default", (1, "nonprod"), {})]


@pytest.mark.parametrize("raw,expected", [(None, None), ("", None), ("none", None), ("CLEAR", None), (" NonProd ", "nonprod"), (" PROD ", "prod")])
def test_preview_env_values_match_the_put(raw, expected):
    api = _api()
    g = _with_gov(api, preview_default={"ok": True})
    api.preview_account_default(1, env=raw, current_user=EDITOR_ONE)
    assert g.calls[0][1] == (1, expected)


def test_preview_rejects_an_unknown_environment_and_a_missing_account():
    api = _api()
    _with_gov(api, preview_default={"ok": True})
    assert _http(api.preview_account_default, 1, env="staging", current_user=EDITOR_ONE).status_code == 400   # only prod / nonprod are accepted
    assert _http(api.preview_account_default, 1, env="uat", current_user=EDITOR_ONE).status_code == 400
    _with_gov(api, preview_default=None)
    assert _http(api.preview_account_default, 9, env="prod", current_user=EDITOR_ONE).status_code == 404


# ---- reads are scoped ---------------------------------------------------------------------------------

def test_history_passes_the_callers_accounts_down_and_404s_what_it_cannot_see():
    api = _api(accessible={2, 1})
    g = _with_gov(api, resource_history=None)
    assert _http(api.resource_history, 5, current_user=EDITOR_ONE).status_code == 404
    assert g.calls[0] == ("resource_history", (5, [1, 2]), {})


def test_unrestricted_callers_pass_none_meaning_every_account():
    api = _api(accessible=None)
    g = _with_gov(api, compliance_report={"totals": {}, "accounts": []})
    api.compliance(account_id=None, current_user={"id": 1, "username": "a", "role": "admin"})
    assert g.calls[0] == ("compliance_report", (None, None), {})


def test_an_empty_scope_is_passed_as_an_empty_list_not_as_everything():
    api = _api(accessible=set())
    g = _with_gov(api, compliance_report={"totals": {}, "accounts": []})
    api.compliance(account_id=None, current_user=EDITOR_ONE)
    assert g.calls[0][1][0] == []


@pytest.mark.parametrize("call", [
    lambda api, u: api.compliance(account_id=2, current_user=u),
    lambda api, u: api.compliance_trend(account_id=2, days=30, current_user=u),
    lambda api, u: api.compliance_csv(account_id=2, current_user=u),
    lambda api, u: api.tagging_gaps_csv(account_id=2, resource_type=None, essential_only=False, current_user=u),
])
def test_naming_an_account_outside_scope_is_a_403_on_every_report(call):
    api = _api(accessible={1})
    g = _with_gov(api)
    assert _http(call, api, EDITOR_ONE).status_code == 403 and g.calls == []


def test_trend_says_so_when_migration_099_is_missing():
    api = _api()

    class NoTable(Exception):
        errno = 1146

    def boom(*a, **k):
        raise NoTable("no such table")
    g = _with_gov(api)
    g.compliance_trend = boom
    assert api.compliance_trend(account_id=None, days=30, current_user=EDITOR_ONE) == {"points": [], "available": False}


# ---- CSV ----------------------------------------------------------------------------------------------

def test_tagging_gaps_csv_is_an_attachment_with_neutralised_cells_and_a_truncation_header():
    api = _api()
    _with_gov(api, tagging_gap_rows=([["=cmd()", "web"], ["A", "+1"]], True))
    r = api.tagging_gaps_csv(account_id=None, resource_type=None, essential_only=False, current_user=EDITOR_ONE)
    body = r.body.decode("utf-8")
    assert r.media_type.startswith("text/csv") and body.startswith("\ufeff")
    assert 'attachment; filename="tagging-gaps.csv"' == r.headers["content-disposition"]
    assert "'=cmd()" in body and "'+1" in body and "\n=cmd" not in body
    assert r.headers["x-rows-truncated"] == "true" and r.headers["cache-control"] == "no-store"


def test_gaps_csv_passes_the_filters_through():
    api = _api(accessible={1})
    g = _with_gov(api, tagging_gap_rows=([], False))
    api.tagging_gaps_csv(account_id=1, resource_type="ec2", essential_only=True, current_user=EDITOR_ONE)
    assert g.calls[0] == ("tagging_gap_rows", ([1], 1, "ec2", True), {})


# ---- validation ---------------------------------------------------------------------------------------

def test_classify_refuses_an_unknown_via_before_touching_anything():
    api = _api()
    err = _http(api.classify, request=None, payload={"env": "prod", "resource_ids": [1], "via": "magic"},
                current_user=EDITOR_ONE)
    assert err.status_code == 400 and "suggestion" in err.detail


def test_unknown_warning_kind_filter_is_a_400():
    api = _api()
    _with_gov(api)
    err = _http(api.list_resources, view="mismatches", env=None, source=None, tag_state=None, account_id=None,
                resource_type=None, region=None, q=None, warning_kind="made_up", suggest=False, limit=50, offset=0,
                current_user=EDITOR_ONE)
    assert err.status_code == 400


# ---- digest preview -----------------------------------------------------------------------------------

def test_digest_preview_shows_only_the_callers_accounts_and_never_addresses():
    api = _api(accessible={1})
    install_stub("app.env_digest", preview=lambda: {"enabled": False, "weekday": 0, "accounts": [
        {"account_id": 1, "account_name": "A", "recipient_count": 2}, {"account_id": 2, "account_name": "B", "recipient_count": 1}]})
    out = api.digest_preview(current_user=EDITOR_ONE)
    assert [a["account_id"] for a in out["accounts"]] == [1]
    assert "@" not in str(out)
