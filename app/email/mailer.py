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
import smtplib
import ssl
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


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
    """
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
