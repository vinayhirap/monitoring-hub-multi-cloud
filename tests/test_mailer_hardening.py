"""Mailer: security mode resolution, headers, envelope sender, error text, retry policy."""
import smtplib
import ssl
import importlib.util
import pathlib

_p = pathlib.Path(__file__).resolve().parent.parent / "app" / "email" / "mailer.py"
_spec = importlib.util.spec_from_file_location("mailer_under_test", _p)
mailer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mailer)

ENV = ["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_USE_TLS", "SMTP_SECURITY",
       "SMTP_TIMEOUT", "SMTP_EHLO_HOSTNAME", "MAIL_FROM", "SMTP_FROM"]


def _env(monkeypatch, **kw):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_security_defaults_by_port(monkeypatch):
    _env(monkeypatch, SMTP_HOST="h", SMTP_PORT="465")
    assert mailer._settings()["security"] == "ssl"
    _env(monkeypatch, SMTP_HOST="h", SMTP_PORT="587")
    assert mailer._settings()["security"] == "starttls"


def test_legacy_use_tls_and_new_var_precedence(monkeypatch):
    _env(monkeypatch, SMTP_HOST="h", SMTP_USE_TLS="false")
    assert mailer._settings()["security"] == "ssl"
    _env(monkeypatch, SMTP_HOST="h", SMTP_USE_TLS="false", SMTP_SECURITY="none")
    assert mailer._settings()["security"] == "none"


def test_config_problems(monkeypatch):
    _env(monkeypatch, SMTP_HOST="h", SMTP_PORT="465", SMTP_SECURITY="starttls", SMTP_USERNAME="u")
    probs = " | ".join(mailer.config_problems())
    assert "465" in probs and "SMTP_PASSWORD" in probs
    _env(monkeypatch, SMTP_HOST="h", SMTP_PORT="587", SMTP_USERNAME="u", SMTP_PASSWORD="p", MAIL_FROM="a@b.com")
    assert mailer.config_problems() == []


def test_envelope_sender_strips_display_name():
    assert mailer._envelope_sender("CloudOps <alerts@x.com>") == "alerts@x.com"
    assert mailer._envelope_sender("alerts@x.com") == "alerts@x.com"


def test_message_has_date_and_message_id():
    m = mailer.MIMEText("hi", "plain", "utf-8")
    mailer._stamp(m, "CloudOps <alerts@x.com>", "a@b.com", "Subject é")
    assert m["Date"] and m["Message-ID"].endswith("@x.com>")
    assert "Subject" in m.as_string()


def test_send_email_ex_not_configured(monkeypatch):
    _env(monkeypatch)
    assert mailer.send_email_ex("a@b.com", "s", "b") == (False, "SMTP is not configured (SMTP_HOST unset)")


def test_crlf_refused(monkeypatch):
    _env(monkeypatch, SMTP_HOST="h")
    ok, err = mailer.send_email_ex("a@b.com\r\nBcc: x@y.com", "s", "b")
    assert not ok and "line break" in err


def test_success_path_uses_bare_envelope_sender(monkeypatch):
    _env(monkeypatch, SMTP_HOST="h", SMTP_PORT="587", MAIL_FROM="CloudOps <alerts@x.com>")
    seen = {}
    monkeypatch.setattr(mailer, "_send_mime", lambda *a, **k: seen.update(env_from=a[5], to=a[6], kw=k))
    assert mailer.send_email("a@b.com", "s", "b") is True
    assert seen["env_from"] == "alerts@x.com" and seen["kw"]["timeout"] == 15


def test_auth_failure_is_not_retried_and_message_is_helpful(monkeypatch):
    _env(monkeypatch, SMTP_HOST="h")
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise smtplib.SMTPAuthenticationError(535, b"nope secret-text")
    monkeypatch.setattr(mailer, "_send_mime", boom)
    ok, err = mailer.send_email_ex("a@b.com", "s", "b")
    assert not ok and len(calls) == 1 and "SMTP_PASSWORD" in err and "secret-text" not in err


def test_transient_error_retried_once(monkeypatch):
    _env(monkeypatch, SMTP_HOST="h")
    monkeypatch.setattr(mailer.time, "sleep", lambda s: None)
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise smtplib.SMTPServerDisconnected("gone")
    monkeypatch.setattr(mailer, "_send_mime", flaky)
    assert mailer.send_email("a@b.com", "s", "b") is True and len(calls) == 2


def test_permanent_5xx_not_retried(monkeypatch):
    _env(monkeypatch, SMTP_HOST="h")
    calls = []

    def rej(*a, **k):
        calls.append(1)
        raise smtplib.SMTPDataError(554, b"rejected")
    monkeypatch.setattr(mailer, "_send_mime", rej)
    assert mailer.send_email("a@b.com", "s", "b") is False and len(calls) == 1


def test_tls_error_message(monkeypatch):
    assert "SMTP_SECURITY" in mailer._short_err(ssl.SSLError(1, "WRONG_VERSION_NUMBER"))
