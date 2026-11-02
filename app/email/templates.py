# app/email/templates.py
"""
One look for every CloudOps email.

Every builder returns {"subject", "text", "html"}:
  * html  -- table layout with inline CSS only (Outlook-safe: no flex/grid, no <style> dependence, no web fonts),
             a hidden preheader, a bulletproof button, and EVERY dynamic value passed through html.escape().
  * text  -- the same content as plain text (the multipart/alternative fallback, and what text-only clients show).

Nothing here sends mail or touches the database; app/email/mailer.py sends, callers just pick a builder.
Builders never raise on odd input (None, missing keys, huge strings): a mail must not be the thing that breaks an alert.

Timestamps are shown in the display zone (MAIL_TIMEZONE, default Asia/Kolkata) with UTC beside it, so the time on
the mail matches the clock the reader sees and can be matched to the log.
"""
import html as _html
import os
import re
from datetime import datetime, timedelta, timezone

BRAND = "CloudOps"
ORG = "Aurionpro"

# severity / state -> (accent colour, soft background, label)
ACCENTS = {
    "CRITICAL": ("#C62828", "#FDECEA"),
    "WARNING":  ("#B26A00", "#FFF4E0"),
    "INFO":     ("#0F766E", "#E6F4F2"),
    "OK":       ("#2E7D32", "#E8F5E9"),
    "NEUTRAL":  ("#37474F", "#ECEFF1"),
}
_FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MONO = "Consolas,'SFMono-Regular',Menlo,monospace"
_URL_OK = re.compile(r"^https?://[^\s<>\"']+$", re.I)


def esc(value) -> str:
    return _html.escape("" if value is None else str(value), quote=True)


def _clip(value, n=300) -> str:
    s = "" if value is None else str(value)
    s = re.sub(r"[\r\n\t]+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ── time ─────────────────────────────────────────────────────────────

def _display_tz():
    name = (os.getenv("MAIL_TIMEZONE") or "Asia/Kolkata").strip()
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), (os.getenv("MAIL_TIMEZONE_LABEL") or ("IST" if name == "Asia/Kolkata" else name))
    except Exception:
        return timezone(timedelta(hours=5, minutes=30)), "IST"


def _as_utc(dt):
    if isinstance(dt, str):
        s = dt.strip().replace(" ", "T", 1)
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(dt, datetime):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def fmt_when(dt, with_utc=True) -> str:
    """'02 Nov 2026, 11:33 IST (06:03 UTC)'. Naive datetimes are UTC (the project convention). '' if unparseable."""
    u = _as_utc(dt)
    if not u:
        return ""
    tz, label = _display_tz()
    local = u.astimezone(tz)
    out = f"{local:%d %b %Y, %H:%M} {label}"
    return f"{out} ({u:%H:%M} UTC)" if with_utc and label != "UTC" else out


def fmt_day(dt) -> str:
    """Date only, '22 Oct 2026'. Used for report periods, whose stored clock time carries no zone."""
    if isinstance(dt, datetime):
        return f"{dt:%d %b %Y}"
    if isinstance(dt, str):
        try:
            return f"{datetime.fromisoformat(dt.strip().replace(' ', 'T', 1)[:19]):%d %b %Y}"
        except ValueError:
            return _clip(dt, 24)
    return ""


def fmt_period(start, end) -> str:
    a, b = fmt_day(start), fmt_day(end)
    if not a or not b:
        return a or b
    if a[3:] == b[3:]:                                   # same month and year: '22–29 Oct 2026'
        return f"{a[:2]}–{b}"
    return f"{a} – {b}"


# ── resource / metric wording ────────────────────────────────────────

_HEXISH = re.compile(r"^[0-9a-f]{12,}$", re.I)


def short_resource(resource_id) -> str:
    """The name a person would recognise. 'arn:aws:elasticloadbalancing:...:loadbalancer/app/xrai-alb/ed01...' -> 'xrai-alb'.
    'synthetic-3 (Payment API)' -> 'Payment API'. Anything else is returned unchanged (clipped)."""
    rid = _clip(resource_id, 400)
    if not rid:
        return ""
    if rid.startswith("synthetic-") and "(" in rid:
        return rid.split("(", 1)[1].rstrip(") ").strip() or rid
    if rid.startswith("arn:"):
        tail = rid.split(":", 5)[-1]
        if tail.count(":") == 1 and "/" not in tail:                      # 'db:mydb', 'function:my-fn'
            return tail.split(":", 1)[1] or rid
        parts = [p for p in tail.split("/") if p]
        if len(parts) >= 2 and _HEXISH.match(parts[-1]):                  # drop the trailing load-balancer / target id
            parts = parts[:-1]
        return parts[-1] if parts else rid
    return rid


def metric_text(metric_name) -> str:
    try:
        from app.metric_labels import metric_label
        return metric_label(metric_name) or str(metric_name or "")
    except Exception:
        return str(metric_name or "")


_UNIT_SUFFIX = {"Seconds": " s", "Milliseconds": " ms", "Bytes": " B", "Microseconds": " µs"}


def value_text(metric_name, v) -> str:
    """121 -> '121', 87.5 on a percent metric -> '87.5%', cost metric -> '$12.34'. Never raises."""
    if v is None or v == "":
        return "-"
    if str(metric_name or "") == "cloud_cost_jump":
        try:
            return f"${float(v):,.2f}"
        except (TypeError, ValueError):
            return str(v)
    try:
        from app.metric_labels import format_metric_value, metric_unit, metric_unit_name
        out = format_metric_value(metric_name, v, grouped=True)
        pct = metric_unit(metric_name)
        return f"{out}{pct}" if pct else f"{out}{_UNIT_SUFFIX.get(metric_unit_name(metric_name), '')}"
    except Exception:
        return str(v)


def _link(app_url, path) -> str:
    base = (app_url or "").rstrip("/")
    url = f"{base}{path}" if base else ""
    return url if _URL_OK.match(url) else ""


# ── layout ───────────────────────────────────────────────────────────

def render(*, preheader="", eyebrow="", title="", accent="INFO", intro=None, facts=(), paragraphs=(),
           button=None, fallback_note="", footer="", chip=None):
    """-> (html, text). facts: [(label, value)] or [(label, value, muted_second_line)]. button: (label, url) or None.
    chip: short coloured tag shown above the title (e.g. 'CRITICAL')."""
    color, soft = ACCENTS.get(accent, ACCENTS["INFO"])
    fact_rows = []
    for f in facts:
        if not f or f[1] in (None, ""):
            continue
        label, value = f[0], f[1]
        sub = f[2] if len(f) > 2 else ""
        fact_rows.append((str(label), str(value), str(sub or "")))

    h = []
    h.append(f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
             f'<meta name="viewport" content="width=device-width,initial-scale=1">'
             f'<meta name="color-scheme" content="light"><title>{esc(title)}</title></head>')
    h.append('<body style="margin:0;padding:0;background:#F3F5F7;">')
    h.append(f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;mso-hide:all;">'
             f'{esc(preheader)}{"&#8199;&zwnj;" * 40}</div>')
    h.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#F3F5F7;">'
             '<tr><td align="center" style="padding:24px 12px;">')
    h.append('<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
             'style="width:100%;max-width:600px;background:#FFFFFF;border:1px solid #DDE3E8;border-radius:8px;">')
    # brand bar + accent stripe
    h.append(f'<tr><td style="background:#0B1220;padding:16px 24px;border-radius:8px 8px 0 0;font-family:{_FONT};">'
             f'<span style="font-size:18px;font-weight:700;color:#FFFFFF;letter-spacing:.2px;">{esc(BRAND)}</span>'
             f'<span style="font-size:11px;color:#8FA3B5;letter-spacing:1.5px;text-transform:uppercase;"> &nbsp;{esc(ORG)}</span>'
             f'</td></tr>')
    h.append(f'<tr><td height="4" style="height:4px;line-height:4px;font-size:0;background:{color};">&nbsp;</td></tr>')
    # title block
    h.append(f'<tr><td style="padding:24px 24px 4px 24px;font-family:{_FONT};">')
    if chip:
        h.append(f'<span style="display:inline-block;background:{soft};color:{color};font-size:11px;font-weight:700;'
                 f'letter-spacing:.8px;text-transform:uppercase;padding:3px 9px;border-radius:3px;">{esc(chip)}</span>')
    elif eyebrow:
        h.append(f'<div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:{color};">{esc(eyebrow)}</div>')
    h.append(f'<div style="font-size:21px;line-height:28px;font-weight:700;color:#0B1220;margin-top:8px;">{esc(title)}</div>')
    if intro:
        h.append(f'<div style="font-size:14px;line-height:21px;color:#37474F;margin-top:8px;">{esc(intro)}</div>')
    h.append('</td></tr>')
    # facts
    if fact_rows:
        h.append(f'<tr><td style="padding:16px 24px 4px 24px;font-family:{_FONT};">'
                 '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                 'style="border-top:1px solid #E6EBEF;">')
        for label, value, sub in fact_rows:
            h.append(f'<tr><td valign="top" width="34%" style="padding:9px 12px 9px 0;border-bottom:1px solid #EEF2F5;'
                     f'font-size:12px;color:#607D8B;text-transform:uppercase;letter-spacing:.5px;">{esc(label)}</td>'
                     f'<td valign="top" style="padding:9px 0;border-bottom:1px solid #EEF2F5;font-size:14px;color:#0B1220;'
                     f'word-break:break-word;">{esc(value)}'
                     + (f'<div style="font-family:{_MONO};font-size:11px;line-height:16px;color:#78909C;margin-top:3px;'
                        f'word-break:break-all;">{esc(sub)}</div>' if sub else '')
                     + '</td></tr>')
        h.append('</table></td></tr>')
    for p in paragraphs:
        if p:
            h.append(f'<tr><td style="padding:12px 24px 0 24px;font-family:{_FONT};font-size:14px;line-height:21px;color:#37474F;">{esc(p)}</td></tr>')
    # button
    url = ""
    if button and _URL_OK.match(button[1] or ""):
        url = button[1]
        h.append(f'<tr><td style="padding:22px 24px 6px 24px;font-family:{_FONT};">'
                 '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
                 f'<td bgcolor="{color}" style="border-radius:5px;background:{color};">'
                 f'<a href="{esc(url)}" target="_blank" style="display:inline-block;padding:11px 24px;font-size:14px;font-weight:700;'
                 f'color:#FFFFFF;text-decoration:none;border-radius:5px;font-family:{_FONT};">{esc(button[0])}</a>'
                 '</td></tr></table></td></tr>')
        h.append(f'<tr><td style="padding:6px 24px 0 24px;font-family:{_FONT};font-size:12px;line-height:18px;color:#78909C;">'
                 f'Button not working? Copy this link into your browser:<br>'
                 f'<span style="font-family:{_MONO};word-break:break-all;color:#546E7A;">{esc(url)}</span></td></tr>')
    if fallback_note:
        h.append(f'<tr><td style="padding:12px 24px 0 24px;font-family:{_FONT};font-size:12px;line-height:18px;color:#607D8B;">{esc(fallback_note)}</td></tr>')
    h.append(f'<tr><td style="padding:24px;font-family:{_FONT};">'
             f'<div style="border-top:1px solid #E6EBEF;padding-top:14px;font-size:11px;line-height:17px;color:#90A4AE;">'
             f'{esc(footer or "This is an automated message from CloudOps Monitoring Hub. Please do not reply to it.")}</div>'
             f'</td></tr>')
    h.append('</table></td></tr></table></body></html>')

    t = []
    if chip:
        t.append(f"[{chip}]")
    elif eyebrow:
        t.append(eyebrow.upper())
    t.append(title)
    if intro:
        t += ["", intro]
    if fact_rows:
        t.append("")
        width = max(len(l) for l, _, _ in fact_rows) + 2
        for label, value, sub in fact_rows:
            t.append(f"{label + ':':<{width}}{value}")
            if sub:
                t.append(f"{'':<{width}}{sub}")
    for p in paragraphs:
        if p:
            t += ["", p]
    if url:
        t += ["", f"{button[0]}: {url}"]
    if fallback_note:
        t += ["", fallback_note]
    t += ["", "--", footer or "This is an automated message from CloudOps Monitoring Hub. Please do not reply to it."]
    return "".join(h), "\n".join(t) + "\n"


def _pack(subject, html, text):
    return {"subject": re.sub(r"\s+", " ", subject).strip(), "html": html, "text": text}


# ── alerts ───────────────────────────────────────────────────────────

def alert_email(event: str, a: dict, app_url: str = "") -> dict:
    """event: 'opened' | 'escalated'. a: the alert dict every notifier already builds (see notifications.sender)."""
    a = a or {}
    sev = str(a.get("severity") or "").upper()
    accent = sev if sev in ("CRITICAL", "WARNING") else "INFO"
    prio = a.get("priority") if a.get("priority") in (1, 2, 3, 4) else None
    metric_raw = a.get("metric_name") or ""
    metric = metric_text(metric_raw)
    rid = a.get("resource_id") or ""
    name = a.get("resource_name") or short_resource(rid)
    account = a.get("account_name") or ""
    val, thr = a.get("value"), a.get("threshold")
    have_nums = val is not None and thr is not None
    val_s, thr_s = value_text(metric_raw, val), value_text(metric_raw, thr)

    tag = f"P{prio} {sev}" if prio else sev
    tag = f"ESCALATED · {tag}" if event == "escalated" else tag
    nums = f"{val_s} vs {thr_s}" if have_nums else ""
    subject = f"[{tag.strip()}] {metric}" + (f": {nums}" if nums else "") \
              + (f" · {_clip(name, 50)}" if name else "") + (f" · {_clip(account, 30)}" if account else "") \
              + (f" → {_clip(a.get('group_name'), 40)}" if event == "escalated" and a.get("group_name") else "")
    subject = subject if len(subject) <= 140 else subject[:139].rstrip() + "…"

    title = ("Alert escalated" if event == "escalated" else "Alert opened") + (f": {metric}" if metric else "")
    when = fmt_when(a.get("triggered_at"))
    facts = [
        ("Resource", name or rid, rid if rid and rid != name else ""),
        ("Account", account),
        ("Metric", metric, metric_raw if metric_raw and metric_raw != metric else ""),
        ("Value", f"{val_s}   (threshold {thr_s})" if have_nums else ""),
        ("Region", a.get("region") or ""),
        ("Opened", when),
        ("Priority", f"P{prio}" if prio else ""),
        ("Escalated to", a.get("group_name") if event == "escalated" else ""),
        ("Alert ID", f"#{a['id']}" if isinstance(a.get("id"), int) and a["id"] > 0 else ""),
    ]
    paragraphs = [_clip(a["detail"], 500)] if a.get("detail") else []
    aid = a.get("id")
    path = f"/alerts?alert={aid}" if isinstance(aid, int) and aid > 0 else "/alerts"
    link = _link(app_url, path)
    pre = f"{sev.title()} alert" + (f" on {name}" if name else "") + (f" in {account}" if account else "") + (f" · {nums}" if nums else "")
    footer = ("You are receiving this because a CloudOps notification channel or escalation policy includes you. "
              "Manage channels under Settings → Notification channels.")
    html, text = render(preheader=pre, chip=tag, title=title, accent=accent, facts=facts, paragraphs=paragraphs,
                        button=("View alert", link) if link else None, footer=footer)
    return _pack(subject, html, text)


def test_notification_email(app_url: str = "", channel_name: str = "") -> dict:
    link = _link(app_url, "/settings")
    html, text = render(
        preheader="Your CloudOps notification channel works.", eyebrow="Notification test",
        title="This channel is working", accent="OK",
        intro="If you can read this message, CloudOps can reach this address. Alerts that match the channel's severity and "
              "event settings will arrive here.",
        facts=[("Channel", channel_name), ("Sent", fmt_when(datetime.now(timezone.utc)))],
        button=("Open settings", link) if link else None)
    return _pack("[CloudOps] Test notification: this channel works", html, text)


def cost_test_email(app_url: str = "") -> dict:
    link = _link(app_url, "/cloud-cost")
    html, text = render(
        preheader="You will receive an email like this when a cost-jump alert opens.", eyebrow="Cost-jump alert · test",
        title="Cost-jump alert recipients: test", accent="WARNING",
        intro="This is a test from CloudOps. If you can read it, you will receive an email when the cost-jump alert opens. "
              "The real alert looks like this, with the account, the day's cost, the 7-day average and the alert limit.",
        facts=[("Example account", "Aurionpro"), ("Example cost", "$12.40 on 31 Oct 2026"),
               ("Example 7-day average", "$8.10"), ("Example alert limit", "$10.53 (+30%)"),
               ("Sent", fmt_when(datetime.now(timezone.utc)))],
        button=("Open Cloud cost", link) if link else None)
    return _pack("[CloudOps] Test: cost-jump alert recipients", html, text)


def escalation_email(*, severity, metric_name, resource_id, group_name, alert_id=None, account_name="", app_url="",
                     triggered_at=None) -> dict:
    d = alert_email("escalated", {"id": alert_id, "severity": severity, "metric_name": metric_name, "resource_id": resource_id,
                                  "account_name": account_name, "group_name": group_name, "triggered_at": triggered_at}, app_url)
    return d


def synthetic_test_email(*, check_name, target, sent_by="", app_url="") -> dict:
    link = _link(app_url, "/synthetic-checks")
    html, text = render(
        preheader=f"You are listed as an alert contact for {check_name}.", eyebrow="Alert contacts · test",
        title=f"Test: alert contacts for {check_name}", accent="OK",
        intro="You are listed as an alert contact for this synthetic check. You will be emailed if it goes down or its "
              "certificate is about to expire. If you can read this, delivery works.",
        facts=[("Check", check_name), ("Target", target), ("Sent by", sent_by), ("Sent", fmt_when(datetime.now(timezone.utc)))],
        button=("Open synthetic checks", link) if link else None)
    return _pack(f"[CloudOps] Test: alert contacts for {check_name}", html, text)


# ── accounts ─────────────────────────────────────────────────────────

def account_created_email(*, username, role, link, hours=24, login_url="", created_by="") -> dict:
    html, text = render(
        preheader="Set your password to start using CloudOps.", eyebrow="Welcome", title="Your CloudOps account is ready",
        accent="INFO",
        intro=f"An account has been created for you on CloudOps{f' by {created_by}' if created_by else ''}. "
              "Set a password to sign in.",
        facts=[("Username", username), ("Role", str(role or "").title()), ("Link valid for", f"{hours} hours, single use")],
        button=("Set your password", link),
        fallback_note="If you weren't expecting this, ignore this email or contact your CloudOps administrator. "
                      "Nobody at CloudOps will ever ask you for your password.",
        footer="This link signs you in once. Do not forward it.")
    return _pack("Set up your CloudOps account", html, text)


def password_reset_email(*, username, link, valid_text, requested_by="") -> dict:
    """Reset started by an administrator."""
    who = f"An administrator ({requested_by})" if requested_by else "An administrator"
    html, text = render(
        preheader="Reset your CloudOps password.", eyebrow="Password reset", title="Reset your password", accent="INFO",
        intro=f"{who} requested a password reset for your CloudOps account.",
        facts=[("Username", username), ("Link valid for", valid_text)],
        button=("Choose a new password", link),
        fallback_note="If you weren't expecting this, ignore this email or contact your CloudOps administrator: "
                      "your password will not change until you use the link.",
        footer="This link works once. Do not forward it.")
    return _pack("Reset your CloudOps password", html, text)


def self_reset_email(*, username, link, valid_text) -> dict:
    html, text = render(
        preheader="Reset your CloudOps password.", eyebrow="Password reset", title="Reset your password", accent="INFO",
        intro="We received a request to reset the password for your CloudOps account.",
        facts=[("Username", username), ("Link valid for", valid_text)],
        button=("Choose a new password", link),
        fallback_note="If you didn't ask for this, you can safely ignore this email: your password will not change.",
        footer="This link works once. Do not forward it.")
    return _pack("Reset your CloudOps password", html, text)


# ── reports ──────────────────────────────────────────────────────────

_TYPE_WORD = {"WEEKLY": "Weekly", "MONTHLY": "Monthly", "QUARTERLY": "Quarterly", "CUSTOM": "Custom"}


def report_email(report: dict, *, filename="", size_bytes=None, app_url="") -> dict:
    report = report or {}
    rtype = _TYPE_WORD.get(str(report.get("report_type") or "").upper(), "Operations")
    scope = _clip(report.get("scope_label") or report.get("scope_id") or "", 80)
    period = fmt_period(report.get("period_start"), report.get("period_end"))
    subject = f"CloudOps {rtype} Report · {scope}" + (f" · {period}" if period else "")
    size = ""
    if isinstance(size_bytes, (int, float)) and size_bytes > 0:
        size = f"{size_bytes / 1024:.0f} KB" if size_bytes < 1024 * 1024 else f"{size_bytes / 1048576:.1f} MB"
    link = _link(app_url, "/reports")
    html, text = render(
        preheader=f"{rtype} report for {scope}" + (f", {period}" if period else "") + ": PDF attached.",
        eyebrow=f"{rtype} report", title=scope or "CloudOps report", accent="INFO",
        intro="Your CloudOps monitoring report is attached as a PDF.",
        facts=[("Report", f"{rtype} operations report"), ("Scope", scope), ("Period", period),
               ("Generated", fmt_when(report.get("created_at") or report.get("generated_at"))),
               ("Attachment", filename, size)],
        button=("Open in CloudOps", link) if link else None,
        footer="This report was sent from CloudOps Monitoring Hub at the request of a CloudOps user. "
               "It may contain operational data: share it only with people who should see it.")
    return _pack(subject, html, text)


def report_attachment_name(report: dict, fallback="report.pdf") -> str:
    try:
        from app.report_names import periodic_report_filename
        name = periodic_report_filename(report)
    except Exception:
        name = fallback
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or fallback
    return name if name.lower().endswith(".pdf") else name + ".pdf"


# ── plain text -> branded mail (synthetic checks, integrity, digest) ──

_FACT_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9 /_-]{1,28}):\s{1,}(\S.*)$")
_OPEN_LINE = re.compile(r"^(Open in CloudOps|Open|View and acknowledge it here):\s*(https?://\S+)\s*$", re.I)


def from_plain(subject: str, body: str, *, accent="INFO", eyebrow="", footer="", button_label="Open in CloudOps") -> dict:
    """Wraps an existing plain-text notification in the shared layout without changing its wording.
    First line -> title; 'Label:   value' lines -> a facts table; 'Open ...: <url>' -> the button; the rest -> paragraphs."""
    lines = [l.rstrip() for l in (body or "").splitlines()]
    title, facts, paras, url = "", [], [], ""
    buf = []

    def flush():
        if buf:
            paras.append(" ".join(buf))
            buf.clear()

    for i, l in enumerate(lines):
        if not l.strip():
            flush()
            continue
        m = _OPEN_LINE.match(l.strip())
        if m:
            flush()
            url = m.group(2)
            continue
        if not title and i == 0:
            title = l.strip()
            continue
        fm = _FACT_LINE.match(l.strip())
        if fm and len(fm.group(2)) < 400:
            flush()
            facts.append((fm.group(1), fm.group(2)))
        else:
            buf.append(l.strip())
    flush()
    html, text = render(preheader=_clip(title or subject, 110), eyebrow=eyebrow, title=title or subject, accent=accent,
                        facts=facts, paragraphs=paras, button=(button_label, url) if url else None, footer=footer)
    return _pack(subject, html, text)
