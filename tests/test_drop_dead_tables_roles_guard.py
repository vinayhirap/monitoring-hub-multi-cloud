# tests/test_drop_dead_tables_roles_guard.py
"""
Regression tests for apply_drop_dead_tables.py's handling of `roles`.

`roles` is in that script's DEAD_TABLES (a LEGACY table of that name was
superseded by users.role), but db/migrations/040_rbac_v2_bindings.sql later
REUSED the name for the live RBAC v2 roles table (role_key/role_rank/
is_builtin). The script runs on EVERY setup/deploy/update. Before the fix it
found the seeded v2 table, refused to drop it (non-empty) and then exited 1
with a "needs manual review" WARNING -- a permanent false alarm on every box
running RBAC v2, with MySQL's foreign-key protection as the only thing
standing between an empty v2 table and DROP TABLE.

The fix recognises the v2 table by its `role_key` column and leaves it alone,
while the legacy table keeps its exact old behaviour. No database is needed:
run_sql() (a thin wrapper over the mysql CLI) and the mysqldump subprocess
are replaced with an in-memory fake.
"""
import builtins
import contextlib
import importlib.util
import io
import pathlib
import re
import sys

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "apply_drop_dead_tables.py"


def _run_script(state, monkeypatch):
    """state: {table: {"rows": int, "role_key": bool}} -- the fake database.
    Returns (exit_code, dropped_tables, stdout)."""
    spec = importlib.util.spec_from_file_location("apply_drop_dead_tables_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass  # module-level env lookup may exit when no .env exists; run_sql is replaced anyway

    dropped = []

    def fake_run_sql(sql, database=None):
        table = re.search(r"TABLE_NAME = '(\w+)'", sql)
        if "information_schema.TABLES" in sql:
            return f"{1 if table.group(1) in state else 0}\n"
        if "information_schema.COLUMNS" in sql:
            return f"{1 if state.get('roles', {}).get('role_key') else 0}\n"
        count = re.match(r"SELECT COUNT\(\*\) FROM `(\w+)`", sql)
        if count:
            return f"{state[count.group(1)]['rows']}\n"
        drop = re.match(r"DROP TABLE IF EXISTS `(\w+)`", sql)
        if drop:
            dropped.append(drop.group(1))
            return ""
        raise AssertionError(f"unexpected SQL: {sql!r}")

    class _Ok:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(mod, "run_sql", fake_run_sql)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Ok())  # mysqldump "succeeds"
    monkeypatch.setattr(mod.os, "makedirs", lambda *a, **k: None)
    real_open = builtins.open
    monkeypatch.setattr(
        builtins, "open",
        lambda p, *a, **k: io.StringIO() if str(p).startswith("db_backups") else real_open(p, *a, **k))
    monkeypatch.setattr(sys, "argv", ["apply_drop_dead_tables.py"])

    out = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(out):
            mod.main()
    except SystemExit as e:
        code = e.code or 0
    return code, dropped, out.getvalue()


def test_live_rbac_v2_roles_table_is_left_alone_and_does_not_fail_the_run(monkeypatch):
    """The core regression: 3 seeded builtin rows + a role_key column must be
    neither dropped nor reported as a failure (exit 0, not 1)."""
    code, dropped, out = _run_script({"roles": {"rows": 3, "role_key": True}}, monkeypatch)
    assert code == 0, f"live v2 table must not fail the deploy step:\n{out}"
    assert dropped == []
    assert "live RBAC v2 table" in out


def test_empty_live_v2_roles_table_is_still_never_dropped(monkeypatch):
    """Before the fix the only protection here was MySQL refusing to drop a
    table referenced by foreign keys; now the script itself won't try."""
    code, dropped, out = _run_script({"roles": {"rows": 0, "role_key": True}}, monkeypatch)
    assert code == 0
    assert dropped == []


def test_legacy_empty_roles_table_is_still_dropped(monkeypatch):
    """Unchanged behaviour: a box still holding the ancient, empty legacy
    `roles` table needs it dropped so migration 040's CREATE TABLE IF NOT
    EXISTS can create the real RBAC v2 table."""
    code, dropped, _ = _run_script({"roles": {"rows": 0, "role_key": False}}, monkeypatch)
    assert code == 0
    assert dropped == ["roles"]


def test_legacy_nonempty_roles_table_is_still_blocked_for_human_review(monkeypatch):
    code, dropped, out = _run_script({"roles": {"rows": 4, "role_key": False}}, monkeypatch)
    assert code == 1
    assert dropped == []
    assert "roles: 4 row(s)" in out


def test_nothing_present_is_a_clean_noop(monkeypatch):
    code, dropped, out = _run_script({}, monkeypatch)
    assert code == 0 and dropped == []
    assert "dead tables exist -- nothing to do" in out
