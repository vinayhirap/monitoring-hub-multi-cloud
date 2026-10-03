# tests/test_audit_p4_error_contract_and_audit_context.py
"""
Audit C8 / E8:
  * every error keeps `detail` and gains {error: {code, message, request_id}}
  * X-Request-ID on every response; client-supplied ids only if they are safe tokens
  * 422 no longer echoes submitted input; 500 leaks nothing
  * audit rows carry user agent + request id, and survive a DB without migration 078
"""
import asyncio
import json
import logging
import sys
import types

import app          # noqa: F401
import app.auth     # noqa: F401

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from tests.conftest import load_module, install_stub  # noqa: E402
from app import errors  # noqa: E402
from app.request_context import (  # noqa: E402
    request_id_middleware, new_request_id, RequestIdFilter, set_request_id,
)


def _app():
    a = FastAPI()
    errors.install(a)
    a.middleware("http")(request_id_middleware)

    class Login(BaseModel):
        username: str
        password: str

    @a.get("/ok")
    def ok():
        return {"fine": True}

    @a.get("/nf")
    def nf():
        raise HTTPException(status_code=404, detail="Account not found")

    @a.get("/rl")
    def rl():
        raise HTTPException(status_code=429, detail="Too many attempts", headers={"Retry-After": "30"})

    @a.get("/boom")
    def boom():
        raise RuntimeError("secret internal detail: db password=hunter2")

    @a.post("/login")
    def login(body: Login):
        return {}

    return a


def _call(app_, method, path, headers=None, body=b""):
    out = {}

    async def run():
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
                out["headers"] = {k.decode().lower(): v.decode() for k, v in msg["headers"]}
            elif msg["type"] == "http.response.body":
                out["body"] = out.get("body", b"") + msg.get("body", b"")

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "path": path, "raw_path": path.encode(), "query_string": b"", "scheme": "http",
                 "server": ("test", 80), "client": ("1.2.3.4", 1234),
                 "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
        try:
            await app_(scope, receive, send)
        except Exception:
            # Starlette's ServerErrorMiddleware sends the 500 from our handler and THEN re-raises so
            # the server can log it. The response is already captured; only fail if none was sent.
            if "status" not in out:
                raise

    asyncio.run(run())
    return out["status"], out["headers"], out["body"]


def test_http_exception_keeps_detail_and_adds_error_object():
    status, h, body = _call(_app(), "GET", "/nf")
    j = json.loads(body)
    assert status == 404 and j["detail"] == "Account not found"
    assert j["error"]["code"] == "not_found" and j["error"]["message"] == "Account not found"
    assert j["error"]["request_id"] == h["x-request-id"]


def test_exception_headers_are_preserved():
    status, h, _ = _call(_app(), "GET", "/rl")
    assert status == 429 and h["retry-after"] == "30"


def test_validation_error_hides_submitted_input():
    status, h, body = _call(_app(), "POST", "/login", {"content-type": "application/json"},
                            json.dumps({"username": "bob", "password": 12345}).encode())
    j = json.loads(body)
    assert status == 422 and j["error"]["code"] == "validation_error"
    assert isinstance(j["detail"], list) and j["detail"]
    assert "input" not in json.dumps(j) and "12345" not in json.dumps(j)
    assert all(set(e) <= {"type", "loc", "msg"} for e in j["detail"])


def test_unhandled_exception_is_generic_json_with_request_id(caplog):
    caplog.set_level(logging.ERROR)
    a = _app()
    # TestClient-style raise_app_exceptions is not involved here: ServerErrorMiddleware sends the 500
    status, h, body = _call(a, "GET", "/boom")
    j = json.loads(body)
    assert status == 500 and j["detail"] == "Internal server error"
    assert "hunter2" not in body.decode() and "RuntimeError" not in body.decode()
    assert j["error"]["code"] == "internal_error" and j["error"]["request_id"]
    assert any("RuntimeError" in (r.exc_text or "") or r.exc_info for r in caplog.records)


def test_request_id_header_on_success():
    status, h, _ = _call(_app(), "GET", "/ok")
    assert status == 200 and len(h["x-request-id"]) == 32


def test_safe_client_request_id_is_honoured_unsafe_is_replaced():
    assert new_request_id("trace-1234.abcd") == "trace-1234.abcd"
    for bad in ("short", "has space in it 123", "line\nbreak12345", "x" * 65, "<script>12345678"):
        out = new_request_id(bad)
        assert out != bad and len(out) == 32
    _, h, _ = _call(_app(), "GET", "/ok", {"x-request-id": "my-trace-0001"})
    assert h["x-request-id"] == "my-trace-0001"


def test_log_filter_adds_request_id():
    set_request_id("rid-abc-12345")
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "m", (), None)
    assert RequestIdFilter().filter(rec) and rec.request_id == "rid-abc-12345"
    set_request_id(None)
    rec2 = logging.LogRecord("x", logging.INFO, "f", 1, "m", (), None)
    RequestIdFilter().filter(rec2)
    assert rec2.request_id == "-"


# ── audit rows ───────────────────────────────────────────────────────

class _MissingColumn(Exception):
    errno = 1054


class _Cur:
    def __init__(self, log, fail_new):
        self.log, self.fail_new = log, fail_new
    def execute(self, sql, params=None):
        if self.fail_new and "user_agent" in sql:
            raise _MissingColumn("Unknown column 'user_agent'")
        self.log.append((sql, params))
    def close(self):
        pass


class _Conn:
    def __init__(self, log, fail_new=False):
        self.log, self.fail_new, self.closed, self.committed = log, fail_new, False, False
    def cursor(self):
        return _Cur(self.log, self.fail_new)
    def commit(self):
        self.committed = True
    def close(self):
        self.closed = True


def _audit(conn):
    install_stub("app.db", get_connection=lambda: conn)
    return load_module("app/audit.py")


def _req(ua="Mozilla/5.0 (X11)", rid="rid-1234567890"):
    return types.SimpleNamespace(client=types.SimpleNamespace(host="203.0.113.9"),
                                 headers={"user-agent": ua},
                                 state=types.SimpleNamespace(request_id=rid))


def test_audit_row_carries_ip_user_agent_and_request_id():
    log = []
    conn = _Conn(log)
    _audit(conn).write_audit("admin", "Login successful", role="ADMIN", request=_req())
    sql, params = log[0]
    assert "user_agent" in sql and "request_id" in sql
    assert params[3:] == ("203.0.113.9", "Mozilla/5.0 (X11)", "rid-1234567890")
    assert conn.committed and conn.closed


def test_user_agent_is_sanitised_and_capped():
    mod = _audit(_Conn([]))
    ua = mod._user_agent(_req(ua="evil\r\nINJECT: x" + "A" * 400))
    assert "\n" not in ua and "\r" not in ua and len(ua) == 255
    assert mod._user_agent(None) is None


def test_falls_back_to_legacy_insert_when_migration_078_missing():
    log = []
    conn = _Conn(log, fail_new=True)
    _audit(conn).write_audit("admin", "Logout", request=_req())
    assert len(log) == 1 and "user_agent" not in log[0][0]
    assert conn.committed and conn.closed


def test_other_db_errors_are_not_swallowed_into_the_fallback():
    class Boom(Exception):
        errno = 1213
    class C(_Cur):
        def execute(self, sql, params=None):
            raise Boom("deadlock")
    class Cn(_Conn):
        def cursor(self):
            return C([], False)
    conn = Cn([])
    _audit(conn).write_audit("a", "b", request=_req())   # never raises (audit must not break requests)
    assert conn.closed and not conn.committed


def test_audit_api_selects_new_columns_with_legacy_fallback():
    src = open(__file__.rsplit("/tests/", 1)[0] + "/app/api/audit_logs.py").read()
    assert "ip_address, user_agent, request_id" in src and "1054" in src


def test_migration_078_is_idempotent_and_adds_both_columns():
    sql = open(__file__.rsplit("/tests/", 1)[0] + "/db/migrations/078_audit_logs_request_context.sql").read()
    for col in ("user_agent", "request_id"):
        assert f"column_name = '{col}'" in sql and f"ADD COLUMN {col}" in sql


def test_main_wires_everything_and_request_id_is_outermost():
    src = open(__file__.rsplit("/tests/", 1)[0] + "/app/main.py").read()
    assert "_errors.install(app)" in src and "install_log_filter()" in src
    assert src.index("async def _origin_check") < src.index("async def _security_headers") \
        < src.index('app.middleware("http")(request_id_middleware)')
