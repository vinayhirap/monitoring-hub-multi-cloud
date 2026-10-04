# tests/test_audit_p13_rbac_matrix.py
"""
Audit E5: the authorization surface is generated from the code and pinned, so a new route cannot quietly be public or
open to every signed-in user.

If this fails because you added a route on purpose: update the allowlist below in the same commit, and regenerate the
document with   python3 scripts/rbac_matrix.py --write docs/RBAC_MATRIX.md

The matrix is built in a SUBPROCESS: it imports the whole app with the database module stubbed, which must not leak into
the other tests' module state.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Routes that are deliberately reachable without a session.
PUBLIC_ALLOWLIST = {
    ("GET", "/"),                                   # service banner
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),                   # only clears the cookie
    ("POST", "/api/auth/forgot-password"),          # enumeration-safe, rate-limited
    ("POST", "/api/auth/reset-password"),           # needs a valid one-time token
    ("GET", "/api/auth/sso/login"),
    ("POST", "/api/auth/sso/acs"),                  # SAML assertion consumer (validated inside)
    ("GET", "/api/auth/sso/metadata"),
    ("GET", "/api/health/live"),
    ("GET", "/api/health/ready"),
    ("GET", "/api/status-page"),                    # the public status page, by design
    ("POST", "/api/webhooks/deploy"),               # authenticates with its own bearer token
}

# State-changing routes open to ANY signed-in user (no specific permission). Keep this list tiny.
AUTHENTICATED_MUTATIONS_ALLOWLIST = {
    ("POST", "/api/auth/change-password"),          # changes the caller's own password (old password required)
}

# Hand-written guard functions: each is reviewed individually.
CUSTOM_GUARDS_ALLOWLIST = {
    ("GET", "/api/status-page/components"): "_require_view_or_manage",   # status_page.view OR status_page.manage
}


@pytest.fixture(scope="module")
def matrix():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/rbac_matrix.py"), "--json"],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-800:]
    return json.loads(proc.stdout)


def _pairs(matrix, **where):
    out = set()
    for r in matrix:
        if all(r[k] == v for k, v in where.items()):
            out.update((m, r["path"]) for m in r["methods"])
    return out


def test_the_matrix_covers_the_whole_app(matrix):
    assert len(matrix) > 150
    assert {r["level"] for r in matrix} <= {"public", "authenticated", "permission", "role", "custom"}


def test_only_the_expected_routes_are_public(matrix):
    public = _pairs(matrix, level="public")
    assert public == PUBLIC_ALLOWLIST, (
        f"unexpected public: {sorted(public - PUBLIC_ALLOWLIST)}  /  no longer public: {sorted(PUBLIC_ALLOWLIST - public)}")


def test_no_state_changing_route_is_open_to_every_signed_in_user_without_review(matrix):
    open_mutations = {(m, r["path"]) for r in matrix if r["level"] == "authenticated" for m in r["methods"]
                      if m in {"POST", "PUT", "PATCH", "DELETE"}}
    assert open_mutations == AUTHENTICATED_MUTATIONS_ALLOWLIST, sorted(open_mutations ^ AUTHENTICATED_MUTATIONS_ALLOWLIST)


def test_custom_guards_are_all_known(matrix):
    found = {(m, r["path"]): r["custom"][0] for r in matrix if r["level"] == "custom" for m in r["methods"]}
    assert found == CUSTOM_GUARDS_ALLOWLIST


def test_every_mutating_route_has_some_protection_beyond_nothing(matrix):
    for r in matrix:
        if r["mutating"] and r["level"] == "public":
            assert any((m, r["path"]) in PUBLIC_ALLOWLIST for m in r["methods"]), r["path"]


def test_ws_status_is_no_longer_an_unauthenticated_connection_counter(matrix):
    row = next(r for r in matrix if r["path"] == "/ws/status")
    assert row["level"] == "permission" and row["permissions"] == ["operations.view"]


def test_the_checked_in_matrix_document_is_current():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/rbac_matrix.py")], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-800:]
    committed = (ROOT / "docs/RBAC_MATRIX.md").read_text().strip()
    assert committed == proc.stdout.strip(), "docs/RBAC_MATRIX.md is stale: python3 scripts/rbac_matrix.py --write docs/RBAC_MATRIX.md"


# ── the runtime smoke script's decision logic (no server needed) ─────────────

def _smoke():
    import importlib.util
    spec = importlib.util.spec_from_file_location("rbac_smoke", ROOT / "scripts/rbac_smoke.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "scripts"))
    spec.loader.exec_module(mod)
    return mod


def test_smoke_expectations_follow_the_guards():
    f = _smoke().expected_allowed
    perm = {"level": "permission", "permissions": ["alerts.view"]}
    two = {"level": "permission", "permissions": ["a.x", "b.y"]}
    assert f(perm, {"alerts.view"}, False) is True
    assert f(perm, {"reports.view"}, False) is False
    assert f(two, {"a.x"}, False) is False and f(two, {"a.x", "b.y"}, False) is True      # ALL codes are required
    assert f(perm, set(), True) is True                                                    # admin holds everything
    assert f({"level": "authenticated", "permissions": []}, set(), False) is True
    assert f({"level": "custom", "permissions": [], "custom": ["g"]}, set(), False) is None  # reviewed by hand, not guessed


def test_smoke_never_sends_state_changing_requests():
    src = (ROOT / "scripts/rbac_smoke.py").read_text()
    calls = {m for m in __import__("re").findall(r"\bs(?:ession)?\.(get|post|put|patch|delete)\(", src)}
    assert calls == {"get", "post"}                    # post is only the sign-in
    assert src.count(".post(") == 1 and "/api/auth/login" in src.split(".post(")[1][:80]
