# tests/test_installer_seed_passwords.py
"""
setup.sh and deploy/deploy.sh used to seed three accounts with fixed, published
passwords on every fresh install:

    admin/admin123   editor/editor123   viewer/viewer123

and print "<- change these" in the final banner. Those are the first passwords
anyone tries, and this repository is public. If an environment was built that
way and never changed, a leaked password hash is trivially reversible.

Each install now gets its own random passwords (secrets.token_urlsafe(15)), shown
once in the final banner and never written to a file or log.

The seeding step is a Python heredoc inside each shell script. These tests
extract that exact block from the shipped script, substitute the shell variables,
and run it against a fake database connector -- so they exercise the real text,
not a copy, with no MySQL needed.
"""
import contextlib
import io
import pathlib
import re
import sys
import types

import bcrypt
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALLERS = ["setup.sh", "deploy/deploy.sh"]
LEGACY = {"admin": "admin123", "editor": "editor123", "viewer": "viewer123"}


def _seed_block(script):
    text = (ROOT / script).read_text(encoding="utf-8")
    blocks = re.findall(r"<<PYEOF\n(.*?)\nPYEOF", text, re.S)
    seeding = [b for b in blocks if "INSERT INTO users" in b]
    assert len(seeding) == 1, f"{script}: expected exactly one user-seeding heredoc, found {len(seeding)}"
    return (seeding[0].replace("${DB_USER}", "u")
                      .replace("${DB_PASS}", "p")
                      .replace("${DB_NAME}", "d"))


class _FakeCursor:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.append((sql, params))

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.executed, self.committed, self.closed = [], False, False

    def cursor(self):
        return _FakeCursor(self.executed)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def _run_seed(script, monkeypatch):
    conn = _FakeConn()
    fake_connector = types.SimpleNamespace(connect=lambda **kw: conn)
    fake_mysql = types.ModuleType("mysql")
    fake_mysql.connector = fake_connector
    monkeypatch.setitem(sys.modules, "mysql", fake_mysql)
    monkeypatch.setitem(sys.modules, "mysql.connector", fake_connector)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(_seed_block(script), f"<{script} seed block>", "exec"), {"__name__": "__main__"})
    return conn, out.getvalue()


@pytest.mark.parametrize("script", INSTALLERS)
def test_seeds_three_accounts_with_random_passwords_that_match_the_stored_hashes(script, monkeypatch):
    conn, out = _run_seed(script, monkeypatch)
    inserts = [p for sql, p in conn.executed if sql.startswith("INSERT INTO users")]
    assert [(u, role) for u, _h, role in inserts] == [
        ("admin", "admin"), ("editor", "editor"), ("viewer", "viewer")]

    printed = dict(line.split(" / ", 1) for line in out.strip().splitlines())
    assert set(printed) == {"admin", "editor", "viewer"}
    for username, hashed, _role in inserts:
        assert bcrypt.checkpw(printed[username].encode(), hashed.encode()), \
            f"{script}: printed password for {username} does not match the stored hash"
        assert hashed.startswith("$2b$12$"), "bcrypt cost should be 12"
        assert not bcrypt.checkpw(LEGACY[username].encode(), hashed.encode()), \
            f"{script}: {username} still accepts its legacy default password"
    assert len(set(printed.values())) == 3, "the three accounts must not share a password"
    assert all(len(p) >= 20 for p in printed.values())
    assert conn.committed and conn.closed


@pytest.mark.parametrize("script", INSTALLERS)
def test_two_installs_never_get_the_same_passwords(script, monkeypatch):
    _c1, out1 = _run_seed(script, monkeypatch)
    _c2, out2 = _run_seed(script, monkeypatch)
    assert out1 != out2


@pytest.mark.parametrize("script", INSTALLERS)
def test_no_default_password_is_seeded_or_advertised(script):
    live = [l for l in (ROOT / script).read_text(encoding="utf-8").splitlines()
            if not l.lstrip().startswith("#")]
    hits = [l for l in live if any(p in l for p in LEGACY.values())]
    assert not hits, f"{script} still references a default password: {hits}"


@pytest.mark.parametrize("script", INSTALLERS)
def test_variable_is_initialised_because_the_installer_runs_with_set_u(script):
    """Both installers run `set -eu`; the existing-database path never assigns
    SEEDED_LOGINS, so an unset reference would abort the whole install."""
    text = (ROOT / script).read_text(encoding="utf-8")
    assert "set -u" in text
    init = text.index('SEEDED_LOGINS=""')
    assert init < text.index("SEEDED_LOGINS=$(") < text.index('if [ -n "$SEEDED_LOGINS" ]')
