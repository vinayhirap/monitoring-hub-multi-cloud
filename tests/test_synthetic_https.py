# tests/test_synthetic_https.py
"""Synthetic checks: first-class HTTPS + TLS monitoring (migration 082).

Covers the 'https' check type: TLS facts captured from the probe's own
connection, specific failure text (expired / hostname mismatch / untrusted /
self-signed), the cert-expiry alert levels, the SSRF guard staying in force,
scheme validation, and the API wiring. The end-to-end tests run a real local
TLS server with generated certificates (needs `cryptography`, already in
requirements.txt; they skip if it is missing).
"""
import datetime as dt
import ipaddress
import socket
import ssl
import sys
import threading

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app          # noqa: F401
import app.auth     # noqa: F401

from tests.conftest import load_module, install_stub, FakeConn


def _synthetic():
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    return load_module("app/collector/synthetic.py")


def _api():
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    return load_module("app/api/synthetic.py")


# ── pure helpers ─────────────────────────────────────────────────────

@pytest.mark.parametrize("days,expected", [
    (400, None), (31, None),
    (30, ("WARNING", 30)), (8, ("WARNING", 30)),
    (7, ("CRITICAL", 7)), (0, ("CRITICAL", 7)), (-3, ("CRITICAL", 7)),
    (None, None),
])
def test_cert_alert_level(days, expected):
    mod = _synthetic()
    assert mod._cert_alert_level(days) == expected


def test_cert_thresholds_default_and_env_override(monkeypatch):
    mod = _synthetic()
    monkeypatch.delenv("SYNTHETIC_CERT_WARN_DAYS", raising=False)
    monkeypatch.delenv("SYNTHETIC_CERT_CRIT_DAYS", raising=False)
    assert mod._load_cert_thresholds() == (30, 7)
    monkeypatch.setenv("SYNTHETIC_CERT_WARN_DAYS", "45")
    monkeypatch.setenv("SYNTHETIC_CERT_CRIT_DAYS", "10")
    assert mod._load_cert_thresholds() == (45, 10)


@pytest.mark.parametrize("warn,crit", [("5", "9"), ("10", "10"), ("abc", "7"), ("0", "7")])
def test_cert_thresholds_invalid_env_falls_back(monkeypatch, warn, crit):
    mod = _synthetic()
    monkeypatch.setenv("SYNTHETIC_CERT_WARN_DAYS", warn)
    monkeypatch.setenv("SYNTHETIC_CERT_CRIT_DAYS", crit)
    assert mod._load_cert_thresholds() == (30, 7)


def test_summarize_peercert_days_and_names():
    mod = _synthetic()
    now = ssl.cert_time_to_seconds("Jun  1 00:00:00 2027 GMT")
    cert = {
        "notAfter": "Jun 21 12:00:00 2027 GMT",
        "subject": ((("commonName", "api.example.com"),),),
        "issuer": ((("countryName", "US"),), (("organizationName", "Example CA"),)),
    }
    out = mod._summarize_peercert(cert, now_ts=now)
    assert out["cert_days_left"] == 20
    assert out["cert_not_after"] == dt.datetime(2027, 6, 21, 12, 0, 0)
    assert out["cert_subject"] == "api.example.com"
    assert out["cert_issuer"] == "Example CA"


def test_summarize_peercert_subject_falls_back_to_san_and_tolerates_garbage():
    mod = _synthetic()
    out = mod._summarize_peercert({"notAfter": "not a date", "subject": (),
                                   "subjectAltName": (("DNS", "san.example.com"),)})
    assert out["cert_subject"] == "san.example.com"
    assert out["cert_days_left"] is None and out["cert_not_after"] is None
    assert all(v is None for v in mod._summarize_peercert({}).values())


def _verify_error(code, message):
    e = ssl.SSLCertVerificationError(1, message)
    e.verify_code, e.verify_message = code, message
    return e


@pytest.mark.parametrize("code,message,needle", [
    (10, "certificate has expired", "has expired"),
    (9, "certificate is not yet valid", "not yet valid"),
    (62, "Hostname mismatch, certificate is not valid for 'x'", "hostname mismatch"),
    (64, "IP address mismatch", "hostname mismatch"),
    (18, "self-signed certificate", "self-signed"),
    (19, "self-signed certificate in certificate chain", "self-signed"),
    (20, "unable to get local issuer certificate", "certificate chain"),
    (21, "unable to verify the first certificate", "certificate chain"),
    (7, "certificate signature failure", "certificate signature failure"),
])
def test_describe_tls_error_verification(code, message, needle):
    mod = _synthetic()
    text, valid = mod._describe_tls_error(_verify_error(code, message), "api.example.com")
    assert needle in text and text.startswith("TLS") and valid is False


def test_describe_tls_error_handshake_has_no_cert_verdict():
    mod = _synthetic()
    err = ssl.SSLError(1, "[SSL: TLSV1_ALERT_PROTOCOL_VERSION] tlsv1 alert protocol version")
    text, valid = mod._describe_tls_error(err, "h")
    assert text.startswith("TLS handshake failed") and valid is None


# ── validation ───────────────────────────────────────────────────────

def test_validate_target_https_type():
    mod = _synthetic()
    mod.validate_target("https", "https://8.8.8.8/health")
    with pytest.raises(ValueError, match="https://"):
        mod.validate_target("https", "http://8.8.8.8/")
    with pytest.raises(ValueError):
        mod.validate_target("https", "ftp://example.com/")
    with pytest.raises(ValueError):
        mod.validate_target("https", "https://127.0.0.1/")          # SSRF guard applies to the new type too
    with pytest.raises(ValueError):
        mod.validate_target("https", "https://169.254.169.254/latest/meta-data/")


def test_validate_target_http_type_unchanged_but_create_can_steer():
    mod = _synthetic()
    # unchanged for existing checks / PATCH: http type still accepts both schemes
    mod.validate_target("http", "http://8.8.8.8/")
    mod.validate_target("http", "https://8.8.8.8/")
    # new checks (strict_scheme): an https:// URL is steered to the https type
    mod.validate_target("http", "http://8.8.8.8/", strict_scheme=True)
    with pytest.raises(ValueError, match="HTTPS"):
        mod.validate_target("http", "https://8.8.8.8/", strict_scheme=True)


@pytest.mark.parametrize("ctype,target,ok", [
    ("https", "https://example.com/", True),
    ("https", "https://example.com:443/x?y=1", True),
    ("https", "https://example.com:8443/", False),
    ("http", "http://example.com/", False),
    ("tcp", "example.com:443", False),
])
def test_validate_https_redirect_option(ctype, target, ok):
    mod = _synthetic()
    if ok:
        mod.validate_https_redirect_option(ctype, target)
    else:
        with pytest.raises(ValueError):
            mod.validate_https_redirect_option(ctype, target)


# ── alert writers (fake cursor) ──────────────────────────────────────

class _Cur:
    def __init__(self, existing=None):
        self.calls, self.existing, self.rowcount = [], existing, 1

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.existing


_CHECK = {"aws_account_id": 42, "environment": "prod", "name": "api", "id": 7}


def test_cert_alert_insert():
    mod = _synthetic()
    cur = _Cur()
    mod._write_or_update_cert_alert(cur, "synthetic-7", _CHECK, 5, "CRITICAL", 7)
    sql, params = [c for c in cur.calls if c[0].startswith("INSERT INTO alerts")][0]
    assert "aws_account_id" in sql and params[0] == 42            # account scoped (migration 048)
    assert params[2] == "synthetic_cert_expiry" and params[3] == "CRITICAL"
    assert params[4] == "prod" and params[5] == "42:synthetic_check:synthetic_cert_expiry"
    assert params[6:] == (5, 7, 5, 7)                              # current_value, threshold, breach_value, breach_threshold


def test_cert_alert_escalation_reopens_acknowledged():
    mod = _synthetic()
    cur = _Cur({"id": 9, "severity": "WARNING", "status": "acknowledged"})
    mod._write_or_update_cert_alert(cur, "synthetic-7", _CHECK, 6, "CRITICAL", 7)
    sql, params = [c for c in cur.calls if c[0].startswith("UPDATE alerts")][0]
    assert "severity = %s" in sql and "status = 'active'" in sql and "acked = 0" in sql
    assert params[-1] == 9 and "CRITICAL" in params


def test_cert_alert_same_severity_only_refreshes():
    mod = _synthetic()
    cur = _Cur({"id": 9, "severity": "WARNING", "status": "acknowledged"})
    mod._write_or_update_cert_alert(cur, "synthetic-7", _CHECK, 20, "WARNING", 30)
    sql, _ = [c for c in cur.calls if c[0].startswith("UPDATE alerts")][0]
    assert "severity = %s" not in sql and "status = 'active'" not in sql


def test_apply_cert_alert_no_cert_leaves_state_alone():
    mod = _synthetic()
    cur = _Cur()
    mod._apply_cert_alert(cur, "synthetic-7", _CHECK, {"cert_days_left": None})
    mod._apply_cert_alert(cur, "synthetic-7", _CHECK, None)
    assert cur.calls == []                                         # unknown is not "renewed"


def test_apply_cert_alert_renewed_resolves_only_cert_alert():
    mod = _synthetic()
    cur = _Cur()
    mod._apply_cert_alert(cur, "synthetic-7", _CHECK, {"cert_days_left": 80})
    sql, params = cur.calls[0]
    assert sql.startswith("UPDATE alerts SET status = 'resolved'")
    assert params == ("synthetic-7", 42, "synthetic_cert_expiry")


def test_resolve_alert_default_is_still_uptime():
    mod = _synthetic()
    cur = _Cur()
    mod._resolve_alert(cur, "synthetic-7", 42)
    assert cur.calls[0][1] == ("synthetic-7", 42, "synthetic_uptime")


def test_system_metrics_exempt_cert_alert_from_threshold_disabled():
    # without this the evaluator would auto-resolve the cert alert (no `thresholds` row governs it)
    from app import alert_rules
    assert "synthetic_cert_expiry" in alert_rules.SYSTEM_METRICS
    assert "synthetic_uptime" in alert_rules.SYSTEM_METRICS


def test_insert_result_legacy_statement_for_non_https():
    mod = _synthetic()
    cur = _Cur()
    mod._insert_result(cur, {"id": 3}, True, 12, 200, None, None)
    sql, params = cur.calls[0]
    assert "cert_days_left" not in sql and params == (3, True, 12, 200, None)


def test_insert_result_https_carries_tls_columns():
    mod = _synthetic()
    cur = _Cur()
    tls = {k: None for k in mod._TLS_FIELDS}
    tls.update(cert_days_left=40, tls_version="TLSv1.3", cert_valid=1)
    mod._insert_result(cur, {"id": 3}, True, 12, 200, None, tls)
    sql, params = cur.calls[0]
    assert "tls_version" in sql and "handshake_ms" in sql
    assert sql.count("%s") == len(params) == 5 + len(mod._TLS_FIELDS)


# ── redirect check (stubbed session) ─────────────────────────────────

class _Resp:
    def __init__(self, status, location=None):
        self.status_code, self.headers = status, ({"Location": location} if location else {})

    def close(self):
        pass


class _Sess:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.url, self.kw = resp, exc, None, None

    def get(self, url, **kw):
        self.url, self.kw = url, kw
        if self.exc:
            raise self.exc
        return self.resp

    def close(self):
        pass


@pytest.mark.parametrize("resp,ok,needle", [
    (_Resp(301, "https://example.com/health"), True, None),
    (_Resp(308, "/health"), False, "redirects to"),               # relative -> stays http://
    (_Resp(302, "http://example.com/other"), False, "redirects to"),
    (_Resp(200), False, "no redirect"),
])
def test_redirect_check(monkeypatch, resp, ok, needle):
    mod = _synthetic()
    sess = _Sess(resp)
    monkeypatch.setattr(mod, "_guarded_session", lambda *a, **k: sess)
    got, err = mod._probe_https_redirect("https://example.com/health?x=1", 5)
    assert got is ok and (needle is None or needle in err)
    assert sess.url == "http://example.com/health?x=1" and sess.kw["allow_redirects"] is False


def test_redirect_check_unreachable_port_80(monkeypatch):
    import requests
    mod = _synthetic()
    monkeypatch.setattr(mod, "_guarded_session", lambda *a, **k: _Sess(exc=requests.exceptions.ConnectionError("refused")))
    ok, err = mod._probe_https_redirect("https://example.com/", 5)
    assert ok is False and "not reachable" in err


def test_redirect_flag_only_checked_after_main_probe_succeeds(monkeypatch):
    mod = _synthetic()
    calls = []
    monkeypatch.setattr(mod, "_probe_http_impl", lambda *a, **k: (False, 5, 500, "expected status 200, got 500"))
    monkeypatch.setattr(mod, "_probe_https_redirect", lambda *a, **k: calls.append(1) or (True, None))
    ok, _, _, err, _ = mod._probe_https("https://example.com/", 5, None, None, True)
    assert ok is False and err.startswith("expected status") and calls == []


# ── SSRF guard still applies to the https type ───────────────────────

def test_https_probe_blocks_loopback_and_reports_no_tls():
    mod = _synthetic()
    ok, _, status, err, tls = mod._probe_https("https://127.0.0.1:9/", 2, None, None)
    assert ok is False and status is None and err.startswith("blocked")
    assert set(tls) == set(mod._TLS_FIELDS) and all(v is None for v in tls.values())


def test_https_probe_blocks_dns_rebinding_at_connect(monkeypatch):
    mod = _synthetic()
    real = socket.getaddrinfo

    def fake(host, *a, **k):
        if host == "rebind.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]
        return real(host, *a, **k)
    monkeypatch.setattr(mod.socket, "getaddrinfo", fake)
    ok, _, _, err, _ = mod._probe_https("https://rebind.test/", 2, None, None)
    assert ok is False and err.startswith("blocked")


# ── end to end against a real local TLS server ───────────────────────

crypto = pytest.importorskip("cryptography")
from cryptography import x509                                     # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec          # noqa: E402
from cryptography.x509.oid import NameOID                         # noqa: E402

_PEM = serialization.Encoding.PEM


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _make_ca():
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(_name("Test CA")).issuer_name(_name("Test CA"))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _make_leaf(ca, ip="127.0.0.1", days_valid=90, started_days_ago=1):
    ca_key, ca_cert = ca
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(_name("svc.example.test")).issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=started_days_ago))
            .not_valid_after(now + dt.timedelta(days=days_valid))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


class _TlsServer:
    def __init__(self, tmp_path, leaf):
        key, cert = leaf
        self.cert_file, self.key_file = tmp_path / "leaf.pem", tmp_path / "leaf.key"
        self.cert_file.write_bytes(cert.public_bytes(_PEM))
        self.key_file.write_bytes(key.private_bytes(_PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert_file, self.key_file)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.ctx, self.stop = ctx, False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        self.sock.settimeout(0.2)
        while not self.stop:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                continue
            try:
                raw.settimeout(3)
                conn = self.ctx.wrap_socket(raw, server_side=True)
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                conn.close()
            except (ssl.SSLError, OSError):
                raw.close()                                       # client rejected our cert mid-handshake

    def close(self):
        self.stop = True
        self.thread.join(2)
        self.sock.close()


@pytest.fixture
def tls_env(tmp_path, monkeypatch):
    """(module, ca, serve(leaf)->server). Loopback allowed for the test only; the
    test CA is trusted through requests' default bundle path, NOT by disabling
    verification."""
    import requests.adapters
    mod = _synthetic()
    monkeypatch.setattr(mod, "_is_blocked_ip", lambda ip: False)
    ca = _make_ca()
    ca_file = tmp_path / "ca.pem"
    ca_file.write_bytes(ca[1].public_bytes(_PEM))
    monkeypatch.setattr(requests.adapters, "DEFAULT_CA_BUNDLE_PATH", str(ca_file))
    servers = []

    def serve(leaf):
        srv = _TlsServer(tmp_path, leaf)
        servers.append(srv)
        return srv
    yield mod, ca, serve
    for s in servers:
        s.close()


def test_e2e_valid_cert_reports_tls_facts(tls_env):
    mod, ca, serve = tls_env
    srv = serve(_make_leaf(ca, days_valid=90))
    ok, elapsed, status, err, tls = mod._probe_https(f"https://127.0.0.1:{srv.port}/", 5, None, None)
    assert ok is True and status == 200 and err is None
    assert tls["cert_valid"] == 1
    assert 88 <= tls["cert_days_left"] <= 90
    assert tls["tls_version"].startswith("TLSv1.") and tls["tls_cipher"]
    assert isinstance(tls["handshake_ms"], int) and tls["handshake_ms"] >= 0
    assert tls["cert_subject"] == "svc.example.test" and tls["cert_issuer"] == "Test CA"
    assert isinstance(tls["cert_not_after"], dt.datetime)


def test_e2e_near_expiry_is_still_up_and_reports_days(tls_env):
    mod, ca, serve = tls_env
    srv = serve(_make_leaf(ca, days_valid=10))
    ok, _, _, err, tls = mod._probe_https(f"https://127.0.0.1:{srv.port}/", 5, None, None)
    assert ok is True and err is None                              # expiry is the alert's job, not a probe failure
    assert 9 <= tls["cert_days_left"] <= 10
    assert mod._cert_alert_level(tls["cert_days_left"]) == ("WARNING", 30)


def test_e2e_expired_cert_fails_with_specific_text(tls_env):
    mod, ca, serve = tls_env
    srv = serve(_make_leaf(ca, days_valid=-1, started_days_ago=30))
    ok, _, status, err, tls = mod._probe_https(f"https://127.0.0.1:{srv.port}/", 5, None, None)
    assert ok is False and status is None
    assert "certificate has expired" in err
    assert tls["cert_valid"] == 0 and tls["cert_days_left"] is None


def test_e2e_hostname_mismatch_fails_with_specific_text(tls_env):
    mod, ca, serve = tls_env
    srv = serve(_make_leaf(ca, ip="127.0.0.2"))                    # cert is for another address
    ok, _, _, err, tls = mod._probe_https(f"https://127.0.0.1:{srv.port}/", 5, None, None)
    assert ok is False and "hostname mismatch" in err and "127.0.0.1" in err
    assert tls["cert_valid"] == 0


def test_e2e_untrusted_issuer_fails(tls_env):
    mod, ca, serve = tls_env
    srv = serve(_make_leaf(_make_ca()))                            # signed by a CA nobody trusts
    ok, _, _, err, tls = mod._probe_https(f"https://127.0.0.1:{srv.port}/", 5, None, None)
    assert ok is False and err.startswith("TLS:") and tls["cert_valid"] == 0


def test_e2e_plain_http_probe_unchanged_on_https_url(tls_env):
    # 'http' type pointed at an https:// URL (existing checks): same 4-tuple, same behaviour
    mod, ca, serve = tls_env
    srv = serve(_make_leaf(ca))
    res = mod._probe_http(f"https://127.0.0.1:{srv.port}/", 5, None, None)
    assert len(res) == 4 and res[0] is True and res[2] == 200
    assert mod._run_probe({"check_type": "https", "target": f"https://127.0.0.1:{srv.port}/",
                           "timeout_seconds": 5, "expected_status_code": None,
                           "expected_keyword": None})[0] is True


def test_e2e_keyword_and_status_checks_still_apply_on_https(tls_env):
    mod, ca, serve = tls_env
    srv = serve(_make_leaf(ca))
    ok, _, status, err, tls = mod._probe_https(f"https://127.0.0.1:{srv.port}/", 5, 200, "nope")
    assert ok is False and status == 200 and "keyword" in err
    assert tls["cert_valid"] == 1                                  # TLS facts kept even when the content check fails


# ── API wiring ───────────────────────────────────────────────────────

def _conn_capturing(sqls, row=None):
    class Cur:
        lastrowid = 11

        def execute(self, sql, params=None):
            sqls.append((" ".join(sql.split()), params))

        def fetchone(self):
            return row

        def close(self):
            pass

    class Conn:
        def cursor(self, dictionary=False):
            return Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass
    return Conn()


_USER = {"id": 1, "username": "u"}


def _create(mod, **over):
    payload = {"aws_account_id": 4, "name": "n", "check_type": "https", "target": "https://8.8.8.8/"}
    payload.update(over)
    return mod.create_check(payload, current_user=_USER)


def test_api_create_https_ok(monkeypatch):
    mod = _api()
    sqls = []
    monkeypatch.setattr(mod, "get_connection", lambda: _conn_capturing(sqls))
    assert _create(mod, expect_https_redirect=True)["id"] == 11
    sql, params = [c for c in sqls if c[0].startswith("INSERT INTO synthetic_checks")][0]
    assert "expect_https_redirect" in sql and "https" in params and 1 in params


@pytest.mark.parametrize("over,needle", [
    ({"target": "http://8.8.8.8/"}, "https://"),                                  # https type, http URL
    ({"check_type": "http", "target": "https://8.8.8.8/"}, "HTTPS"),              # http type, https URL (create)
    ({"check_type": "http", "target": "http://8.8.8.8/", "expect_https_redirect": True}, "only available"),
    ({"target": "https://8.8.8.8:8443/", "expect_https_redirect": True}, "443"),
    ({"expect_https_redirect": "yes"}, "true or false"),
    ({"check_type": "ftps"}, "check_type"),
])
def test_api_create_rejections(over, needle):
    from fastapi import HTTPException
    mod = _api()
    with pytest.raises(HTTPException) as exc:
        _create(mod, **over)
    assert exc.value.status_code == 400 and needle in exc.value.detail


def test_api_create_http_with_http_url_unchanged(monkeypatch):
    mod = _api()
    monkeypatch.setattr(mod, "get_connection", lambda: _conn_capturing([]))
    assert _create(mod, check_type="http", target="http://8.8.8.8/")["status"] == "created"


def test_api_patch_target_scheme_follows_stored_type(monkeypatch):
    from fastapi import HTTPException
    mod = _api()
    monkeypatch.setattr(mod, "_get_check_account_id", lambda cid: 4)
    monkeypatch.setattr(mod, "_get_check_row",
                        lambda cid: {"check_type": "https", "target": "https://8.8.8.8/", "expect_https_redirect": 0})
    with pytest.raises(HTTPException) as exc:
        mod.update_check(7, {"target": "http://8.8.8.8/"}, current_user=_USER)
    assert exc.value.status_code == 400
    # an existing 'http' check may still be pointed at another https:// URL (back-compat)
    monkeypatch.setattr(mod, "_get_check_row",
                        lambda cid: {"check_type": "http", "target": "https://8.8.8.8/", "expect_https_redirect": 0})
    sqls = []
    monkeypatch.setattr(mod, "get_connection", lambda: _conn_capturing(sqls))
    assert mod.update_check(7, {"target": "https://8.8.4.4/"}, current_user=_USER)["status"] == "updated"


def test_api_patch_flag_validated_against_current_target(monkeypatch):
    from fastapi import HTTPException
    mod = _api()
    monkeypatch.setattr(mod, "_get_check_account_id", lambda cid: 4)
    monkeypatch.setattr(mod, "_get_check_row",
                        lambda cid: {"check_type": "https", "target": "https://8.8.8.8:8443/", "expect_https_redirect": 0})
    with pytest.raises(HTTPException) as exc:
        mod.update_check(7, {"expect_https_redirect": True}, current_user=_USER)
    assert exc.value.status_code == 400 and "443" in exc.value.detail


def test_api_delete_also_resolves_cert_alert(monkeypatch):
    mod = _api()
    sqls = []
    monkeypatch.setattr(mod, "get_connection", lambda: _conn_capturing(sqls, row={"aws_account_id": 4}))
    mod.delete_check(7, current_user=_USER)
    sql, params = [c for c in sqls if c[0].startswith("UPDATE alerts")][0]
    assert "synthetic_uptime" in sql and params[-1] == "synthetic_cert_expiry"


# ── run_due_checks wiring (fake DB, stubbed probe) ───────────────────

def _run_due(mod, monkeypatch, check, probe_result):
    sqls = []

    class Cur:
        def execute(self, sql, params=None):
            sqls.append((" ".join(sql.split()), params))
            self._last = " ".join(sql.split())

        def fetchall(self):
            return [dict(check)] if self._last.startswith("SELECT id, aws_account_id") else []

        def fetchone(self):
            return None                                            # no open alert yet

        def close(self):
            pass
        rowcount = 0

    class Conn:
        def cursor(self, dictionary=False):
            return Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass
    monkeypatch.setattr(mod, "get_connection", lambda: Conn())
    monkeypatch.setattr(mod, "_probe_https", lambda *a, **k: probe_result)
    assert mod.run_due_checks() == 1
    return sqls


_DUE = {"id": 7, "aws_account_id": 42, "name": "api", "check_type": "https", "target": "https://example.com/",
        "expected_status_code": None, "expected_keyword": None, "expect_https_redirect": 0,
        "timeout_seconds": 10, "interval_seconds": 300, "consecutive_failure_threshold": 2,
        "environment": "prod", "consecutive_failures": 0, "current_status": "up"}


def _tls(mod, **over):
    t = {k: None for k in mod._TLS_FIELDS}
    t.update(over)
    return t


def test_run_due_checks_https_writes_tls_row_and_cert_alert(monkeypatch):
    mod = _synthetic()
    tls = _tls(mod, cert_days_left=5, tls_version="TLSv1.3", cert_valid=1)
    sqls = _run_due(mod, monkeypatch, _DUE, (True, 40, 200, None, tls))
    assert any("expect_https_redirect" in s for s, _ in sqls if s.startswith("SELECT id, aws_account_id"))
    res = [(s, p) for s, p in sqls if s.startswith("INSERT INTO synthetic_check_results")][0]
    assert "tls_version" in res[0] and "TLSv1.3" in res[1] and 5 in res[1]
    alert = [(s, p) for s, p in sqls if s.startswith("INSERT INTO alerts")]
    assert len(alert) == 1 and alert[0][1][2:4] == ("synthetic_cert_expiry", "CRITICAL")
    # the check itself is UP: a near-expiry cert does not create a synthetic_uptime alert
    assert not any(p and "synthetic_uptime" in str(p) for s, p in sqls if s.startswith("INSERT INTO alerts"))


def test_run_due_checks_https_healthy_cert_no_alert(monkeypatch):
    mod = _synthetic()
    tls = _tls(mod, cert_days_left=200, tls_version="TLSv1.3", cert_valid=1)
    sqls = _run_due(mod, monkeypatch, _DUE, (True, 40, 200, None, tls))
    assert not [s for s, _ in sqls if s.startswith("INSERT INTO alerts")]


def test_run_due_checks_http_type_keeps_legacy_insert(monkeypatch):
    mod = _synthetic()
    sqls = []

    class Cur:
        def execute(self, sql, params=None):
            sqls.append((" ".join(sql.split()), params))
            self._last = " ".join(sql.split())

        def fetchall(self):
            return [dict(_DUE, check_type="http")] if self._last.startswith("SELECT id, aws_account_id") else []

        def fetchone(self):
            return None

        def close(self):
            pass
        rowcount = 0

    class Conn:
        def cursor(self, dictionary=False):
            return Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass
    monkeypatch.setattr(mod, "get_connection", lambda: Conn())
    monkeypatch.setattr(mod, "_run_probe", lambda check: (True, 12, 200, None))
    assert mod.run_due_checks() == 1
    res = [s for s, _ in sqls if s.startswith("INSERT INTO synthetic_check_results")][0]
    assert "cert_days_left" not in res
    assert not [s for s, _ in sqls if s.startswith("INSERT INTO alerts")]


def test_run_due_checks_failed_handshake_leaves_cert_alert_untouched(monkeypatch):
    mod = _synthetic()
    tls = _tls(mod, cert_valid=0)                                  # expired cert: handshake refused, no days_left
    sqls = _run_due(mod, monkeypatch, _DUE, (False, 30, None, "TLS: certificate has expired", tls))
    assert not [s for s, p in sqls if "synthetic_cert_expiry" in str(p)]   # not resolved, not re-opened
    res = [(s, p) for s, p in sqls if s.startswith("INSERT INTO synthetic_check_results")][0]
    assert "TLS: certificate has expired" in res[1]
