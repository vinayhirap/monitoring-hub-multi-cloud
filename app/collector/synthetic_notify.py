# app/collector/synthetic_notify.py
"""
Who is told when a synthetic check goes down or its TLS certificate is about to expire.

Before this module a synthetic outage or an expiring certificate only produced an alert ROW: it showed on the
Alerts page, but nothing ever sent an email, Slack or Teams message (only threshold alerts and escalations did).
Now:

  * every check has its own CONTACTS (synthetic_check_contacts): email addresses, each limited to outages and / or
    certificate expiry, so the person who owns a website can differ from the person who owns the next one;
  * the notification channels an admin already configured (Settings > Notifications) receive synthetic alerts as well,
    exactly like any other alert (same severity / account / event rules);
  * certificate notices follow a state machine (synthetic_notification_state), so nobody is spammed:

        first notice          the first time a certificate is inside its warning window
        escalated             it moves warning -> critical -> expired: sent immediately
        reminder              same stage again: warning every 7 days, critical / expired every day (contacts only)
        renewed               a certificate with a later expiry replaces the old one: one "all clear"

    A stage is recorded as sent only when at least one message went out, so a missing SMTP setup or a check with no
    contacts yet does not swallow the notice: it is sent at the next probe after that is fixed.

Safety: nothing here can raise into the probe loop (every entry point catches), maintenance windows silence it, and
addresses are validated and CR/LF-checked again by the mailer.
"""
import logging
import os
from datetime import datetime, timedelta

from app.email import mailer

logger = logging.getLogger(__name__)

STAGE_RANK = {"warn": 1, "crit": 2, "expired": 3}


def _int_env(name, default):
    try:
        v = int((os.getenv(name) or "").strip())
        return v if v >= 1 else default
    except ValueError:
        return default


def reminder_days(stage):
    """Days between reminders at a stage (env SYNTHETIC_CERT_REMINDER_WARN_DAYS / _CRIT_DAYS)."""
    return _int_env("SYNTHETIC_CERT_REMINDER_WARN_DAYS", 7) if stage == "warn" \
        else _int_env("SYNTHETIC_CERT_REMINDER_CRIT_DAYS", 1)


# ── pure decisions (tested directly) ────────────────────────────────

def cert_stage(days_left, warn, crit):
    """whole days left -> None | 'warn' | 'crit' | 'expired'. cert_days_left is floored, so 0 means "less than a day
    left" (still critical) and only a negative number means the certificate has expired."""
    if days_left is None:
        return None
    if days_left < 0:
        return "expired"
    if days_left <= crit:
        return "crit"
    if days_left <= warn:
        return "warn"
    return None


def cert_action(prev, stage, now):
    """prev: None or {'stage', 'last_sent_at'} for THIS certificate. -> 'first' | 'escalated' | 'reminder' | None."""
    if stage is None:
        return None
    if prev is None:
        return "first"
    if STAGE_RANK[stage] > STAGE_RANK.get(prev["stage"], 0):
        return "escalated"
    if STAGE_RANK[stage] == STAGE_RANK.get(prev["stage"], 0) \
            and now - prev["last_sent_at"] >= timedelta(days=reminder_days(stage)):
        return "reminder"
    return None


def _key(not_after):
    if isinstance(not_after, datetime):
        return not_after.strftime("%Y-%m-%d %H:%M:%S")
    return str(not_after or "")[:19]


def _host(check):
    t = str(check.get("target") or "")
    return t.split("://", 1)[-1].split("/", 1)[0]


def _app_link(path):
    base = mailer.get_public_app_url()
    return f"{base}{path}" if base else ""


_STAGE_WORDS = {"warn": "WARNING", "crit": "CRITICAL", "expired": "EXPIRED"}


def build_cert_message(check, stage, action, days_left, tls, account_name=""):
    """-> (subject, body). Plain text, no secrets, names the check, the host, the date and what to do."""
    name, host = check.get("name") or "check", _host(check)
    when = tls.get("cert_not_after")
    when_text = when.strftime("%Y-%m-%d %H:%M UTC") if isinstance(when, datetime) else str(when or "unknown")
    if stage == "expired":
        head = f"The TLS certificate for {host} has EXPIRED"
        subject = f"[CloudOps] EXPIRED certificate: {name} ({host})"
    else:
        n = max(days_left, 0)
        unit = "day" if n == 1 else "days"
        head = f"The TLS certificate for {host} expires in {n} {unit}" if n else f"The TLS certificate for {host} expires today"
        subject = f"[CloudOps] {_STAGE_WORDS[stage]}: certificate for {name} expires in {n} {unit}"
    if action == "reminder":
        subject = "Reminder: " + subject
    lines = [head + ".", "",
             f"Check:        {name}",
             f"Target:       {check.get('target')}",
             f"Account:      {account_name or check.get('aws_account_id')}",
             f"Environment:  {check.get('environment') or '-'}",
             f"Expires on:   {when_text}",
             f"Issuer:       {tls.get('cert_issuer') or '-'}",
             f"Subject:      {tls.get('cert_subject') or '-'}",
             f"Severity:     {_STAGE_WORDS[stage]}", "",
             "What to do: renew or replace the certificate and install it on the server. CloudOps re-checks every probe "
             "and will tell you when it sees the new certificate."]
    link = _app_link("/synthetic-checks")
    if link:
        lines += ["", f"Open in CloudOps: {link}"]
    return subject, "\n".join(lines)


def build_renewed_message(check, tls, account_name=""):
    name, host = check.get("name") or "check", _host(check)
    when = tls.get("cert_not_after")
    when_text = when.strftime("%Y-%m-%d %H:%M UTC") if isinstance(when, datetime) else str(when or "unknown")
    subject = f"[CloudOps] Certificate renewed: {name} ({host})"
    body = (f"CloudOps now sees a new TLS certificate on {host}.\n\n"
            f"Check:        {name}\nAccount:      {account_name or check.get('aws_account_id')}\n"
            f"New expiry:   {when_text}\nIssuer:       {tls.get('cert_issuer') or '-'}\n\n"
            "No further action is needed.")
    return subject, body


def build_outage_message(check, down, error=None, minutes=None, account_name=""):
    name = check.get("name") or "check"
    if down:
        subject = f"[CloudOps] DOWN: {name}"
        lines = [f"{name} is DOWN.", "",
                 f"Target:       {check.get('target')}",
                 f"Account:      {account_name or check.get('aws_account_id')}",
                 f"Environment:  {check.get('environment') or '-'}",
                 f"Failed probes in a row: {check.get('consecutive_failures')}"]
        if error:
            lines.append(f"Last error:   {str(error)[:300]}")
        link = _app_link("/synthetic-checks")
        if link:
            lines += ["", f"Open in CloudOps: {link}"]
        return subject, "\n".join(lines)
    subject = f"[CloudOps] Recovered: {name}"
    body = f"{name} is back UP.\n\nTarget:       {check.get('target')}\nAccount:      {account_name or check.get('aws_account_id')}"
    if minutes is not None:
        body += f"\nDown for about {minutes} minute(s)."
    return subject, body


# ── delivery ─────────────────────────────────────────────────────────

def _mask(addr):
    local, _, domain = str(addr).partition("@")
    return f"{local[:1]}***@{domain}" if domain else "(hidden)"


def _contacts(cursor, check_id, topic):
    col = "notify_cert" if topic == "cert" else "notify_outage"
    cursor.execute(f"SELECT email FROM synthetic_check_contacts WHERE check_id = %s AND {col} = 1 ORDER BY id", (check_id,))
    seen, out = set(), []
    for r in cursor.fetchall():
        e = (r["email"] or "").strip()
        if e and e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return out


def _log(cursor, alert_id, event, status, detail=None):
    try:
        cursor.execute(
            "INSERT INTO notification_log (channel_id, channel_name, alert_id, event, status, detail) "
            "VALUES (NULL, 'check contact', %s, %s, %s, %s)", (alert_id, event, status, detail))
    except Exception:
        logger.debug("[synthetic-notify] could not write notification_log", exc_info=True)


def send_to_contacts(cursor, check_id, topic, subject, body, alert_id=None, event="opened"):
    """Emails the check's contacts for this topic. -> number of messages sent. Never raises."""
    try:
        addrs = _contacts(cursor, check_id, topic)
    except Exception:
        logger.exception("[synthetic-notify] could not read contacts for check %s", check_id)
        return 0
    if not addrs:
        return 0
    if not mailer.is_configured():
        _log(cursor, alert_id, event, "skipped", "SMTP is not configured (SMTP_HOST unset)")
        return 0
    sent = 0
    for a in addrs:
        try:
            if mailer.send_email(a, subject, body):
                sent += 1
                _log(cursor, alert_id, event, "sent", _mask(a))
            else:
                _log(cursor, alert_id, event, "failed", f"{_mask(a)}: not accepted by the mail server")
        except Exception as exc:
            _log(cursor, alert_id, event, "failed", f"{_mask(a)}: {type(exc).__name__}")
    return sent


def _alert_for_channels(cursor, check, metric):
    cursor.execute("""
        SELECT a.id, a.severity, a.current_value AS value, a.threshold, acc.account_name
        FROM alerts a JOIN aws_accounts acc ON acc.id = a.aws_account_id
        WHERE a.aws_account_id = %s AND a.resource_id = %s AND a.metric_name = %s
          AND a.status IN ('active', 'acknowledged')
        ORDER BY a.id DESC LIMIT 1""", (check["aws_account_id"], f"synthetic-{check['id']}", metric))
    return cursor.fetchone()


def _fan_out_channels(cursor, check, metric, event):
    """The globally configured channels get synthetic alerts like any other alert. Returns (alert_id, sent)."""
    try:
        a = _alert_for_channels(cursor, check, metric)
        if not a:
            return None, 0
        from app.notifications.sender import notify_alert_event
        sent = notify_alert_event(event, {
            "id": a["id"], "severity": a["severity"], "metric_name": metric,
            "resource_id": f"synthetic-{check['id']} ({check.get('name')})", "account_name": a["account_name"],
            "aws_account_id": check["aws_account_id"], "value": a["value"], "threshold": a["threshold"],
            "silenced": False,
        })
        return a["id"], sent
    except Exception:
        logger.exception("[synthetic-notify] channel fan-out failed for check %s", check.get("id"))
        return None, 0


def _account_name(cursor, check):
    try:
        cursor.execute("SELECT account_name FROM aws_accounts WHERE id = %s", (check["aws_account_id"],))
        r = cursor.fetchone()
        return (r or {}).get("account_name") or ""
    except Exception:
        return ""


# ── entry points called by the probe loop (after the check's own commit) ──

def on_outage_change(conn, cursor, check, went_down, error=None, silenced=None):
    """went_down True = just crossed the failure threshold; False = just recovered. Never raises."""
    try:
        if silenced:
            return
        acct = _account_name(cursor, check)
        if went_down:
            alert_id, _ = _fan_out_channels(cursor, check, "synthetic_uptime", "opened")
            subject, body = build_outage_message(check, True, error, account_name=acct)
            send_to_contacts(cursor, check["id"], "outage", subject, body, alert_id, "opened")
        else:
            cursor.execute("""SELECT TIMESTAMPDIFF(MINUTE, triggered_at, COALESCE(resolved_at, UTC_TIMESTAMP())) AS m
                              FROM alerts WHERE aws_account_id = %s AND resource_id = %s AND metric_name = 'synthetic_uptime'
                              ORDER BY id DESC LIMIT 1""", (check["aws_account_id"], f"synthetic-{check['id']}"))
            r = cursor.fetchone()
            subject, body = build_outage_message(check, False, minutes=(r or {}).get("m"), account_name=acct)
            send_to_contacts(cursor, check["id"], "outage", subject, body, None, "recovered")
        conn.commit()
    except Exception:
        logger.exception("[synthetic-notify] outage notice failed for check %s", check.get("id"))
        try:
            conn.rollback()
        except Exception:
            pass


def on_certificate(conn, cursor, check, tls, warn, crit, silenced=None, now=None):
    """Applies the notice state machine for the certificate seen on this probe. Never raises."""
    try:
        days_left, not_after = (tls or {}).get("cert_days_left"), (tls or {}).get("cert_not_after")
        if days_left is None or not not_after:
            return                                   # no certificate seen: unknown is not 'renewed' and not 'expiring'
        now = now or datetime.utcnow()
        key = _key(not_after)
        stage = cert_stage(days_left, warn, crit)
        cursor.execute("SELECT subject_key, stage, last_sent_at FROM synthetic_notification_state "
                       "WHERE check_id = %s AND kind = 'cert' ORDER BY subject_key DESC", (check["id"],))
        rows = cursor.fetchall()
        current = next((r for r in rows if r["subject_key"] == key), None)
        older = [r for r in rows if r["subject_key"] < key]

        if older and current is None and not silenced:
            # a certificate that expires LATER than every one we warned about: it was renewed
            acct = _account_name(cursor, check)
            subject, body = build_renewed_message(check, tls, acct)
            if send_to_contacts(cursor, check["id"], "cert", subject, body, None, "renewed"):
                cursor.execute("DELETE FROM synthetic_notification_state WHERE check_id = %s AND kind = 'cert' "
                               "AND subject_key < %s", (check["id"], key))

        action = cert_action(current, stage, now)
        if action is None or silenced:
            conn.commit()
            return
        acct = _account_name(cursor, check)
        subject, body = build_cert_message(check, stage, action, days_left, tls, acct)
        delivered = 0
        alert_id = None
        if action in ("first", "escalated"):
            alert_id, via_channels = _fan_out_channels(cursor, check, "synthetic_cert_expiry",
                                                       "opened" if action == "first" else "escalated")
            delivered += via_channels
        delivered += send_to_contacts(cursor, check["id"], "cert", subject, body, alert_id, "opened" if action == "first" else action)
        if delivered:
            cursor.execute("""
                INSERT INTO synthetic_notification_state (check_id, kind, subject_key, stage, last_sent_at, sent_count)
                VALUES (%s, 'cert', %s, %s, %s, 1)
                ON DUPLICATE KEY UPDATE stage = VALUES(stage), last_sent_at = VALUES(last_sent_at),
                                        sent_count = sent_count + 1""", (check["id"], key, stage, now))
        conn.commit()
    except Exception:
        logger.exception("[synthetic-notify] certificate notice failed for check %s", check.get("id"))
        try:
            conn.rollback()
        except Exception:
            pass
