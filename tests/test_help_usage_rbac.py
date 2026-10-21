# tests/test_help_usage_rbac.py
"""
Phase E: help.view / usage.view / usage.manage -- migration seeds, route gates, deny overrides, 401, server-side section
filtering, and the nav/route wiring in the frontend.

The gate logic under test is the REAL app/auth/permissions.py (loaded by path); only the database-backed lookups are
replaced: get_role_permissions is fed from the codes the migration itself seeds (parsed out of the .sql file, so a
migration edit that changes who gets what fails here), and the RBAC resolver is a stub that returns scripted denials.
"""
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from conftest import load_module, install_stub  # noqa: E402

import pytest  # noqa: E402
from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "db/migrations/110_help_and_usage_permissions.sql"
CODES = {"help.view", "usage.view", "usage.manage"}

EXPECTED = {
    "viewer": {"help.view"},
    "editor": {"help.view", "usage.view"},
    "admin": {"help.view", "usage.view", "usage.manage"},
}


# ── migration ────────────────────────────────────────────────────────────────

def _sql():
    return MIGRATION.read_text()


def _legacy_grants():
    out = {}
    for role, codes in re.findall(
            r"INSERT IGNORE INTO role_permissions \(role, permission_id\)\s+SELECT '(\w+)', id FROM permissions WHERE code IN \(([^)]*)\)",
            _sql()):
        out.setdefault(role, set()).update(re.findall(r"'([a-z_.]+)'", codes))
    return out


def _v2_grants():
    out = {}
    for role, codes in re.findall(
            r"WHERE r\.role_key = '(\w+)' AND p\.code IN \(([^)]*)\)", _sql()):
        out.setdefault(role, set()).update(re.findall(r"'([a-z_.]+)'", codes))
    return out


def test_migration_registers_exactly_the_three_codes():
    block = re.search(r"INSERT IGNORE INTO permissions.*?;", _sql(), re.S).group(0)
    assert set(re.findall(r"\('([a-z_.]+)',\s*'", block)) == CODES
    assert block.count(", 0)") == 3                      # none is_internal: all appear in the permission matrix screen


def test_migration_grants_match_the_contract_in_both_tables():
    assert _legacy_grants() == EXPECTED, _legacy_grants()
    assert _v2_grants() == EXPECTED, _v2_grants()


def test_viewer_gets_no_usage_codes_and_only_admin_gets_manage():
    for table in (_legacy_grants(), _v2_grants()):
        assert not (table["viewer"] & {"usage.view", "usage.manage"})
        assert "usage.manage" not in table["editor"]
        assert "usage.manage" in table["admin"]


def test_migration_is_idempotent_and_non_destructive():
    sql = _sql()
    stmts = [s.strip() for s in re.sub(r"--[^\n]*", "", sql).split(";") if s.strip()]
    assert stmts and all(s.upper().startswith("INSERT IGNORE INTO") for s in stmts), [s[:40] for s in stmts]
    assert not re.search(r"\b(DELETE|DROP|TRUNCATE|UPDATE|ALTER)\b", re.sub(r"--[^\n]*", "", sql), re.I)


def test_no_semicolon_inside_a_string_literal():
    # migrate.py splits on every ';' after stripping '--' comments and has no string-literal handling.
    sys.path.insert(0, str(ROOT))
    spec = __import__("importlib.util").util.spec_from_file_location("migrate_under_test", ROOT / "migrate.py")
    mod = __import__("importlib.util").util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for stmt in mod._split_sql_statements(_sql()):
        assert stmt.count("'") % 2 == 0, stmt[:80]          # an odd quote count means a split landed inside a string
    assert len(mod._split_sql_statements(_sql())) == 7


def test_migration_number_is_unique_and_in_order():
    nums = [p.name.split("_", 1)[0] for p in (ROOT / "db/migrations").glob("*.sql")]
    assert nums.count("110") == 1


# ── route harness: real gate, scripted data ──────────────────────────────────

class _Denial:
    def __init__(self, code, scope=None):
        self.permission_code, self.scope = code, scope


@pytest.fixture()
def world():
    """Builds the real permissions module + help module on stubs. Returns (client_factory, state)."""
    state = {"denials": []}

    def get_current_user(request: Request):
        raw = request.headers.get("x-test-user")
        if not raw:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return json.loads(raw)

    install_stub("app.db", get_connection=lambda: (_ for _ in ()).throw(RuntimeError("no db in this test")))
    install_stub("app.auth.deps", get_current_user=get_current_user)
    install_stub("app.auth.rbac", resolve=lambda user: types.SimpleNamespace(denials=list(state["denials"])))
    perm = load_module("app/auth/permissions.py")
    perm.get_role_permissions = lambda role: set(EXPECTED.get(role, set())) | (
        {"users.view", "alerts.view"} if role == "admin" else set())
    install_stub("app.auth.permissions", **{k: getattr(perm, k) for k in (
        "get_role_permissions", "denied_permission_codes", "require_permission", "has_permission")})
    helpmod = load_module("app/api/help.py")

    def make(include_usage=True):
        app = FastAPI()
        app.include_router(helpmod.router)
        if include_usage:
            app.include_router(helpmod._placeholder,
                               dependencies=[Depends(perm.require_permission("usage.view"))])
        return TestClient(app)

    return types.SimpleNamespace(make=make, state=state, help=helpmod, perm=perm)


def _hdr(role, uid=7):
    return {"x-test-user": json.dumps({"id": uid, "username": role, "role": role})}


def _content(monkeypatch, sections):
    mod = types.ModuleType("app.help_content")
    mod.get_sections = lambda permissions, role: list(sections)           # deliberately does NOT filter
    monkeypatch.setitem(sys.modules, "app.help_content", mod)


# ── 401 / 403 / 200 per role ─────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/api/help/sections", "/api/usage/cloud-cost"])
def test_unauthenticated_is_401_not_403(world, path):
    assert world.make().get(path).status_code == 401


def test_viewer_can_open_help_but_not_cost(world, monkeypatch):
    _content(monkeypatch, [{"id": "overview", "requires": None}])
    c = world.make()
    assert c.get("/api/help/sections", headers=_hdr("viewer")).status_code == 200
    r = c.get("/api/usage/cloud-cost", headers=_hdr("viewer"))
    assert r.status_code == 403 and "usage.view" in r.json()["detail"]


def test_editor_passes_both_gates(world, monkeypatch):
    _content(monkeypatch, [{"id": "overview", "requires": None}])
    c = world.make()
    assert c.get("/api/help/sections", headers=_hdr("editor")).status_code == 200
    # the placeholder answers 501 only AFTER the gate has let the editor through
    assert c.get("/api/usage/cloud-cost", headers=_hdr("editor")).status_code == 501


def test_admin_passes_both_gates_even_if_codes_were_unseeded(world, monkeypatch):
    _content(monkeypatch, [{"id": "overview", "requires": None}])
    world.perm.get_role_permissions = lambda role: set()                 # admin is implicit
    c = world.make()
    assert c.get("/api/help/sections", headers=_hdr("admin")).status_code == 200
    assert c.get("/api/usage/cloud-cost", headers=_hdr("admin")).status_code == 501


def test_unknown_role_gets_nothing(world):
    c = world.make()
    assert c.get("/api/help/sections", headers=_hdr("contractor")).status_code == 403


# ── deny overrides ───────────────────────────────────────────────────────────

def test_unscoped_deny_on_help_view_removes_access(world, monkeypatch):
    _content(monkeypatch, [{"id": "overview", "requires": None}])
    c = world.make()
    assert c.get("/api/help/sections", headers=_hdr("viewer")).status_code == 200
    world.state["denials"] = [_Denial("help.view")]
    assert c.get("/api/help/sections", headers=_hdr("viewer")).status_code == 403


def test_unscoped_deny_on_usage_view_removes_access_from_editor(world):
    c = world.make()
    assert c.get("/api/usage/cloud-cost", headers=_hdr("editor")).status_code == 501
    world.state["denials"] = [_Denial("usage.view")]
    assert c.get("/api/usage/cloud-cost", headers=_hdr("editor")).status_code == 403


def test_deny_of_one_code_does_not_remove_the_other(world, monkeypatch):
    _content(monkeypatch, [{"id": "overview", "requires": None}])
    world.state["denials"] = [_Denial("usage.view")]
    assert world.make().get("/api/help/sections", headers=_hdr("editor")).status_code == 200


def test_scoped_deny_is_not_a_route_gate_by_design(world, monkeypatch):
    # app/auth/permissions.py::_globally_denied only enforces UNSCOPED denies at the route gate (scoped ones need
    # row-level enforcement in data endpoints). Help text and the cost summary have no per-row scope, so a scoped
    # deny leaves them reachable. This pins the existing rule rather than endorsing it.
    _content(monkeypatch, [{"id": "overview", "requires": None}])
    world.state["denials"] = [_Denial("help.view", scope=object())]
    assert world.make().get("/api/help/sections", headers=_hdr("viewer")).status_code == 200


def test_admin_cannot_be_locked_out_by_a_deny(world, monkeypatch):
    _content(monkeypatch, [{"id": "overview", "requires": None}])
    world.state["denials"] = [_Denial("help.view"), _Denial("usage.view")]
    c = world.make()
    assert c.get("/api/help/sections", headers=_hdr("admin")).status_code == 200
    assert c.get("/api/usage/cloud-cost", headers=_hdr("admin")).status_code == 501


# ── server-side section filtering ────────────────────────────────────────────

SECTIONS = [
    {"id": "overview", "requires": None},
    {"id": "alerts", "requires": "alerts.view"},
    {"id": "rbac", "requires": "users.view"},
    {"id": "cost", "requires": "usage.view"},
]


def test_sections_the_user_lacks_never_appear_even_if_content_does_not_filter(world, monkeypatch):
    _content(monkeypatch, SECTIONS)
    c = world.make()
    ids = lambda role: [s["id"] for s in c.get("/api/help/sections", headers=_hdr(role)).json()["sections"]]
    assert ids("viewer") == ["overview"]                      # viewer lacks alerts.view in this scripted world
    assert ids("editor") == ["overview", "cost"]
    assert ids("admin") == ["overview", "alerts", "rbac", "cost"]


def test_a_deny_override_also_removes_the_matching_sections(world, monkeypatch):
    _content(monkeypatch, SECTIONS)
    world.state["denials"] = [_Denial("usage.view")]
    got = [s["id"] for s in world.make().get("/api/help/sections", headers=_hdr("editor")).json()["sections"]]
    assert got == ["overview"]


def test_filter_sections_unit(world):
    f = world.help.filter_sections
    assert f(None, set()) == []
    assert [s["id"] for s in f(SECTIONS, {"alerts.view"})] == ["overview", "alerts"]
    assert [s["id"] for s in f([{"id": "x", "requires": ""}], set())] == ["x"]
    assert len(f(SECTIONS, set(), is_admin=True)) == 4


def test_response_shape_is_sections_key(world, monkeypatch):
    _content(monkeypatch, [])
    assert world.make().get("/api/help/sections", headers=_hdr("viewer")).json() == {"sections": []}


# ── graceful degradation until Phases D and F merge ──────────────────────────

def test_help_is_501_with_a_clear_message_when_content_package_is_absent(world, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.help_content", None)           # makes `from app.help_content import ...` fail
    r = world.make().get("/api/help/sections", headers=_hdr("viewer"))
    assert r.status_code == 501 and "not installed" in r.json()["detail"]


def test_a_broken_dependency_inside_the_content_package_is_not_masked_as_501(world, monkeypatch):
    mod = types.ModuleType("app.help_content")

    def boom(permissions, role):
        raise ModuleNotFoundError("No module named 'yaml'", name="yaml")
    mod.get_sections = boom
    monkeypatch.setitem(sys.modules, "app.help_content", mod)
    c = TestClient(world.make().app, raise_server_exceptions=False)
    assert c.get("/api/help/sections", headers=_hdr("viewer")).status_code == 500


def test_missing_module_detection_is_exact(world):
    m = world.help._missing
    assert m(ModuleNotFoundError(name="app.help_content"), "app.help_content")
    assert m(ModuleNotFoundError(name="app"), "app.help_content")
    assert not m(ModuleNotFoundError(name="yaml"), "app.help_content")
    assert not m(ModuleNotFoundError(name="app.help_content.sections"), "app.help_content")


def test_usage_router_falls_back_to_the_placeholder_and_prefers_phase_f(world, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.api.usage", None)
    assert world.help.usage_router() is world.help._placeholder
    real = types.SimpleNamespace(router=object())
    monkeypatch.setitem(sys.modules, "app.api.usage", real)
    assert world.help.usage_router() is real.router


# ── the whole app: routes and permissions as the matrix sees them ────────────

@pytest.fixture(scope="module")
def matrix():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/rbac_matrix.py"), "--json"],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-800:]
    return json.loads(proc.stdout)


def _row(matrix, path):
    return next(r for r in matrix if r["path"] == path)


def test_matrix_shows_both_routes_gated_by_their_permission(matrix):
    h = _row(matrix, "/api/help/sections")
    u = _row(matrix, "/api/usage/cloud-cost")
    assert (h["level"], h["permissions"], h["methods"]) == ("permission", ["help.view"], ["GET"])
    assert (u["level"], u["permissions"], u["methods"]) == ("permission", ["usage.view"], ["GET"])


def test_nothing_under_help_or_usage_is_open_or_state_changing(matrix):
    rows = [r for r in matrix if r["path"].startswith(("/api/help", "/api/usage"))]
    assert rows and all(r["level"] == "permission" and not r["mutating"] for r in rows)


def test_every_code_those_routes_name_is_seeded(matrix):
    named = {c for r in matrix if r["path"].startswith(("/api/help", "/api/usage")) for c in r["permissions"]}
    assert named <= set(re.findall(r"\('([a-z_.]+)',\s*'", re.search(r"INSERT IGNORE INTO permissions.*?;", _sql(), re.S).group(0)))


# ── frontend wiring (static: no browser here) ────────────────────────────────

LAYOUT = (ROOT / "frontend/src/components/Layout.jsx").read_text()
APP = (ROOT / "frontend/src/App.jsx").read_text()


def _nav(to):
    m = re.search(r'\{ group: "([^"]+)", to: "%s",[^\n]*' % re.escape(to), LAYOUT)
    assert m, to
    return m.group(0), m.group(1)


def test_help_nav_is_visible_to_every_role_and_gated_by_help_view():
    line, group = _nav("/help")
    assert 'roles: ["admin","editor","viewer"]' in line and 'perm: "help.view"' in line
    assert group in re.search(r"const GROUP_ORDER = \[([^\]]*)\]", LAYOUT).group(1)


def test_api_cost_nav_is_under_administration_for_admin_and_editor_only():
    line, group = _nav("/api-cost")
    assert group == "Administration"
    assert 'roles: ["admin","editor"]' in line and "viewer" not in line and 'perm: "usage.view"' in line


def test_route_access_mirrors_the_nav_rules():
    assert re.search(r'"help":\s*\{ roles: \["admin","editor","viewer"\], perm: "help.view" \}', APP)
    assert re.search(r'"api-cost":\s*\{ roles: \["admin","editor"\],\s*perm: "usage.view" \}', APP)


def test_routes_exist_and_are_wrapped_in_require_access():
    assert '<Route path="help"' in APP and '<RequireAccess path="help"><HelpCenter />' in APP
    assert '<Route path="api-cost"' in APP and '<RequireAccess path="api-cost"><ApiCost />' in APP


def test_nav_icons_are_defined_and_distinct():
    for name in ("HelpIcon", "CostIcon"):
        assert re.search(r"function %s\(\)" % name, LAYOUT), name
    assert re.search(r"function HelpIcon\(\).*", LAYOUT).group(0) != re.search(r"function CostIcon\(\).*", LAYOUT).group(0)
