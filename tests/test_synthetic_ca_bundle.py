# tests/test_synthetic_ca_bundle.py
"""Synthetic HTTPS checks: SYNTHETIC_CA_BUNDLE (additional private/corporate CAs).

Proves: unset = unchanged; a leaf signed by an unknown CA fails without the
bundle and passes with it; the default public roots keep working next to the
extra CAs; hostname/expiry checks stay on; invalid/missing/oversized files fall
back to the default bundle with one warning (never raise); the SSRF guard is
untouched; the error text points at the env var only while it is unset.
Uses a real local TLS server (needs `cryptography`; skipped if missing).
"""
import datetime as dt
import ipaddress
import logging
import os
import socket
import ssl
import sys
import threading

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app          # noqa: F401
import app.auth     # noqa: F401

from tests.conftest import load_module, install_stub, FakeConn

pytest.importorskip("cryptography")
from cryptography import x509                                     # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec          # noqa: E402
from cryptography.x509.oid import NameOID                         # noqa: E402

_PEM = serialization.Encoding.PEM


def _synthetic():
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    return load_module("app/collector/synthetic.py")


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _make_ca(cn="Corp Private CA"):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(cn))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
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
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


class _TlsServer:
    def __init__(self, tmp_path, leaf, tag="s"):
        key, cert = leaf
        cert_file, key_file = tmp_path / f"{tag}-leaf.pem", tmp_path / f"{tag}-leaf.key"
        cert_file.write_bytes(cert.public_bytes(_PEM))
        key_file.write_bytes(key.private_bytes(_PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert_file, key_file)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.stop = False
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
                raw.close()

    def close(self):
        self.stop = True
        self.thread.join(2)
        self.sock.close()

    @property
    def url(self):
        return f"https://127.0.0.1:{self.port}/"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """(mod, tmp_path, serve, write_ca). The default bundle is pointed at a
    throw-away 'public' CA so the real certifi store is irrelevant and the
    private CA is guaranteed untrusted unless SYNTHETIC_CA_BUNDLE adds it."""
    import requests.adapters
    mod = _synthetic()
    monkeypatch.setattr(mod, "_is_blocked_ip", lambda ip: False)   # loopback allowed for the test only
    monkeypatch.delenv("SYNTHETIC_CA_BUNDLE", raising=False)
    public_ca = _make_ca("Pretend Public Root")
    public_file = tmp_path / "public.pem"
    public_file.write_bytes(public_ca[1].public_bytes(_PEM))
    monkeypatch.setattr(requests.adapters, "DEFAULT_CA_BUNDLE_PATH", str(public_file))
    servers = []

    def serve(leaf):
        srv = _TlsServer(tmp_path, leaf, tag=str(len(servers)))
        servers.append(srv)
        return srv

    def write_ca(ca, name="corp-ca.pem"):
        f = tmp_path / name
        f.write_bytes(ca[1].public_bytes(_PEM))
        return f

    yield mod, public_ca, serve, write_ca
    for s in servers:
        s.close()


def _probe(mod, srv):
    return mod._probe_https(srv.url, 5, None, None)


# ── core behaviour ───────────────────────────────────────────────────

def test_private_ca_fails_without_bundle_and_passes_with_it(env, monkeypatch):
    mod, _, serve, write_ca = env
    corp = _make_ca()
    srv = serve(_make_leaf(corp))

    ok, _, _, err, tls = _probe(mod, srv)                          # no env var: chain not trusted
    assert ok is False and err.startswith("TLS:") and tls["cert_valid"] == 0

    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", str(write_ca(corp)))
    ok, _, status, err, tls = _probe(mod, srv)                     # same server, same code, bundle set
    assert ok is True and status == 200 and err is None
    assert tls["cert_valid"] == 1 and tls["cert_issuer"] == "Corp Private CA"


def test_default_public_roots_still_work_with_bundle_set(env, monkeypatch):
    mod, public_ca, serve, write_ca = env
    corp = _make_ca()
    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", str(write_ca(corp)))
    ok_public = _probe(mod, serve(_make_leaf(public_ca)))          # signed by the default bundle's CA
    ok_corp = _probe(mod, serve(_make_leaf(corp)))                 # signed by the extra CA
    assert ok_public[0] is True and ok_corp[0] is True


def test_unrelated_ca_still_rejected_with_bundle_set(env, monkeypatch):
    mod, _, serve, write_ca = env
    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", str(write_ca(_make_ca())))
    ok, _, _, err, tls = _probe(mod, serve(_make_leaf(_make_ca("Some Other CA"))))
    assert ok is False and err.startswith("TLS:") and tls["cert_valid"] == 0


def test_verification_not_weakened_hostname_and_expiry(env, monkeypatch):
    mod, _, serve, write_ca = env
    corp = _make_ca()
    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", str(write_ca(corp)))
    ok, _, _, err, _ = _probe(mod, serve(_make_leaf(corp, ip="127.0.0.2")))
    assert ok is False and "hostname mismatch" in err
    ok, _, _, err, _ = _probe(mod, serve(_make_leaf(corp, days_valid=-1, started_days_ago=30)))
    assert ok is False and "certificate has expired" in err


def test_session_verify_is_a_path_never_false(env, monkeypatch):
    mod, _, _, write_ca = env
    assert mod._guarded_session().verify is True                   # unset: requests default, untouched
    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", str(write_ca(_make_ca())))
    v = mod._guarded_session().verify
    assert isinstance(v, str) and os.path.isfile(v)                # a merged bundle path, not False


def test_merged_bundle_contains_default_and_extra_roots(env, monkeypatch):
    mod, _, _, write_ca = env
    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", str(write_ca(_make_ca())))
    merged = mod._extra_ca_verify_path()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=merged)
    assert ctx.cert_store_stats()["x509_ca"] == 2                  # 1 default + 1 extra
    assert oct(os.stat(merged).st_mode & 0o777) == "0o600"


def test_multi_cert_bundle_with_stray_text(env, monkeypatch, tmp_path):
    mod, _, serve, _ = env
    a, b = _make_ca("CA A"), _make_ca("CA B")
    f = tmp_path / "two.pem"
    f.write_text("# my corp roots\n" + a[1].public_bytes(_PEM).decode() + "\nnotes\n" + b[1].public_bytes(_PEM).decode())
    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", str(f))
    assert _probe(mod, serve(_make_leaf(a)))[0] is True
    assert _probe(mod, serve(_make_leaf(b)))[0] is True


def test_bundle_change_is_picked_up_without_restart(env, monkeypatch):
    mod, _, serve, write_ca = env
    a, b = _make_ca("CA A"), _make_ca("CA B")
    f = write_ca(a)
    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", str(f))
    srv_b = serve(_make_leaf(b))
    assert _probe(mod, srv_b)[0] is False
    f.write_bytes(a[1].public_bytes(_PEM) + b[1].public_bytes(_PEM))   # operator adds CA B
    assert _probe(mod, srv_b)[0] is True


# ── inert + safe fallbacks ───────────────────────────────────────────

def test_unset_or_blank_is_inert(env, monkeypatch):
    mod, _, _, _ = env
    assert mod._extra_ca_verify_path() is None
    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", "   ")
    assert mod._extra_ca_verify_path() is None


def _bad_files(tmp_path):
    empty = tmp_path / "empty.pem"
    empty.write_bytes(b"")
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("this is not a certificate\n")
    corrupt = tmp_path / "corrupt.pem"
    corrupt.write_text("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")
    key_only = tmp_path / "key.pem"
    ca = _make_ca()
    key_only.write_bytes(ca[0].private_bytes(_PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
                         + ca[1].public_bytes(_PEM))
    big = tmp_path / "big.pem"
    big.write_bytes(b"x" * 5_000_001)
    binary = tmp_path / "binary.pem"
    binary.write_bytes(b"\xff\xfe\x00\x80")
    return {"missing": tmp_path / "nope.pem", "empty": empty, "garbage": garbage, "corrupt": corrupt,
            "with_private_key": key_only, "too_big": big, "binary": binary,
            "a_directory": tmp_path}


@pytest.mark.parametrize("kind", ["missing", "empty", "garbage", "corrupt", "with_private_key",
                                  "too_big", "binary", "a_directory"])
def test_bad_bundle_falls_back_to_default_with_one_warning(env, monkeypatch, caplog, tmp_path, kind):
    mod, public_ca, serve, _ = env
    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", str(_bad_files(tmp_path)[kind]))
    with caplog.at_level(logging.WARNING, logger=mod.logger.name):
        for _ in range(3):
            assert mod._extra_ca_verify_path() is None             # never raises, never half-applies
            assert mod._guarded_session().verify is True            # exactly the default behaviour
    warns = [r for r in caplog.records if "SYNTHETIC_CA_BUNDLE" in r.getMessage() and r.levelno == logging.WARNING]
    assert len(warns) == 1                                          # warned once, not per probe
    # and probing still works against the default roots
    assert _probe(mod, serve(_make_leaf(public_ca)))[0] is True


def test_bad_bundle_does_not_make_private_ca_pass(env, monkeypatch, tmp_path):
    mod, _, serve, _ = env
    bad = tmp_path / "bad.pem"
    bad.write_text("garbage")
    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", str(bad))
    assert _probe(mod, serve(_make_leaf(_make_ca())))[0] is False


def test_non_file_default_bundle_falls_back(env, monkeypatch, tmp_path, caplog):
    import requests.adapters
    mod, _, _, write_ca = env
    monkeypatch.setattr(requests.adapters, "DEFAULT_CA_BUNDLE_PATH", str(tmp_path))   # a directory
    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", str(write_ca(_make_ca())))
    with caplog.at_level(logging.WARNING, logger=mod.logger.name):
        assert mod._extra_ca_verify_path() is None
    assert any("default CA bundle" in r.getMessage() for r in caplog.records)


def test_temp_file_failure_falls_back(env, monkeypatch):
    mod, _, _, write_ca = env
    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", str(write_ca(_make_ca())))
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(mod.tempfile, "mkstemp", boom)
    assert mod._extra_ca_verify_path() is None


# ── SSRF guard untouched ─────────────────────────────────────────────

def test_ssrf_guard_still_blocks_with_bundle_set(tmp_path, monkeypatch):
    mod = _synthetic()                                              # real _is_blocked_ip
    f = tmp_path / "ca.pem"
    f.write_bytes(_make_ca()[1].public_bytes(_PEM))
    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", str(f))
    ok, _, _, err, tls = mod._probe_https("https://127.0.0.1:9/", 3, None, None)
    assert ok is False and "blocked" in err and tls["cert_valid"] is None
    ok, _, _, err = mod._probe_http("https://169.254.169.254/latest/meta-data/", 3, None, None)
    assert ok is False and "blocked" in err
    with pytest.raises(ValueError):
        mod.validate_target("https", "https://127.0.0.1/")


def test_guarded_adapter_pool_classes_unchanged(env, monkeypatch):
    mod, _, _, write_ca = env
    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", str(write_ca(_make_ca())))
    sess = mod._guarded_session({})
    pools = sess.get_adapter("https://x/").poolmanager.pool_classes_by_scheme
    assert pools["http"] is mod._GuardedHTTPPool and issubclass(pools["https"], mod.HTTPSConnectionPool)


# ── error text ───────────────────────────────────────────────────────

def _verify_error(code, message):
    e = ssl.SSLCertVerificationError(1, message)
    e.verify_code, e.verify_message = code, message
    return e


@pytest.mark.parametrize("code,msg,needle", [
    (19, "self-signed certificate in certificate chain", "self-signed"),
    (18, "self-signed certificate", "self-signed"),
    (20, "unable to get local issuer certificate", "certificate chain"),
    (21, "unable to verify the first certificate", "certificate chain"),
])
def test_chain_errors_mention_env_var_only_while_unset(monkeypatch, code, msg, needle):
    mod = _synthetic()
    monkeypatch.delenv("SYNTHETIC_CA_BUNDLE", raising=False)
    text, valid = mod._describe_tls_error(_verify_error(code, msg), "h")
    assert needle in text and "SYNTHETIC_CA_BUNDLE" in text and valid is False and len(text) < 490
    monkeypatch.setenv("SYNTHETIC_CA_BUNDLE", "/etc/ssl/corp.pem")
    text, _ = mod._describe_tls_error(_verify_error(code, msg), "h")
    assert needle in text and "SYNTHETIC_CA_BUNDLE" not in text


@pytest.mark.parametrize("code,msg", [(10, "certificate has expired"), (9, "not yet valid"),
                                      (62, "Hostname mismatch"), (7, "signature failure")])
def test_other_errors_never_get_the_hint(monkeypatch, code, msg):
    mod = _synthetic()
    monkeypatch.delenv("SYNTHETIC_CA_BUNDLE", raising=False)
    text, _ = mod._describe_tls_error(_verify_error(code, msg), "h")
    assert "SYNTHETIC_CA_BUNDLE" not in text
