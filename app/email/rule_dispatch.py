# app/email/rule_dispatch.py
"""
Sends the mail for alert and incident email rules (patch 0005). The rules themselves live in app/email/alert_rules.py
(0004); this module decides who gets mail for one event and hands it to the mailer.

NEVER DELAYS OR BREAKS THE COLLECTOR
  * The hooks (alert opened, alert escalated, the once-per-cycle sweep) only call enqueue_alert_event() /
    request_sweep(). Those touch no database and no network: they put a small job on a bounded in-memory queue and
    return. One daemon thread does every query and every SMTP send, so a slow mail server delays that thread only.
  * A full queue drops the job with a warning (the sweep still catches resolved alerts and incidents; an "opened" mail
    for a burst beyond the queue is the price of never blocking).
  * A sweep that is already queued or running is not queued again ("skipped if the previous one is still running").
  * Nothing here raises to a caller. A failed send is logged and recorded by the mailer in the delivery log.
  * Off unless EMAIL_CENTER_ENABLED=true: no thread is started, nothing is queued, no table is touched.

WHO IS MAILED (per event, per rule that matches event + severity + account)
  * The rule's recipient list and extra addresses, through recipients.resolve(): inactive users, users with no email and
    users who may not see the account are dropped; typed addresses always stay.
  * Not an address a notification email channel already mails for this alert (opened / escalated), and not a member of
    the escalation group for `alert_escalated` (escalation.py already mailed them). If that lookup fails, nothing is sent.
  * Never a silenced alert. Never the same person twice for the same alert/incident event (unique key, across rules),
    never the same resource+event to one person inside the throttle window, never more than the hourly cap per rule.
  * email_rule_sends gets its row only AFTER a successful send.

The mail goes out as category 'alert_rule' (sender label 'Alerts'), so it shows in the delivery log.
"""
import html as _html
import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

from app.email import alert_rules, delivery_log, recipients

logger = logging.getLogger(__name__)

CATEGORY = "alert_rule"
QUEUE_MAX = 500
SWEEP_LIMIT = 500                     # rows per sweep query: a burst beyond this is picked up by the next sweep

_ALERT_VERBS = {"alert_opened": "opened", "alert_escalated": "escalated", "alert_resolved": "resolved"}
_INCIDENT_VERBS = {"incident_opened": "opened", "incident_resolved": "resolved"}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── message ──────────────────────────────────────────────────────────

def _clean(value, limit=200):
    return " ".join(str(value if value is not None else "").split())[:limit]


def _fmt(v):
    try:
        f = float(v)
        return f"{f:.2f}".rstrip("0").rstrip(".") if abs(f) < 1e6 else f"{f:.3g}"
    except (TypeError, ValueError):
        return _clean(v, 40)


def build_message(event, ctx, app_url=""):
    """-> {"subject", "text", "html"}. Names the account, the resource / title, severity and a link; nothing else."""
    sev = _clean(ctx.get("severity"), 20).upper() or "ALERT"
    account = _clean(ctx.get("account_name"), 120)
    url = (app_url or "").rstrip("/")
    if ctx.get("kind") == "incident":
        verb = _INCIDENT_VERBS.get(event, "updated")
        title = _clean(ctx.get("title"), 200) or f"#{ctx.get('object_id')}"
        subject = f"[{sev}] Incident {verb}: {title}"
        lines = [f"{alert_rules.EVENT_LABELS.get(event, 'Incident')}: {sev} - {title}"]
        link = f"{url}/accounts/{ctx['account_id']}/incidents" if url and ctx.get("account_id") else ""
    else:
        verb = _ALERT_VERBS.get(event, "updated")
        metric, resource = _clean(ctx.get("metric_name"), 100), _clean(ctx.get("resource_id"), 200)
        subject = f"[{sev}] {metric} on {resource} - {verb}"
        lines = [f"{alert_rules.EVENT_LABELS.get(event, 'Alert')}: {sev} {metric} on {resource}"]
        if ctx.get("priority") in (1, 2, 3, 4):
            lines.append(f"Priority: P{ctx['priority']}")
        if ctx.get("value") is not None and ctx.get("threshold") is not None:
            lines.append(f"Value {_fmt(ctx['value'])} against threshold {_fmt(ctx['threshold'])}")
        if event == "alert_escalated" and ctx.get("group_name"):
            lines.append(f"Escalated to group: {_clean(ctx['group_name'], 100)}")
        aid = ctx.get("object_id")
        link = f"{url}/alerts" + (f"?alert={aid}" if isinstance(aid, int) and aid > 0 else "") if url else ""
    if account:
        lines.insert(1, f"Account: {account}")
    if link:
        lines.append(f"Open: {link}")
    text = "\n".join(lines)
    body = "".join(f"<p style=\"margin:0 0 8px\">{_html.escape(l)}</p>" for l in lines if not l.startswith("Open: "))
    if link:
        body += f"<p style=\"margin:12px 0 0\"><a href=\"{_html.escape(link, quote=True)}\">Open in CloudOps</a></p>"
    return {"subject": subject, "text": text,
            "html": f"<div style=\"font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1f2937\">{body}</div>"}


# ── queries (every one runs on the worker thread, never in the collector) ─

def _load_rules(cur):
    cur.execute("SELECT * FROM email_alert_rules WHERE enabled = 1 ORDER BY id")
    return cur.fetchall()


def _excluded_addresses(cur, event, ctx):
    """Lower-cased addresses somebody else already mails for this event. Raises when it cannot tell."""
    out = set()
    if event in ("alert_opened", "alert_escalated"):
        from app.notifications import sender
        channel_event = "opened" if event == "alert_opened" else "escalated"
        cur.execute("SELECT * FROM notification_channels WHERE enabled = 1 AND type = 'email'")
        for ch in cur.fetchall():
            if sender.channel_matches(ch, channel_event, ctx.get("severity"), ctx.get("account_id")):
                out.update(a.lower() for a in sender.parse_addresses(ch.get("target")))
    if event == "alert_escalated" and ctx.get("group_id"):
        cur.execute("SELECT DISTINCT u.email FROM user_group_memberships ugm JOIN users u ON u.id = ugm.user_id "
                    "WHERE ugm.group_id = %s AND u.email IS NOT NULL AND u.email != ''", (ctx["group_id"],))
        out.update(str(r["email"]).strip().lower() for r in cur.fetchall())
    return out


def _rule_addresses(cur, rule, account_id, list_cache):
    row = None
    list_id = rule.get("recipient_list_id")
    if list_id is not None:
        if list_id not in list_cache:
            cur.execute("SELECT * FROM email_recipient_lists WHERE id = %s", (list_id,))
            list_cache[list_id] = cur.fetchone()
        row = list_cache[list_id]
    return recipients.resolve(row, rule.get("extra_emails"), account_id=account_id, cursor=cur)["addresses"]


def _already_sent(cur, rkey, kind, object_id, event):
    cur.execute("SELECT 1 AS ok FROM email_rule_sends WHERE recipient_key = %s AND kind = %s AND object_id = %s "
                "AND event = %s LIMIT 1", (rkey, kind, object_id, event))
    return cur.fetchone() is not None


def _recent_sends(cur, rule_id, rkey, now):
    cur.execute("SELECT resource_key, TIMESTAMPDIFF(SECOND, sent_at, NOW()) AS age FROM email_rule_sends "
                "WHERE rule_id = %s AND recipient_key = %s AND sent_at > DATE_SUB(NOW(), INTERVAL 1 HOUR)",
                (rule_id, rkey))
    return [{"sent_at": now - timedelta(seconds=int(r["age"] or 0)), "resource_key": r.get("resource_key")}
            for r in cur.fetchall()]


def _record_send(conn, cur, rule_id, rkey, kind, object_id, event, res_key):
    cur.execute("INSERT IGNORE INTO email_rule_sends (rule_id, recipient_key, kind, object_id, event, resource_key) "
                "VALUES (%s,%s,%s,%s,%s,%s)", (rule_id, rkey, kind, object_id, event, res_key))
    conn.commit()


def _dispatch_one(conn, cur, rules, event, ctx, now, mailer):
    if ctx.get("silenced"):
        return 0
    kind, object_id, account_id = ctx["kind"], ctx["object_id"], ctx.get("account_id")
    matching = [r for r in rules if alert_rules.matches(r, event, ctx.get("severity"), account_id)]
    if not matching:
        return 0
    excluded = _excluded_addresses(cur, event, ctx)
    # throttle per resource AND event: an "opened" mail must not swallow the "resolved" mail minutes later
    res_key = alert_rules.resource_key(account_id, f"{event}:{ctx['resource_id']}") if ctx.get("resource_id") else None
    list_cache, message, sent = {}, None, 0
    for rule in matching:
        for addr in _rule_addresses(cur, rule, account_id, list_cache):
            if addr.lower() in excluded:
                continue
            rkey = alert_rules.recipient_key(addr)
            ok, _why = alert_rules.decide(now, _recent_sends(cur, rule["id"], rkey, now), res_key,
                                          already_sent=_already_sent(cur, rkey, kind, object_id, event))
            if not ok:
                continue
            if message is None:
                message = build_message(event, ctx, mailer.get_public_app_url())
            sent_ok, _err = mailer.send_email_ex(addr, message["subject"], message["text"], message["html"],
                                                 sender_label="Alerts", category=CATEGORY)
            if sent_ok:
                _record_send(conn, cur, rule["id"], rkey, kind, object_id, event, res_key)
                sent += 1
    return sent


def dispatch_many(items, now=None):
    """items = [(event, ctx)]. Returns how many mails were sent. Never raises."""
    if not items or not delivery_log.enabled():
        return 0
    sent = 0
    try:
        from app.email import mailer
        if not mailer.is_configured():
            logger.info("[email-rules] SMTP is not configured; no rule mail sent")
            return 0
        from app.db import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor(dictionary=True)
            rules = _load_rules(cur)
            if not rules:
                return 0
            for event, ctx in items:
                try:
                    sent += _dispatch_one(conn, cur, rules, event, ctx, now or _utcnow(), mailer)
                except Exception as exc:
                    logger.warning(f"[email-rules] {event} {ctx.get('kind')} {ctx.get('object_id')} skipped: "
                                   f"{type(exc).__name__}: {str(exc)[:160]}")
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[email-rules] dispatch failed (non-fatal): {type(exc).__name__}: {str(exc)[:160]}")
    return sent


def sweep(minutes=None):
    """Mails alerts resolved and incidents opened / resolved in the last `minutes` (EMAIL_RULE_SWEEP_MINUTES), which also
    covers manual and auto-resolve paths no hook sees. Overlap with an earlier sweep is harmless (once-per-event key).
    Alerts are timed with UTC_TIMESTAMP() and incidents with NOW(), exactly as their writers stamp them."""
    if not delivery_log.enabled():
        return 0
    minutes = alert_rules.sweep_minutes() if minutes is None else minutes
    items = []
    try:
        from app.db import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor(dictionary=True)
            wanted = set()
            for rule in _load_rules(cur):
                wanted.update(e.strip() for e in str(rule.get("events") or "").split(","))
            if "alert_resolved" in wanted:
                cur.execute(
                    "SELECT a.id, a.aws_account_id, a.resource_id, a.metric_name, a.severity, a.current_value, "
                    "a.threshold, acc.account_name FROM alerts a LEFT JOIN aws_accounts acc ON acc.id = a.aws_account_id "
                    "WHERE a.status = 'resolved' AND a.resolved_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s MINUTE) "
                    "AND COALESCE(a.silenced, 0) = 0 ORDER BY a.id LIMIT %s", (minutes, SWEEP_LIMIT))
                for r in cur.fetchall():
                    items.append(("alert_resolved", {
                        "kind": "alert", "object_id": r["id"], "account_id": r["aws_account_id"],
                        "resource_id": r["resource_id"], "severity": r["severity"], "metric_name": r["metric_name"],
                        "value": r["current_value"], "threshold": r["threshold"], "account_name": r.get("account_name"),
                        "silenced": False}))
            for event, cond in (("incident_opened", "i.created_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)"),
                                ("incident_resolved", "i.status = 'resolved' AND i.resolved_at >= "
                                                      "DATE_SUB(NOW(), INTERVAL %s MINUTE)")):
                if event not in wanted:
                    continue
                cur.execute("SELECT i.id, i.aws_account_id, i.title, i.severity, acc.account_name FROM incidents i "
                            "LEFT JOIN aws_accounts acc ON acc.id = i.aws_account_id WHERE " + cond +
                            " ORDER BY i.id LIMIT %s", (minutes, SWEEP_LIMIT))
                for r in cur.fetchall():
                    items.append((event, {"kind": "incident", "object_id": r["id"], "account_id": r["aws_account_id"],
                                          "title": r["title"], "severity": r["severity"],
                                          "account_name": r.get("account_name"), "resource_id": None, "silenced": False}))
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[email-rules] sweep query failed (non-fatal): {type(exc).__name__}: {str(exc)[:160]}")
        return 0
    return dispatch_many(items)


# ── background worker ────────────────────────────────────────────────

class _Worker:
    """One daemon thread, one bounded queue. submit() never blocks and never raises."""

    def __init__(self, handler, maxsize=QUEUE_MAX):
        self._q = queue.Queue(maxsize)
        self._handler = handler
        self._thread = None
        self._lock = threading.Lock()
        self._sweep_pending = False

    def _ensure_thread(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="email-rule-worker", daemon=True)
                self._thread.start()

    def submit(self, job):
        try:
            self._q.put_nowait(job)
        except queue.Full:
            logger.warning("[email-rules] queue is full; dropped one rule-mail job")
            return False
        self._ensure_thread()
        return True

    def submit_sweep(self):
        with self._lock:
            if self._sweep_pending:
                return False
            self._sweep_pending = True
        if not self.submit({"type": "sweep"}):
            with self._lock:
                self._sweep_pending = False
            return False
        return True

    def _run(self):
        while True:
            job = self._q.get()
            try:
                self._handler(job)
            except Exception as exc:                       # the thread must outlive any single failure
                logger.warning(f"[email-rules] worker job failed: {type(exc).__name__}: {str(exc)[:160]}")
            finally:
                if job.get("type") == "sweep":
                    with self._lock:
                        self._sweep_pending = False
                self._q.task_done()

    def join(self, timeout=5.0):
        """For tests: waits until the queue is drained."""
        end = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < end:
            threading.Event().wait(0.01)
        return not self._q.unfinished_tasks


def _handle(job):
    if job.get("type") == "sweep":
        sweep()
    else:
        dispatch_many([(job["event"], job["ctx"])])


_worker = _Worker(_handle)


# ── what the hooks call ──────────────────────────────────────────────

def enqueue_alert_event(event, alert):
    """Hook entry for alert_opened / alert_escalated. `alert` is the dict the notification channels already get
    (id, severity, metric_name, resource_id, aws_account_id, account_name, value, threshold, priority, silenced, group_id,
    group_name). Returns True when queued. No database, no network, never raises."""
    try:
        if not delivery_log.enabled() or event not in _ALERT_VERBS or alert.get("silenced"):
            return False
        ctx = {"kind": "alert", "object_id": alert.get("id"), "account_id": alert.get("aws_account_id"),
               "resource_id": alert.get("resource_id"), "severity": alert.get("severity"),
               "metric_name": alert.get("metric_name"), "value": alert.get("value"), "threshold": alert.get("threshold"),
               "account_name": alert.get("account_name"), "priority": alert.get("priority"),
               "group_id": alert.get("group_id"), "group_name": alert.get("group_name"), "silenced": False}
        if not isinstance(ctx["object_id"], int) or ctx["object_id"] <= 0:
            return False
        return _worker.submit({"type": "event", "event": event, "ctx": ctx})
    except Exception as exc:
        logger.warning(f"[email-rules] could not queue {event}: {type(exc).__name__}: {str(exc)[:160]}")
        return False


def request_sweep():
    """Hook entry for the once-per-cycle sweep. Returns True when a sweep was queued, False when it was skipped because
    one is already queued or running (or the feature is off). Never blocks, never raises."""
    try:
        return bool(delivery_log.enabled()) and _worker.submit_sweep()
    except Exception as exc:
        logger.warning(f"[email-rules] could not queue the sweep: {type(exc).__name__}: {str(exc)[:160]}")
        return False
