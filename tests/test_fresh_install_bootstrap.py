# tests/test_fresh_install_bootstrap.py
"""
Regression tests for two defects found by rehearsing a genuine FRESH install
(MySQL 8.0, db_schema_only.sql, seeded users, then every setup.sh step):

1. Installer scripts REWROTE tracked source code.
   apply_org_group_rbac.py (run by setup.sh, deploy.sh and update.sh) replaced
   app/auth/authorization.py wholesale with an embedded Phase-2 snapshot
   whenever a single anchor line still existed -- silently discarding later
   fixes to the account-scoping core (fail-closed JSON parsing, two of the three
   try/finally connection-release blocks, a selected column). Its sibling
   apply_group_level_role_fix.py then re-added GROUP_LEVEL_ROLE, the
   role-from-group auto-promotion that was removed on purpose.

2. `migrate.py baseline --all-except-rollbacks` recorded EVERY migration as
   applied without running it. 24 tables and columns such as users.token_version
   never existed on a fresh install, so the seeded admin could not log in
   (HTTP 500, "Unknown column 'token_version'"). `migrate.py bootstrap` baselines
   only the three migrations the base schema already reflects and APPLIES the rest.

No database is needed: the scripts run against an isolated copy of the tree, and
migrate.py's two commands are replaced with recorders.
"""
import importlib.util
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── 1. installer scripts must not rewrite tracked code ───────────────

@pytest.fixture()
def tree_copy(tmp_path):
    """An isolated copy of just what the two scripts look at."""
    shutil.copytree(ROOT / "app", tmp_path / "app",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "db" / "migrations").mkdir(parents=True)
    # 011 is a pre-flight prerequisite of apply_org_group_rbac.py; without it the
    # script aborts BEFORE reaching the rewrite and this test would prove nothing.
    for mig in ("011_access_scopes.sql", "013_org_group_rbac.sql"):
        shutil.copy(ROOT / "db" / "migrations" / mig, tmp_path / "db" / "migrations" / mig)
    for name in ("apply_org_group_rbac.py", "apply_group_level_role_fix.py"):
        shutil.copy(ROOT / name, tmp_path / name)
    return tmp_path


def _snapshot(tree):
    return {str(p.relative_to(tree)): p.read_bytes()
            for p in (tree / "app").rglob("*.py")}


def test_org_group_rbac_does_not_rewrite_authorization_py(tree_copy):
    before = _snapshot(tree_copy)
    result = subprocess.run(
        [sys.executable, "apply_org_group_rbac.py", "--skip-db"],
        cwd=tree_copy, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _snapshot(tree_copy) == before, (
        "apply_org_group_rbac.py modified tracked code under app/ -- it would "
        "revert later fixes to authorization.py on every setup/deploy/update")
    assert "Code step skipped" in result.stdout
    assert not list((tree_copy / "app").rglob("*.bak*")), "left backup files behind"


def test_group_level_role_fix_does_not_re_add_the_removed_mapping(tree_copy):
    before = _snapshot(tree_copy)
    result = subprocess.run(
        [sys.executable, "apply_group_level_role_fix.py"],
        cwd=tree_copy, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _snapshot(tree_copy) == before
    auth = (tree_copy / "app" / "auth" / "authorization.py").read_text(encoding="utf-8")
    assert "GROUP_LEVEL_ROLE = {" not in auth, "role-from-group escalation mapping re-added"


def test_current_authorization_py_keeps_the_hardening_the_installer_used_to_revert():
    """Guards the guard: the properties the old rewrite destroyed are really
    present in the tracked file, so the tests above are protecting something."""
    src = (ROOT / "app" / "auth" / "authorization.py").read_text(encoding="utf-8")
    assert "except (ValueError, TypeError)" in src        # fail-closed JSON parse
    assert src.count("finally:") >= 3                     # connection release on every path
    assert "GROUP_LEVEL_ROLE = {" not in src


# ── 2. migrate.py bootstrap ──────────────────────────────────────────

def _load_migrate():
    spec = importlib.util.spec_from_file_location("migrate_under_test", ROOT / "migrate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_bootstrap_covered_file_exists():
    """cmd_baseline() raises MigrateError for a file that isn't in
    db/migrations, which would abort every fresh install."""
    mod = _load_migrate()
    existing = set(mod.list_migration_files())
    missing = [f for f in mod.BOOTSTRAP_COVERED if f not in existing]
    assert not missing, f"BOOTSTRAP_COVERED names files that no longer exist: {missing}"


def test_bootstrap_covers_only_early_migrations():
    """The covered list is an exception list for migrations the base schema and
    apply_*.py scripts already reflect. It must not creep upward: anything from
    020 onward creates tables/columns nothing else provides and MUST be applied."""
    mod = _load_migrate()
    late = [f for f in mod.BOOTSTRAP_COVERED if int(f.split("_", 1)[0].rstrip("abc")) >= 20]
    assert not late, f"late migrations must be applied on a fresh install, not baselined: {late}"


def test_bootstrap_baselines_the_covered_files_then_applies_everything_else(monkeypatch):
    mod = _load_migrate()
    calls = []
    monkeypatch.setattr(mod, "cmd_baseline", lambda conn, files: calls.append(("baseline", list(files))))
    monkeypatch.setattr(mod, "cmd_apply_all_pending",
                        lambda conn, skip_confirm=False: calls.append(("apply_all", skip_confirm)))
    mod.cmd_bootstrap(object())
    assert calls == [("baseline", list(mod.BOOTSTRAP_COVERED)), ("apply_all", True)]


def test_bootstrap_command_is_registered():
    assert "bootstrap" in (ROOT / "migrate.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("script", ["setup.sh", "deploy/deploy.sh"])
def test_installers_no_longer_baseline_every_migration(script):
    """Regression guard for the fresh-install login failure."""
    text = (ROOT / script).read_text(encoding="utf-8")
    live = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
    assert not any("baseline --all-except-rollbacks" in l for l in live), (
        f"{script} baselines every migration again -- a fresh install would be "
        "missing 24 tables and users.token_version (admin login returns 500)")
    assert any("migrate.py bootstrap" in l for l in live), f"{script} must call migrate.py bootstrap"
