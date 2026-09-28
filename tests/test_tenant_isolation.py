# tests/test_tenant_isolation.py
"""
Guards recurring bug class #1: cross-account data bleed -- an endpoint that
takes an account / alert / resource / report id but never checks it against
the caller's scope lets one tenant read or mutate another's data.

Two layers, both stub-free:

1. STATIC RATCHET. Every route handler under app/api/ that accepts an
   identifier parameter (account_id, alert_id, resource_id, ...) must show
   evidence of a scope check: it either calls a scope-shaped helper
   (name contains access/scope/accessible/visible/manageable) or hands
   `current_user` to a helper that can apply one. A handler with neither is
   almost certainly reading by bare id. This is a heuristic (a helper that
   ignores current_user would fool it) so it is a *floor*, not a proof; the
   behavioral tests below pin down the helpers everything else relies on.
   It is a ratchet: new offenders fail, and fixing an allowlisted one fails
   until it is removed from _KNOWN_UNSCOPED.

2. BEHAVIORAL CONTRACT of the three scope-check helpers that most routes
   delegate to (live_data._check_account_scope, metric_catalog.
   _require_account_access, alerts._require_alert_access). They are
   extracted from the real source with ast and executed against an injected
   scope resolver, so the tests run the actual code text without importing
   boto3/FastAPI apps or standing up a DB. Contract:
       scope None        -> unrestricted (admin)          -> allowed
       id in scope set   -> allowed
       id NOT in set     -> 403
       EMPTY set         -> 403   (deny by default: zero scopes sees nothing)
"""
import ast
import pathlib
import re

import pytest
from fastapi import HTTPException

ROOT = pathlib.Path(__file__).resolve().parent.parent

_ID_PARAMS = {"account_id", "account_db_id", "alert_id", "resource_id",
              "report_id", "threshold_id", "incident_id", "check_id", "slo_id"}
_SCOPE_NAME = re.compile(r"(access|scope|accessible|_visible|manageable)", re.I)
_HTTP_VERBS = {"get", "post", "put", "patch", "delete"}

# "path::function" -> reason it is exempt today.
_KNOWN_UNSCOPED = {
    "app/api/admin/accounts.py::delete_account": (
        "Guarded only by the accounts.delete permission, which migration 049 "
        "grants to admin alone, so today it is only reachable by an "
        "unrestricted principal. But it takes an account id and performs no "
        "scope check: the moment accounts.delete is delegated to a role with a "
        "scoped binding (which RBAC v2 allows), that user could deactivate an "
        "account outside their scope. Fix: call get_accessible_account_ids() "
        "and 403 on non-membership, as list/get/discover_account already do."
    ),
}


def _is_route(fn):
    for d in fn.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        if isinstance(target, ast.Attribute) and target.attr in _HTTP_VERBS:
            return True
    return False


def _scope_aware(fn):
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
        if _SCOPE_NAME.search(name):
            return True
        for arg in list(node.args) + [k.value for k in node.keywords]:
            if isinstance(arg, ast.Name) and arg.id == "current_user":
                return True
    return False


def _unscoped_routes():
    found = set()
    for path in sorted((ROOT / "app" / "api").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _is_route(fn):
                continue
            params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            if params & _ID_PARAMS and not _scope_aware(fn):
                found.add(f"{rel}::{fn.name}")
    return found


def test_route_scanner_sees_a_realistic_number_of_id_taking_routes():
    """Guards the guard against the scanner silently matching nothing."""
    total = 0
    for path in (ROOT / "app" / "api").rglob("*.py"):
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_route(fn):
                if {a.arg for a in fn.args.args} & _ID_PARAMS:
                    total += 1
    assert total >= 40, f"only {total} id-taking routes found -- scanner likely broken"


def test_no_new_id_taking_route_lacks_a_scope_check():
    new = sorted(_unscoped_routes() - set(_KNOWN_UNSCOPED))
    assert not new, (
        "Route handler(s) take an account/alert/resource/report id but show no "
        "scope check (call get_accessible_account_ids()/a *_access/*_scope "
        "helper, or pass current_user to one):\n  " + "\n  ".join(new)
    )


def test_known_unscoped_allowlist_has_no_stale_entries():
    stale = sorted(set(_KNOWN_UNSCOPED) - _unscoped_routes())
    assert not stale, f"Now scoped (or removed) -- delete from _KNOWN_UNSCOPED: {stale}"


# ── behavioral contract of the scope helpers ────────────────────────

def _extract(relpath, func_name, resolver):
    """Compile ONE function out of a real source file and bind it to an
    injected scope resolver (standing in for get_accessible_account_ids)."""
    src = (ROOT / relpath).read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == func_name)
    module = ast.Module(body=[node], type_ignores=[])
    ns = {"HTTPException": HTTPException, "get_accessible_account_ids": resolver}
    exec(compile(module, relpath, "exec"), ns)
    return ns[func_name], ns


def _account_checkers(resolver):
    live, _ = _extract("app/api/live_data.py", "_check_account_scope", resolver)
    meta, _ = _extract("app/api/metric_catalog.py", "_require_account_access", resolver)
    return {"live_data._check_account_scope": live,
            "metric_catalog._require_account_access": meta}


@pytest.mark.parametrize("name", ["live_data._check_account_scope",
                                  "metric_catalog._require_account_access"])
def test_unrestricted_principal_is_allowed(name):
    check = _account_checkers(lambda user: None)[name]
    result = check({"id": 1}, 999) if name.startswith("live") else check(999, {"id": 1})
    assert result is None  # allowed == returns quietly, raises nothing


@pytest.mark.parametrize("name", ["live_data._check_account_scope",
                                  "metric_catalog._require_account_access"])
def test_account_inside_scope_is_allowed(name):
    check = _account_checkers(lambda user: {7, 8})[name]
    result = check({"id": 1}, 7) if name.startswith("live") else check(7, {"id": 1})
    assert result is None  # allowed == returns quietly, raises nothing


@pytest.mark.parametrize("name", ["live_data._check_account_scope",
                                  "metric_catalog._require_account_access"])
def test_account_outside_scope_is_forbidden(name):
    check = _account_checkers(lambda user: {7, 8})[name]
    with pytest.raises(HTTPException) as exc:
        check({"id": 1}, 9) if name.startswith("live") else check(9, {"id": 1})
    assert exc.value.status_code == 403


@pytest.mark.parametrize("name", ["live_data._check_account_scope",
                                  "metric_catalog._require_account_access"])
def test_empty_scope_denies_everything_not_allows_everything(name):
    """The classic inversion bug: treating an EMPTY set like None (falsy ->
    'no restriction') would hand a zero-scope user the whole fleet."""
    check = _account_checkers(lambda user: set())[name]
    with pytest.raises(HTTPException) as exc:
        check({"id": 1}, 7) if name.startswith("live") else check(7, {"id": 1})
    assert exc.value.status_code == 403


def _alert_checker(resolver, account_of):
    fn, ns = _extract("app/api/alerts.py", "_require_alert_access", resolver)
    ns["_get_alert_account_id"] = account_of
    return fn


def test_alert_access_unknown_alert_is_404_even_for_unrestricted_caller():
    check = _alert_checker(lambda u: None, lambda alert_id: None)
    with pytest.raises(HTTPException) as exc:
        check(123, {"id": 1})
    assert exc.value.status_code == 404


def test_alert_access_returns_account_id_when_in_scope():
    check = _alert_checker(lambda u: {7}, lambda alert_id: 7)
    assert check(123, {"id": 1}) == 7


def test_alert_access_other_accounts_alert_is_forbidden():
    check = _alert_checker(lambda u: {7}, lambda alert_id: 9)
    with pytest.raises(HTTPException) as exc:
        check(123, {"id": 1})
    assert exc.value.status_code == 403


def test_alert_access_empty_scope_is_forbidden():
    check = _alert_checker(lambda u: set(), lambda alert_id: 7)
    with pytest.raises(HTTPException) as exc:
        check(123, {"id": 1})
    assert exc.value.status_code == 403
