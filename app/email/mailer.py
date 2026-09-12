# app/email/mailer.py
"""
app/email/mailer.py

Minimal SMTP mail sender, stdlib-only (smtplib + email.mime) -- no new
pip dependency required. Configured entirely via environment
variables, matching this app's existing convention (DB_HOST,
JWT_SECRET, VM_URL, ...) of env-var config rather than a DB-backed
settings table.

Required env vars to actually send mail:
  SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, MAIL_FROM

Optional:
  SMTP_USE_TLS   ("true"/"false", default "true" -- STARTTLS on the
                  normal submission port 587; set to "false" only if
                  using implicit TLS on port 465 instead)
  PUBLIC_APP_URL (used to build links in email bodies, e.g. the
                  password-reset link; default "http://localhost" --
                  set this to the server's real public URL)

If SMTP_HOST is unset, is_configured() returns False and send_email()
is a safe no-op that logs a warning and returns False. Every caller in
this app is written to fall back to its pre-mail behavior (e.g.
returning a reset token directly in the API response instead of
emailing it) rather than break when mail isn't configured -- the same
degrade-gracefully-never-crash pattern already used for VM/YACE
metrics elsewhere in this app.
"""
import logging
import os
import re
import smtplib
import ssl
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

# Defense-in-depth against CRLF/header injection (CWE-93): every current
# caller (app/api/auth.py's forgot-password, app/api/admin/users.py's
# welcome email) passes a hardcoded subject string, but to_addr always
# comes from the users.email column, which create_user() only
# .strip()s -- no format validation, no rejection of embedded control
# characters. Without this check here, an editor creating a user could
# put a CRLF sequence into the email field and have it land verbatim
# in both the MIME "To" header (header injection -- e.g. smuggling in
# an extra Bcc: line) AND smtplib.sendmail's raw envelope RCPT TO
# argument (the more severe case: some smtplib/server combinations
# would treat embedded CRLF there as SMTP command injection).
# Centralized here rather than only at create_user's input boundary so
# every current AND future caller is protected uniformly, matching
# this app's own "centralized, reusable security controls over
# duplicated fixes" convention (see app/auth/authorization.py's
# docstring for the same principle applied to RBAC).
_HEADER_INJECTION_RE = re.compile(r"[\r\n]")


def _reject_header_injection(value: str, field_name: str) -> None:
    if _HEADER_INJECTION_RE.search(value):
        raise ValueError(f"{field_name} contains a disallowed control character (CR/LF)")


def is_configured() -> bool:
    return bool(os.getenv("SMTP_HOST"))


def get_public_app_url() -> str:
    return os.getenv("PUBLIC_APP_URL", "http://localhost").rstrip("/")


def send_email(to_addr: str, subject: str, body_text: str) -> bool:
    """
    Sends a plain-text email. Returns True on success, False if SMTP
    isn't configured or the send failed (always logged, never raises
    -- callers should treat a False return as "email not sent, fall
    back to your existing non-email behavior", not as an error to
    surface to the end user).

    Never raises -- if to_addr or subject contain embedded CR/LF (which
    would otherwise be a header/envelope injection vulnerability: that
    value lands verbatim in both the MIME "To" header and smtplib's raw
    envelope RCPT TO argument), this is treated the same as any other
    send failure: logged at error level and False is returned, so a
    malicious/malformed value can never reach the wire but also never
    take down the caller's own request (e.g. create_user's account
    creation must still succeed even if the just-typed email is bad;
    the caller can decide separately whether to reject the value up
    front -- see admin/users.py's create_user for the input-side check).
    """
    try:
        _reject_header_injection(to_addr, "to_addr")
        _reject_header_injection(subject, "subject")
    except ValueError as e:
        logger.error(f"Refusing to send mail -- {e} (to_addr={to_addr!r}, subject={subject!r})")
        return False

    if not is_configured():
        logger.warning(
            f"Mail not sent to {to_addr!r} (subject={subject!r}) -- SMTP_HOST is not set. "
            "Configure SMTP_HOST/SMTP_PORT/SMTP_USERNAME/SMTP_PASSWORD/MAIL_FROM in .env to enable email."
        )
        return False

    host      = os.getenv("SMTP_HOST")
    port      = int(os.getenv("SMTP_PORT", "587"))
    username  = os.getenv("SMTP_USERNAME", "")
    password  = os.getenv("SMTP_PASSWORD", "")
    use_tls   = os.getenv("SMTP_USE_TLS", "true").strip().lower() == "true"
    mail_from = os.getenv("MAIL_FROM", username or "cloudops@aurionpro.com")

    msg = MIMEText(body_text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = mail_from
    msg["To"]      = to_addr

    try:
        if use_tls:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.starttls(context=ssl.create_default_context())
                if username:
                    server.login(username, password)
                server.sendmail(mail_from, [to_addr], msg.as_string())
        else:
            with smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context()) as server:
                if username:
                    server.login(username, password)
                server.sendmail(mail_from, [to_addr], msg.as_string())
        logger.info(f"Mail sent to {to_addr!r} (subject={subject!r})")
        return True
    except Exception as e:
        logger.error(f"Mail send failed to {to_addr!r} (subject={subject!r}): {e}")
        return False
