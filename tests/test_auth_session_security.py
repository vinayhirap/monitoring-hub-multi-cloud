# tests/test_auth_session_security.py
"""
Audit B01 regression tests: JWT/session revocation, password-reset flow,
login hygiene, rate-limit TTL repair, SAML request binding and SSO
provisioning. No MySQL/Redis/IdP needed -- see conftest.py for the
load-module-in-isolation approach.
"""
import asyncio
import base64
import hashlib
import os
import sys
import time
import types
from datetime import datetime, timedelta

import jwt
import pytest
from fastapi import BackgroundTasks, HTTPException, Response

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_module, install_stub, FakeConn, contains

os.environ.setdefault("JWT_SECRET", "unit-test-secret-" + "x" * 32)


def _security():
    return load_module("app/auth/security.py")


# ───────────────────────── security.py ─────────────────────────

def test_token_has_jti_tv_and_required_claims():
    sec = _security()
    c = sec.decode_token(sec.create_access_token(7, "bob", "viewer", token_version=3))
    assert c["id"] == 7 and c["tv"] == 3 and c["jti"] and c["exp"] > time.time()


def test_alg_none_and_missing_exp_are_rejected():
    sec = _security()
    forged = jwt.encode({"sub": "1", "username": "a", "role": "admin", "iat": int(time.time())},
                        "", algorithm="none")
    with pytest.raises(jwt.PyJWTError):
        sec.decode_token(forged)
    no_exp = jwt.encode({"sub": "1", "username": "a", "role": "admin", "iat": int(time.time())},
                        os.environ["JWT_SECRET"], algorithm="HS256")
    with pytest.raises(jwt.PyJWTError):
        sec.decode_token(no_exp)


def test_legacy_token_without_jti_still_decodes():
    sec = _security()
    legacy = jwt.encode({"sub": "1", "username": "a", "role": "admin", "iat": int(time.time()),
                         "exp": int(time.time()) + 60}, os.environ["JWT_SECRET"], algorithm="HS256")
    c = sec.decode_token(legacy)
    assert c["jti"] is None and c["tv"] == 0


def test_password_truncation_is_on_bytes_and_consistent():
    sec = _security()
    pw = "é" * 50                       # 100 bytes, 50 chars
    h = sec.hash_password(pw)
    assert sec.verify_password(pw, h)
    assert sec.verify_password(pw + "anything", h)   # bcrypt only sees first 72 bytes
    assert not sec.verify_password("é" * 35 + "z", h)
    assert sec.verify_password("x", None) is False


# ───────────────────────── deps.py ─────────────────────────

def _deps(rows, counter=None, raise_exc=None):
    sec = _security()
    install_stub("app.auth.security", decode_token=sec.decode_token)

    def get_connection():
        if counter is not None:
            counter.append(1)
        if raise_exc:
            raise raise_exc
        return FakeConn([(contains("FROM users u"), rows)])
    install_stub("app.db", get_connection=get_connection)
    return load_module("app/auth/deps.py")


def _claims(**kw):
    base = {"id": 5, "username": "u", "role": "viewer", "jti": "j1", "tv": 0, "exp": int(time.time()) + 600}
    base.update(kw)
    return base


def _ok_row(**kw):
    row = {"role": "editor", "active": 1, "token_version": 0, "revoked": 0}
    row.update(kw)
    return [row]


def test_valid_session_returns_current_db_role_not_token_role():
    deps = _deps(_ok_row(role="editor"))
    assert deps.validate_session_claims(_claims(role="admin")) == "editor"


@pytest.mark.parametrize("row", [
    {"active": 0},            # deactivated
    {"revoked": 1},           # logged out
    {"token_version": 1},     # password changed after issue
])
def test_dead_sessions_are_401(row):
    deps = _deps(_ok_row(**row))
    with pytest.raises(HTTPException) as e:
        deps.validate_session_claims(_claims())
    assert e.value.status_code == 401


def test_deleted_user_is_401():
    deps = _deps([])
    with pytest.raises(HTTPException) as e:
        deps.validate_session_claims(_claims())
    assert e.value.status_code == 401


def test_state_lookup_is_cached_per_session():
    calls = []
    deps = _deps(_ok_row(), counter=calls)
    for _ in range(5):
        deps.validate_session_claims(_claims())
    assert len(calls) == 1
    deps.forget_user_sessions(5)
    deps.validate_session_claims(_claims())
    assert len(calls) == 2


def test_db_failure_is_503_not_open():
    deps = _deps(None, raise_exc=RuntimeError("db down"))
    with pytest.raises(HTTPException) as e:
        deps.validate_session_claims(_claims())
    assert e.value.status_code == 503


def test_unmigrated_schema_degrades_to_legacy_behaviour():
    from mysql.connector import errors
    deps = _deps(None, raise_exc=errors.ProgrammingError("Unknown column", errno=1054))
    assert deps.validate_session_claims(_claims(role="viewer")) == "viewer"


def test_revoke_session_inserts_jti_and_ignores_legacy_tokens():
    executed = []

    class Cur:
        def execute(self, sql, params=None): executed.append((" ".join(sql.split()), params))
        def close(self): pass

    class Conn:
        def cursor(self, dictionary=False): return Cur()
        def commit(self): pass
        def close(self): pass

    sec = _security()
    install_stub("app.auth.security", decode_token=sec.decode_token)
    install_stub("app.db", get_connection=lambda: Conn())
    deps = load_module("app/auth/deps.py")
    deps.revoke_session(5, None, int(time.time()) + 60)
    assert executed == []
    deps.revoke_session(5, "abc", int(time.time()) + 60)
    assert "INSERT IGNORE INTO revoked_sessions" in executed[0][0] and executed[0][1][0] == "abc"


# ───────────────────────── rate_limit.py ─────────────────────────

class _FakeRedis:
    def __init__(self):
        self.store, self.expiries, self.expire_calls = {}, {}, 0
    def ping(self): return True
    def incr(self, k):
        self.store[k] = self.store.get(k, 0) + 1
        return self.store[k]
    def expire(self, k, s):
        self.expire_calls += 1
        self.expiries[k] = s
    def ttl(self, k): return self.expiries.get(k, -1)


def test_rate_limit_repairs_key_left_without_ttl():
    mod = load_module("app/auth/rate_limit.py")
    r = _FakeRedis()
    mod._get_redis = lambda: r
    r.store["ratelimit:k"] = 50            # orphaned counter, no expiry set
    with pytest.raises(HTTPException) as e:
        mod.check_rate_limit("k", max_attempts=5, window_seconds=300)
    assert e.value.status_code == 429
    assert r.expiries["ratelimit:k"] == 300   # TTL restored -> lockout is not permanent


# ───────────────────────── saml.py ─────────────────────────

def _saml():
    return load_module("app/auth/saml.py")


def test_extract_in_response_to():
    saml = _saml()
    xml = '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" InResponseTo="ONELOGIN_abc123"/>'
    assert saml.extract_in_response_to(base64.b64encode(xml.encode()).decode()) == "ONELOGIN_abc123"
    unsolicited = '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"/>'
    assert saml.extract_in_response_to(base64.b64encode(unsolicited.encode()).decode()) is None
    assert saml.extract_in_response_to("not-base64!!") is None


def test_request_id_is_single_use():
    saml = _saml()

    class R:
        def __init__(self): self.d = {}
        def set(self, k, v, ex=None): self.d[k] = v
        def delete(self, k): return 1 if self.d.pop(k, None) is not None else 0

    saml._redis = lambda: R() if not hasattr(saml, "_r") else saml._r
    saml._r = R()
    saml.remember_request_id("ONELOGIN_x")
    assert saml.consume_request_id("ONELOGIN_x") is True
    assert saml.consume_request_id("ONELOGIN_x") is False      # replay
    assert saml.consume_request_id("never-issued") is False    # unsolicited


def test_request_data_comes_from_acs_url_not_host_header(monkeypatch):
    saml = _saml()
    monkeypatch.setenv("SSO_SP_ACS_URL", "https://hub.example.com/api/auth/sso/acs")

    class Req:
        headers = {"host": "evil.example.net", "x-forwarded-proto": "http"}
        query_params = {}
        async def form(self): return {"SAMLResponse": "abc"}

    d = asyncio.run(saml.build_request_data(Req()))
    assert d["http_host"] == "hub.example.com" and d["https"] == "on"
    assert d["script_name"] == "/api/auth/sso/acs" and d["post_data"] == {"SAMLResponse": "abc"}


def test_saml_settings_reject_deprecated_algorithms(monkeypatch):
    saml = _saml()
    for k in ("SSO_SP_ENTITY_ID", "SSO_SP_ACS_URL", "SSO_IDP_ENTITY_ID", "SSO_IDP_SSO_URL", "SSO_IDP_X509_CERT"):
        monkeypatch.setenv(k, "x")
    sec = saml.build_saml_settings()["security"]
    assert sec["rejectDeprecatedAlgorithm"] and sec["wantAssertionsSigned"] and sec["wantMessagesSigned"]


# ───────────────────────── sso.py provisioning ─────────────────────────

class _RecCursor:
    """Answers the SELECT with `rows`, records everything else."""
    def __init__(self, rows, log):
        self.rows, self.log, self.lastrowid = rows, log, 99
    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))
    def fetchall(self): return self.rows
    def close(self): pass


class _RecConn:
    def __init__(self, rows, log): self.rows, self.log = rows, log
    def cursor(self, dictionary=True): return _RecCursor(self.rows, self.log)
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


def _sso(rows, log):
    saml = _saml()
    install_stub("app.db", get_connection=lambda: _RecConn(rows, log))
    install_stub("app.auth.security", create_access_token=lambda *a, **k: "tok")
    install_stub("app.auth.deps", set_session_cookie=lambda r, t: None)
    install_stub("app.auth.rate_limit", enforce_sso_rate_limit=lambda r: None)
    install_stub("app.auth", saml=saml)
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    return load_module("app/api/sso.py")


def test_sso_never_reprovisions_a_deactivated_account(monkeypatch):
    monkeypatch.setenv("SSO_AUTO_PROVISION", "true")
    log = []
    sso = _sso([{"id": 1, "username": "jdoe", "role": "admin", "active": 0, "token_version": 0}], log)
    with pytest.raises(sso._SsoDenied):
        sso._find_or_provision_user("jdoe@corp.com")
    assert not any(sql.startswith("INSERT") for sql, _ in log)


def test_sso_refuses_ambiguous_email():
    rows = [{"id": i, "username": f"u{i}", "role": "viewer", "active": 1, "token_version": 0} for i in (1, 2)]
    sso = _sso(rows, [])
    with pytest.raises(sso._SsoDenied):
        sso._find_or_provision_user("shared@corp.com")


def test_sso_existing_active_user_matches():
    sso = _sso([{"id": 3, "username": "amy", "role": "editor", "active": 1, "token_version": 4}], [])
    assert sso._find_or_provision_user("amy@corp.com") == (3, "amy", "editor", 4)


def test_sso_provision_uses_password_column_and_caps_role(monkeypatch):
    monkeypatch.setenv("SSO_AUTO_PROVISION", "true")
    monkeypatch.setenv("SSO_DEFAULT_ROLE", "admin")     # must NOT be honoured
    log = []
    sso = _sso([], log)
    uid, username, role, tv = sso._find_or_provision_user("new@corp.com")
    insert_sql, params = [x for x in log if x[0].startswith("INSERT")][0]
    assert "password_hash" not in insert_sql and "(username, email, password, role" in insert_sql
    assert role == "viewer" and params[3] == "viewer" and uid == 99


def test_sso_off_by_default_returns_none():
    sso = _sso([], [])
    assert sso._find_or_provision_user("nobody@corp.com") is None


# ───────────────────────── api/auth.py ─────────────────────────

class _Store:
    """Tiny in-memory model of users + password_reset_tokens for auth.py."""
    def __init__(self):
        self.users = {1: {"id": 1, "username": "amy", "email": "amy@corp.com", "role": "editor",
                          "active": 1, "pw": "old", "token_version": 0}}
        self.tokens = {}     # id -> dict(user_id, token, expires_at, created_at_age)
        self.next_id = 1
        self.log = []
        self.recent_token = False
        self.opened = 0
        self.closed = 0


class _AuthCursor:
    def __init__(self, st): self.st, self._r, self.rowcount = st, [], 0
    def execute(self, sql, params=None):
        sql = " ".join(sql.split()); st = self.st; p = params or ()
        st.log.append((sql, p))
        if sql.startswith("SELECT id, role") or ("FROM users WHERE username" in sql and "SELECT id, email" in sql):
            u = [u for u in st.users.values() if u["username"] == p[0] and u["active"]]
            self._r = u
        elif sql.startswith("SELECT id, username, role, token_version"):
            self._r = [u for u in st.users.values() if u["username"] == p[0] and u["active"]]
        elif sql.startswith("SELECT 1 FROM password_reset_tokens"):
            self._r = [{"x": 1}] if st.recent_token else []
        elif sql.startswith("INSERT INTO password_reset_tokens"):
            st.tokens[st.next_id] = {"id": st.next_id, "user_id": p[0], "token": p[1], "expires_at": p[2]}
            st.next_id += 1
        elif sql.startswith("SELECT prt.id"):
            self._r = []
            for t in st.tokens.values():
                u = st.users[t["user_id"]]
                if t["token"] in p and u["active"]:
                    self._r = [{"token_id": t["id"], "user_id": t["user_id"], "expires_at": t["expires_at"],
                                "username": u["username"], "role": u["role"]}]
        elif sql.startswith("DELETE FROM password_reset_tokens WHERE id"):
            self.rowcount = 1 if st.tokens.pop(p[0], None) else 0
        elif sql.startswith("DELETE FROM password_reset_tokens WHERE user_id"):
            for k in [k for k, t in st.tokens.items() if t["user_id"] == p[0]]:
                del st.tokens[k]
        elif sql.startswith("UPDATE users SET password"):
            st.users[p[1]]["pw"] = p[0]; st.users[p[1]]["token_version"] += 1
        elif sql.startswith("SELECT token_version"):
            self._r = [{"token_version": st.users[p[0]]["token_version"]}]
        elif sql.startswith("SELECT id, password AS pw"):
            self._r = [{"id": u["id"], "pw": u["pw"]} for u in st.users.values() if u["username"] == p[0]]
        else:
            raise AssertionError(f"unexpected SQL: {sql}")
    def fetchone(self): return self._r[0] if self._r else None
    def close(self): pass


class _AuthConn:
    def __init__(self, st): self.st = st; st.opened += 1
    def cursor(self, dictionary=True): return _AuthCursor(self.st)
    def commit(self): pass
    def rollback(self): pass
    def close(self): self.st.closed += 1


def _auth(st, mail_configured=True):
    sec = _security()
    sent = []
    install_stub("app.db", get_connection=lambda: _AuthConn(st))
    install_stub("app.auth.security", create_access_token=sec.create_access_token,
                 decode_token=sec.decode_token,
                 hash_password=lambda p: "H:" + p, verify_password=lambda p, h: h == "H:" + p)
    install_stub("app.auth.deps", get_current_user=lambda: None, COOKIE_NAME="mh_session",
                 COOKIE_SECURE=False, COOKIE_MAX_AGE_SECONDS=1,
                 set_session_cookie=lambda r, t: r.set_cookie("mh_session", t),
                 clear_session_cookie=lambda r: None,
                 revoke_session=lambda *a: sent.append(("revoke",) + a),
                 forget_user_sessions=lambda uid: sent.append(("forget", uid)))
    install_stub("app.auth.rate_limit", enforce_login_rate_limit=lambda *a: None,
                 enforce_forgot_password_rate_limit=lambda *a: None,
                 enforce_reset_password_rate_limit=lambda *a: None)
    install_stub("app.email", mailer=types.SimpleNamespace(
        is_configured=lambda: mail_configured, get_public_app_url=lambda: "https://hub",
        send_email=lambda **kw: sent.append(("mail", kw)) or True))
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    mod = load_module("app/api/auth.py")
    mod._sent = sent
    return mod


def _req():
    return types.SimpleNamespace(client=types.SimpleNamespace(host="203.0.113.9"), cookies={})


def test_forgot_password_stores_only_hash_and_mails_in_background():
    st = _Store()
    auth = _auth(st)
    bg = BackgroundTasks()
    auth.forgot_password(_req(), bg, {"username": "amy"})
    (stored,) = [t["token"] for t in st.tokens.values()]
    (task,) = bg.tasks
    raw = task.args[2]
    assert stored == hashlib.sha256(raw.encode()).hexdigest() and stored != raw
    assert not [s for s in auth._sent if s[0] == "mail"]     # not sent inline in the request


def test_forgot_password_same_response_for_unknown_user_and_cooldown():
    st = _Store()
    auth = _auth(st)
    unknown = auth.forgot_password(_req(), BackgroundTasks(), {"username": "ghost"})
    known = auth.forgot_password(_req(), BackgroundTasks(), {"username": "amy"})
    st.recent_token = True
    bg = BackgroundTasks()
    cooled = auth.forgot_password(_req(), bg, {"username": "amy"})
    assert unknown == known == cooled and bg.tasks == []
    assert len(st.tokens) == 1                                # cooldown created no 2nd token


def test_undelivered_token_not_logged_by_default(caplog, monkeypatch):
    st = _Store()
    auth = _auth(st, mail_configured=False)
    monkeypatch.delenv("RESET_TOKEN_LOG_FALLBACK", raising=False)
    with caplog.at_level("WARNING"):
        auth.forgot_password(_req(), BackgroundTasks(), {"username": "amy"})
    (stored,) = [t["token"] for t in st.tokens.values()]
    assert "token=" not in caplog.text and stored not in caplog.text
    monkeypatch.setenv("RESET_TOKEN_LOG_FALLBACK", "true")
    caplog.clear()
    st.recent_token = False
    with caplog.at_level("WARNING"):
        auth.forgot_password(_req(), BackgroundTasks(), {"username": "amy"})
    assert "token=" in caplog.text


def _issue(auth, st):
    bg = BackgroundTasks()
    auth.forgot_password(_req(), bg, {"username": "amy"})
    return bg.tasks[0].args[2]


def test_reset_is_single_use_and_bumps_token_version():
    st = _Store(); auth = _auth(st)
    raw = _issue(auth, st)
    assert auth.reset_password(_req(), {"token": raw, "new_password": "NewPassw0rd"}) == {"status": "ok"}
    assert st.users[1]["pw"] == "H:NewPassw0rd" and st.users[1]["token_version"] == 1
    assert ("forget", 1) in auth._sent
    with pytest.raises(HTTPException) as e:
        auth.reset_password(_req(), {"token": raw, "new_password": "Another1234"})
    assert e.value.status_code == 400


def test_stored_hash_cannot_be_replayed_as_a_token():
    st = _Store(); auth = _auth(st)
    _issue(auth, st)
    (stored_hash,) = [t["token"] for t in st.tokens.values()]
    with pytest.raises(HTTPException):
        auth.reset_password(_req(), {"token": stored_hash, "new_password": "NewPassw0rd"})
    assert st.users[1]["pw"] == "old"


def test_legacy_raw_token_from_admin_flow_still_works():
    st = _Store(); auth = _auth(st)
    st.tokens[1] = {"id": 1, "user_id": 1, "token": "legacyRawToken_43chars_urlsafe-abcdefghijklm",
                    "expires_at": datetime.utcnow() + timedelta(hours=1)}
    auth.reset_password(_req(), {"token": "legacyRawToken_43chars_urlsafe-abcdefghijklm", "new_password": "NewPassw0rd"})
    assert st.users[1]["pw"] == "H:NewPassw0rd"


def test_expired_and_inactive_reset_rejected():
    st = _Store(); auth = _auth(st)
    raw = _issue(auth, st)
    for t in st.tokens.values():
        t["expires_at"] = datetime.utcnow() - timedelta(seconds=1)
    with pytest.raises(HTTPException):
        auth.reset_password(_req(), {"token": raw, "new_password": "NewPassw0rd"})
    st2 = _Store(); auth2 = _auth(st2)
    raw2 = _issue(auth2, st2)
    st2.users[1]["active"] = 0
    with pytest.raises(HTTPException):
        auth2.reset_password(_req(), {"token": raw2, "new_password": "NewPassw0rd"})


def test_change_password_bumps_version_reissues_cookie_and_kills_reset_links():
    st = _Store(); st.users[1]["pw"] = "H:OldPassw0rd"
    auth = _auth(st)
    _issue(auth, st)
    resp = Response()
    auth.change_password(resp, {"current_password": "OldPassw0rd", "new_password": "BrandNew123"},
                         {"id": 1, "username": "amy", "role": "editor"})
    assert st.users[1]["token_version"] == 1 and st.tokens == {}
    assert "mh_session=" in resp.headers["set-cookie"]
    with pytest.raises(HTTPException) as e:
        auth.change_password(Response(), {"current_password": "wrong", "new_password": "BrandNew123"},
                             {"id": 1, "username": "amy", "role": "editor"})
    assert e.value.status_code == 401 and st.opened == st.closed      # no leaked connection


def test_login_unknown_user_burns_a_bcrypt_verify_and_releases_connection():
    st = _Store(); auth = _auth(st)
    seen = []
    auth._verify_password = lambda p, h: seen.append(h) or False
    with pytest.raises(HTTPException) as e:
        auth.login(_req(), Response(), {"username": "ghost", "password": "whatever1"})
    assert e.value.status_code == 401 and seen and seen[0].startswith("$2") and st.opened == st.closed


def test_login_non_string_fields_are_400_not_500():
    auth = _auth(_Store())
    with pytest.raises(HTTPException) as e:
        auth.login(_req(), Response(), {"username": ["a"], "password": 1})
    assert e.value.status_code == 400


def test_login_connection_released_when_query_raises():
    st = _Store(); auth = _auth(st)
    class Boom(_AuthCursor):
        def execute(self, *a, **k): raise RuntimeError("db hiccup")
    class BoomConn(_AuthConn):
        def cursor(self, dictionary=True): return Boom(self.st)
    auth.get_connection = lambda: BoomConn(st)
    with pytest.raises(RuntimeError):
        auth.login(_req(), Response(), {"username": "amy", "password": "whatever1"})
    assert st.opened == st.closed == 1


def test_logout_revokes_the_session_server_side():
    st = _Store(); auth = _auth(st)
    sec = _security()
    tok = sec.create_access_token(1, "amy", "editor")
    req = types.SimpleNamespace(cookies={"mh_session": tok}, client=None)
    assert auth.logout(req, Response()) == {"status": "ok"}
    (rev,) = [s for s in auth._sent if s[0] == "revoke"]
    assert rev[1] == 1 and rev[2] and rev[3] > time.time()
    # garbage / missing cookies still succeed
    assert auth.logout(types.SimpleNamespace(cookies={"mh_session": "junk"}, client=None), Response()) == {"status": "ok"}
    assert auth.logout(types.SimpleNamespace(cookies={}, client=None), Response()) == {"status": "ok"}
