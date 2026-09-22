# tests/test_admin_users_security.py
"""
Audit B01 follow-up: three fixes to app/api/admin/users.py.

1. _hash_password() used its own bcrypt.hashpw() call (STRING-sliced to 72
   chars, hardcoded cost) instead of app.auth.security.hash_password() (BYTE-
   truncated, BCRYPT_ROUNDS-driven) -- a second, independently-drifting
   implementation of the same thing. Now delegates to the shared function.
2. create_user()'s password minimum was 6 chars; every other password-setting
   path in the app (POST /api/auth/reset-password, /change-password) requires
   8. Raised to match.
3. The welcome-email password-reset token created here was INSERTed raw,
   unlike app/api/auth.py's /forgot-password (SHA-256 at rest). Now hashed the
   same way; /reset-password already accepts either form so the emailed link
   is unaffected.
"""
import os
import sys
import types

import pytest
from fastapi import HTTPException, BackgroundTasks

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_module, install_stub


def _users_mod(hash_password=None, mail_configured=True):
    sec = load_module("app/auth/security.py")
    install_stub("app.db", get_connection=lambda: (_ for _ in ()).throw(
        AssertionError("test forgot to call _with_db()")))
    install_stub("app.auth.security", hash_password=hash_password or sec.hash_password)
    install_stub("app.auth.permissions",
                 require_permission=lambda perm: (lambda: {"id": 1, "username": "admin", "role": "admin"}))
    install_stub("app.auth", authorization=types.SimpleNamespace(
        can_manage_role=lambda actor, role: True,
        get_effective_scope=lambda u: "FULL_ACCESS",
        scope_within=lambda *a, **k: True,
        validate_scope_shape=lambda *a, **k: None,
        serialize_scope=lambda u: [],
        FULL_ACCESS="FULL_ACCESS",
    ))
    sent = []
    install_stub("app.email", mailer=types.SimpleNamespace(
        is_configured=lambda: mail_configured, get_public_app_url=lambda: "https://hub",
        send_email=lambda **kw: sent.append(kw) or True))
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    mod = load_module("app/api/admin/users.py")
    mod._sent = sent
    return mod


# ───────────────────────── hashing consolidation ─────────────────────────

def test_hash_password_delegates_to_shared_security_module():
    calls = []
    mod = _users_mod(hash_password=lambda p: calls.append(p) or ("H:" + p))
    assert mod._hash_password("Secret12345") == "H:Secret12345"
    assert calls == ["Secret12345"]


def test_no_local_bcrypt_import_left_in_the_module():
    """The old implementation imported bcrypt directly and hardcoded its own
    cost/truncation; the fix removes that import entirely in favour of the
    shared app.auth.security.hash_password()."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "api", "admin", "users.py")).read()
    assert "import bcrypt" not in src
    assert "bcrypt.hashpw" not in src


# ───────────────────────── password length ─────────────────────────

class _Store:
    def __init__(self):
        self.users = {}
        self.next_id = 1
        self.tokens = {}


class _Cur:
    def __init__(self, st): self.st, self.lastrowid, self._r = st, None, []
    def execute(self, sql, params=None):
        sql = " ".join(sql.split()); p = params or ()
        if sql.startswith("SELECT id, provider"):
            self._r = []
        elif sql.startswith("INSERT INTO users"):
            uid = self.st.next_id; self.st.next_id += 1
            self.st.users[uid] = {"username": p[0], "password": p[1], "role": p[2], "email": p[3]}
            self.lastrowid = uid
        elif sql.startswith("INSERT INTO password_reset_tokens"):
            self.st.tokens[p[0]] = {"token": p[1], "expires_at": p[2]}
        else:
            raise AssertionError(f"unexpected SQL: {sql}")
    def fetchall(self): return self._r
    def fetchone(self): return self._r[0] if self._r else None
    def close(self): pass


class _Conn:
    def __init__(self, st): self.st = st
    def cursor(self, dictionary=False): return _Cur(self.st)
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


def _with_db(mod, st):
    install_stub("app.db", get_connection=lambda: _Conn(st))
    mod.get_connection = lambda: _Conn(st)
    return mod


def test_password_minimum_is_now_8_not_6():
    mod = _users_mod()
    st = _Store(); _with_db(mod, st)
    with pytest.raises(HTTPException) as e:
        mod.create_user({"username": "bob", "password": "Sh0rt7x", "role": "viewer"},  # 7 chars
                        current_user={"id": 1, "username": "admin", "role": "admin"})
    assert e.value.status_code == 400 and "8 characters" in e.value.detail

    out = mod.create_user({"username": "bob", "password": "EightCh1", "role": "viewer"},
                          current_user={"id": 1, "username": "admin", "role": "admin"})
    assert out["status"] == "created"


# ───────────────────────── welcome-email reset token ─────────────────────────

def test_welcome_email_reset_token_stored_hashed_not_raw():
    mod = _users_mod()
    st = _Store(); _with_db(mod, st)
    out = mod.create_user(
        {"username": "carol", "password": "GoodPassw0rd", "role": "viewer", "email": "carol@corp.com"},
        current_user={"id": 1, "username": "admin", "role": "admin"},
    )
    assert out["email_sent"] is True
    (stored,) = [t["token"] for t in st.tokens.values()]
    assert len(stored) == 64 and all(c in "0123456789abcdef" for c in stored)
    # the link mailed to the user carries the RAW token, not the stored hash
    (mail,) = mod._sent
    assert stored not in mail["body_text"]


def test_reset_link_token_verifies_against_stored_hash():
    mod = _users_mod()
    st = _Store(); _with_db(mod, st)
    mod.create_user(
        {"username": "dave", "password": "GoodPassw0rd", "role": "viewer", "email": "dave@corp.com"},
        current_user={"id": 1, "username": "admin", "role": "admin"},
    )
    (mail,) = mod._sent
    import re
    raw = re.search(r"token=(\S+)", mail["body_text"]).group(1)
    (stored,) = [t["token"] for t in st.tokens.values()]
    assert mod._token_hash(raw) == stored


def test_no_email_no_token_row_created():
    mod = _users_mod()
    st = _Store(); _with_db(mod, st)
    out = mod.create_user({"username": "erin", "password": "GoodPassw0rd", "role": "viewer"},
                          current_user={"id": 1, "username": "admin", "role": "admin"})
    assert out["email_sent"] is False and st.tokens == {}


def test_mail_not_configured_no_token_row_created():
    mod = _users_mod(mail_configured=False)
    st = _Store(); _with_db(mod, st)
    out = mod.create_user(
        {"username": "frank", "password": "GoodPassw0rd", "role": "viewer", "email": "frank@corp.com"},
        current_user={"id": 1, "username": "admin", "role": "admin"},
    )
    assert out["email_sent"] is False and st.tokens == {}
