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
  SMTP_SECURITY  starttls | ssl | none. Default: ssl on port 465, else
                  starttls. (Legacy SMTP_USE_TLS=true/false still works:
                  true=starttls, false=ssl.)
  SMTP_TIMEOUT   seconds per connection attempt (default 15)
  SMTP_EHLO_HOSTNAME  name announced in EHLO (default: the box's FQDN,
                  which on EC2 is an ip-172-31-... .internal name some
                  servers reject)
  MAIL_FROM_NAME  display-name brand for the From header (default "CloudOps"; the
                  name inside MAIL_FROM is ignored). Alert mail is sent as "<name> Alerts",
                  report mail as "<name> Reports". The address is always MAIL_FROM's.
  MAIL_TIMEZONE   zone times in emails are shown in (default Asia/Kolkata), with UTC beside it
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
import socket
import ssl
import time
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid, parseaddr

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
    return bool((os.getenv("SMTP_HOST") or "").strip())


def get_public_app_url() -> str:
    return os.getenv("PUBLIC_APP_URL", "http://localhost").rstrip("/")


def public_url_problem() -> str:
    """'' when links in emails are safe to click, else why not. Reset and set-password links carry a login token, so
    a plain-http link to anything but localhost sends that token across the network unencrypted."""
    url = get_public_app_url().lower()
    host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if url.startswith("http://") and host not in ("localhost", "127.0.0.1", "::1"):
        return ("PUBLIC_APP_URL is plain http: password-reset and set-password links travel unencrypted. "
                "Serve CloudOps over https (deploy/enable_tls.sh) and set PUBLIC_APP_URL=https://<host>")
    return ""


def _settings() -> dict:
    """Reads and normalises the SMTP env vars in one place.

    SMTP_SECURITY = starttls | ssl | none   (new; wins when set)
    SMTP_USE_TLS  = true -> starttls, false -> ssl  (legacy, kept so an
                    existing .env keeps working). With neither set, port
                    465 means ssl and anything else means starttls.
    """
    host = (os.getenv("SMTP_HOST") or "").strip()
    try:
        port = int((os.getenv("SMTP_PORT") or "587").strip())
    except ValueError:
        port = None
    sec = (os.getenv("SMTP_SECURITY") or "").strip().lower()
    if sec not in ("starttls", "ssl", "none"):
        legacy = (os.getenv("SMTP_USE_TLS") or "").strip().lower()
        if legacy == "true":
            sec = "starttls"
        elif legacy == "false":
            sec = "ssl"
        else:
            sec = "ssl" if port == 465 else "starttls"
    try:
        timeout = max(3, int((os.getenv("SMTP_TIMEOUT") or "15").strip()))
    except ValueError:
        timeout = 15
    username = (os.getenv("SMTP_USERNAME") or "").strip()
    return {
        "host": host, "port": port, "security": sec, "timeout": timeout,
        "username": username,
        "password": os.getenv("SMTP_PASSWORD") or "",
        "mail_from": ((os.getenv("MAIL_FROM") or os.getenv("SMTP_FROM") or username
                       or "cloudops@aurionpro.com").strip()),
        "ehlo": (os.getenv("SMTP_EHLO_HOSTNAME") or "").strip() or None,
    }


def config_problems() -> list:
    """Human-readable problems with the current SMTP config ([] = looks sane).
    Used by scripts/smtp_check.py; cheap enough to surface in a settings page."""
    c = _settings()
    out = []
    if not c["host"]:
        out.append("SMTP_HOST is not set")
        return out
    if c["port"] is None:
        out.append("SMTP_PORT is not an integer")
    if c["port"] == 465 and c["security"] == "starttls":
        out.append("port 465 is implicit TLS: set SMTP_SECURITY=ssl")
    if c["port"] in (587, 25) and c["security"] == "ssl":
        out.append(f"port {c['port']} expects STARTTLS: set SMTP_SECURITY=starttls")
    if c["username"] and not c["password"]:
        out.append("SMTP_USERNAME is set but SMTP_PASSWORD is empty")
    if c["password"] and not c["username"]:
        out.append("SMTP_PASSWORD is set but SMTP_USERNAME is empty")
    if "@" not in parseaddr(c["mail_from"])[1]:
        out.append(f"MAIL_FROM {c['mail_from']!r} is not a valid address")
    if c["security"] == "none" and c["username"]:
        out.append("SMTP_SECURITY=none with a username would send the password in clear text")
    if public_url_problem():
        out.append(public_url_problem())
    return out


def _envelope_sender(mail_from: str) -> str:
    """MAIL FROM must be the bare address; MAIL_FROM may be 'Name <a@b>'."""
    return parseaddr(mail_from)[1] or mail_from


def _from_header(mail_from: str, label: str = "") -> str:
    """'CloudOps Alerts <cloudops@aurionpro.com>'. The address is always MAIL_FROM's (SPF/DKIM are per address);
    only the display name changes. MAIL_FROM_NAME overrides the base name; `label` ('Alerts') is appended to it."""
    _name, addr = parseaddr(mail_from)
    base = (os.getenv("MAIL_FROM_NAME") or "").strip() or "CloudOps"
    if label:
        base = f"{base} {label}"
    if not addr:
        return mail_from
    base = _HEADER_INJECTION_RE.sub(" ", base)
    return formataddr((base, addr))


def _stamp(msg, mail_from: str, to_addr: str, subject: str, label: str = "") -> None:
    """Headers every mail needs. smtplib adds none of these, and a missing
    Date/Message-ID is a classic spam-filter penalty."""
    domain = _envelope_sender(mail_from).rpartition("@")[2] or None
    msg["Subject"] = subject
    msg["From"] = _from_header(mail_from, label)
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["Auto-Submitted"] = "auto-generated"   # stops auto-responders replying to us
    msg["X-Auto-Response-Suppress"] = "All"    # Exchange/Outlook: no out-of-office or read-receipt replies


def _build_body(body_text: str, html_body=None):
    """Plain text alone, or multipart/alternative (text first, HTML last: clients show the last part they understand)."""
    if not html_body:
        return MIMEText(body_text, "plain", "utf-8")
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body_text, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    return alt


def send_email_ex(to_addr: str, subject: str, body_text: str, html_body=None, sender_label: str = ""):
    """Like send_email() but returns (ok, error). `error` is a short,
    secret-free reason ('' on success) that callers can store in a log
    table or show an admin instead of 'check the server log'.
    html_body (optional) adds an HTML alternative; body_text stays the fallback.
    sender_label ('Alerts') is appended to the From display name."""
    try:
        _reject_header_injection(to_addr, "to_addr")
        _reject_header_injection(subject, "subject")
    except ValueError as e:
        logger.error(f"Refusing to send mail -- {e} (to_addr={to_addr!r}, subject={subject!r})")
        return False, "recipient or subject contains a line break"
    if not is_configured():
        logger.warning(
            f"Mail not sent to {to_addr!r} (subject={subject!r}) -- SMTP_HOST is not set. "
            "Configure SMTP_HOST/SMTP_PORT/SMTP_USERNAME/SMTP_PASSWORD/MAIL_FROM in .env to enable email."
        )
        return False, "SMTP is not configured (SMTP_HOST unset)"
    c = _settings()
    if c["port"] is None:
        logger.error(f"Mail not sent to {to_addr!r} -- SMTP_PORT is not an integer")
        return False, "SMTP_PORT is not an integer"
    msg = _build_body(body_text, html_body)
    _stamp(msg, c["mail_from"], to_addr, subject, sender_label)
    ok, err = _deliver(c, to_addr, msg)
    if ok:
        logger.info(f"Mail sent to {to_addr!r} (subject={subject!r})")
    else:
        logger.error(f"Mail send failed to {to_addr!r} (subject={subject!r}): {err}")
    return ok, err


def send_email(to_addr: str, subject: str, body_text: str, html_body=None, sender_label: str = "") -> bool:
    """Sends a plain-text email. Returns True on success, False if SMTP
    isn't configured or the send failed (always logged, never raises --
    callers treat False as "not sent, fall back to non-email behaviour").
    CR/LF in to_addr/subject is refused (header/envelope injection, CWE-93)
    and treated like any other failure so it can never take down a caller.
    """
    return send_email_ex(to_addr, subject, body_text, html_body, sender_label)[0]


def _short_err(exc: Exception) -> str:
    """One-line, secret-free description of an SMTP failure."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return f"login rejected by the mail server ({exc.smtp_code}) -- check SMTP_USERNAME/SMTP_PASSWORD"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return f"sender {exc.sender!r} refused ({exc.smtp_code}) -- MAIL_FROM must be a verified/allowed address"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "recipient refused by the mail server: " + "; ".join(
            f"{r} ({c})" for r, (c, _m) in exc.recipients.items())
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return f"server does not support what was asked: {exc} -- check SMTP_SECURITY / port"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timed out connecting to the mail server (firewall / security-group egress?)"
    if isinstance(exc, socket.gaierror):
        return "SMTP_HOST could not be resolved (DNS)"
    if isinstance(exc, ssl.SSLError):
        return f"TLS error: {getattr(exc, "reason", None) or exc} -- wrong SMTP_SECURITY for this port, or certificate not trusted"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused -- wrong host/port"
    return f"{type(exc).__name__}: {str(exc)[:200]}"


_PERMANENT_NO_RETRY = (smtplib.SMTPAuthenticationError, smtplib.SMTPSenderRefused,
                       smtplib.SMTPRecipientsRefused, smtplib.SMTPNotSupportedError,
                       ssl.SSLError, socket.gaierror)


def _deliver(c: dict, to_addr: str, msg):
    """One send with a single retry for transient network trouble.
    Returns (ok, error). Never raises."""
    last = ""
    for attempt in (1, 2):
        try:
            _send_mime(c["host"], c["port"], c["username"], c["password"], c["security"],
                       _envelope_sender(c["mail_from"]), to_addr, msg,
                       timeout=c["timeout"], ehlo=c["ehlo"])
            return True, ""
        except _PERMANENT_NO_RETRY as e:
            return False, _short_err(e)
        except smtplib.SMTPResponseException as e:
            last = _short_err(e)
            if e.smtp_code < 400 or e.smtp_code >= 500:   # 5xx = permanent
                return False, last
        except Exception as e:
            last = _short_err(e)
        if attempt == 1:
            time.sleep(2)
    return False, last


def _send_mime(host, port, username, password, security, envelope_from, to_addr, msg,
               timeout=15, ehlo=None) -> None:
    """`security` is 'starttls' | 'ssl' | 'none'. A bool is still accepted
    (True=starttls, False=ssl) for any old caller."""
    if security is True:
        security = "starttls"
    elif security is False:
        security = "ssl"
    kw = {"local_hostname": ehlo} if ehlo else {}
    ctx = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx, **kw)
    else:
        server = smtplib.SMTP(host, port, timeout=timeout, **kw)
    with server:
        if security == "starttls":
            server.ehlo()
            server.starttls(context=ctx)
            server.ehlo()
        if username:
            server.login(username, password)
        server.sendmail(envelope_from, [to_addr], msg.as_string())


def send_report_email(to_addr: str, report: dict, pdf_bytes: bytes) -> bool:
    """Sends a generated report as a PDF attachment. Same fail-safe
    contract as send_email(): never raises, returns False (logged) if
    SMTP isn't configured or the send fails. Callers (app/api/
    reports.py) are expected to check mailer.is_configured() first and
    surface a clear 501 to the caller rather than a generic failure --
    this function itself stays silent-safe for consistency with
    send_email()'s existing contract.
    """
    try:
        _reject_header_injection(to_addr, "to_addr")
    except ValueError as e:
        logger.error(f"Refusing to email report -- {e} (to_addr={to_addr!r})")
        return False

    if not is_configured():
        logger.warning(f"Report email not sent to {to_addr!r} -- SMTP_HOST is not set.")
        return False

    c = _settings()
    if c["port"] is None:
        logger.error(f"Report email not sent to {to_addr!r} -- SMTP_PORT is not an integer")
        return False

    from app.email import templates
    filename = templates.report_attachment_name(report)
    mail = templates.report_email(report, filename=filename, size_bytes=len(pdf_bytes or b""), app_url=get_public_app_url())
    subject = mail["subject"]
    try:
        # scope_label is user-supplied; refuse CR/LF in anything that lands in a header
        _reject_header_injection(subject, "subject")
        _reject_header_injection(filename, "filename")
    except ValueError as e:
        logger.error(f"Refusing to email report -- {e} (to_addr={to_addr!r})")
        return False

    msg = MIMEMultipart("mixed")
    _stamp(msg, c["mail_from"], to_addr, subject, "Reports")
    msg.attach(_build_body(mail["text"], mail["html"]))
    attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
    attachment.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(attachment)

    ok, err = _deliver(c, to_addr, msg)
    if ok:
        logger.info(f"Report email sent to {to_addr!r} (subject={subject!r})")
    else:
        logger.error(f"Report email send failed to {to_addr!r}: {err}")
    return ok
