# tests/test_email_rule_dispatch.py
"""
Email & Reports centre, patch 0005: rule_dispatch (who is mailed, throttle / cap / once-per-event, the sweep, the
background worker) and the three hooks (alert opened, alert escalated, sweep). The database is a small in-memory fake;
no SMTP and no real MySQL is involved.
"""
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module  # noqa: E402
from tests.test_email_center import (  # noqa: E402,F401  (fixtures are used by name)
    _enabled, _pure_modules, _restore_stubs, install_stub,
)
from tests.test_audit_b08 import RecConn  # noqa: E402

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 4, 12, 0, 0)


# ── fake database ────────────────────────────────────────────────────

class _State:
    def __init__(self, **kw):
        self.rules = kw.get("rules", [])
        self.lists = kw.get("lists", {})
        self.users = kw.get("users", [])                    # rows for "FROM users WHERE id IN"
        self.group_users = kw.get("group_users", {})        # group id -> user rows (recipient lists)
        self.group_emails = kw.get("group_emails", {})      # group id -> emails (escalation exclusion)
        self.channels = kw.get("channels", [])
        self.sends = kw.get("sends", [])                    # {"rule_id","rk","kind","obj","event","res","age"}
        self.sweep_alerts = kw.get("sweep_alerts", [])
        self.inc_open = kw.get("inc_open", [])
        self.inc_res = kw.get("inc_res", [])
        self.synth_contacts = kw.get("synth_contacts", {})  # (check id, "notify_outage" | "notify_cert") -> emails
        self.cost_jump_rows = kw.get("cost_jump_rows", [])  # rows for cost_jump_settings.load
        self.fail_on = kw.get("fail_on")
        self.log, self.commits, self.connects, self.closed = [], 0, 0, 0


class _Cur:
    rowcount = 0

    def __init__(self, st):
        self.st, self._rows = st, []

    def execute(self, sql, params=None):
        st, n = self.st, " ".join(sql.split())
        st.log.append((n, params))
        if st.fail_on and st.fail_on in n:
            raise RuntimeError("db down")
        p = params or ()
        self.rowcount = 0
        if n.startswith("SELECT * FROM email_alert_rules WHERE enabled = 1"):
            self._rows = [r for r in st.rules if r.get("enabled")]
        elif n.startswith("SELECT * FROM notification_channels"):
            self._rows = [c for c in st.channels if c.get("type") == "email"]
        elif n.startswith("SELECT email FROM synthetic_check_contacts"):
            col = "notify_cert" if "notify_cert" in n else "notify_outage"
            self._rows = [{"email": e} for e in st.synth_contacts.get((p[0], col), [])]
        elif "FROM cost_jump_settings" in n:
            self._rows = st.cost_jump_rows
        elif n.startswith("SELECT DISTINCT u.email FROM user_group_memberships"):
            self._rows = [{"email": e} for e in st.group_emails.get(p[0], [])]
        elif n.startswith("SELECT * FROM email_recipient_lists WHERE id"):
            self._rows = [st.lists[p[0]]] if p[0] in st.lists else []
        elif "FROM users WHERE id IN" in n:
            self._rows = [u for u in st.users if u["id"] in p]
        elif n.startswith("SELECT DISTINCT u.id, u.username"):
            self._rows = [u for g in p for u in st.group_users.get(g, [])]
        elif n.startswith("SELECT 1 AS ok FROM email_rule_sends"):
            rk, kind, obj, event = p
            self._rows = [{"ok": 1}] if any((s["rk"], s["kind"], s["obj"], s["event"]) == (rk, kind, obj, event)
                                            for s in st.sends) else []
        elif n.startswith("SELECT resource_key, TIMESTAMPDIFF"):
            rule_id, rk = p
            self._rows = [{"resource_key": s["res"], "age": s["age"]} for s in st.sends
                          if s["rule_id"] == rule_id and s["rk"] == rk and s["age"] < 3600]
        elif n.startswith("INSERT IGNORE INTO email_rule_sends"):
            rule_id, rk, kind, obj, event, res = p
            if not any((s["rk"], s["kind"], s["obj"], s["event"]) == (rk, kind, obj, event) for s in st.sends):
                st.sends.append({"rule_id": rule_id, "rk": rk, "kind": kind, "obj": obj, "event": event, "res": res, "age": 0})
                self.rowcount = 1
        elif "FROM alerts a LEFT JOIN aws_accounts" in n:
            self._rows = st.sweep_alerts
        elif "FROM incidents i" in n and "i.created_at" in n:
            self._rows = st.inc_open
        elif "FROM incidents i" in n and "i.status = 'resolved'" in n:
            self._rows = st.inc_res
        else:
            raise AssertionError(f"unexpected SQL: {n!r}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, st):
        self.st = st
        st.connects += 1

    def cursor(self, dictionary=False):
        return _Cur(self.st)

    def commit(self):
        self.st.commits += 1

    def close(self):
        self.st.closed += 1


def _env(smtp_ok=True, configured=True, scope=None, **state):
    """-> (rule_dispatch module, state, mails sent). `scope` maps user id -> set of account ids (None = sees all)."""
    _pure_modules()
    st, mails = _State(**state), []
    install_stub("app.db", get_connection=lambda: _Conn(st))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda u: (scope or {}).get(u["id"]))

    def send_email_ex(to, subject, text, html=None, sender_label="", category=""):
        mails.append({"to": to, "subject": subject, "text": text, "html": html, "label": sender_label, "category": category})
        return (smtp_ok(to) if callable(smtp_ok) else smtp_ok), ("" if smtp_ok else "smtp down")
    install_stub("app.email.mailer", is_configured=lambda: configured, send_email_ex=send_email_ex,
                 get_public_app_url=lambda: "https://cloudops.example.com")
    cjs = load_module("app/cost_jump_settings.py")
    install_stub("app.cost_jump_settings", **{k: v for k, v in vars(cjs).items() if not k.startswith("__")})
    sender = load_module("app/notifications/sender.py")
    install_stub("app.notifications.sender", channel_matches=sender.channel_matches, parse_addresses=sender.parse_addresses)
    return load_module("app/email/rule_dispatch.py"), st, mails


def _rule(**over):
    base = {"id": 1, "enabled": 1, "name": "r", "events": "alert_opened,alert_escalated,alert_resolved,incident_opened,incident_resolved",
            "min_severity": "WARNING", "account_id": None, "recipient_list_id": None, "extra_emails": "ops@example.com"}
    base.update(over)
    return base


def _ctx(**over):
    base = {"kind": "alert", "object_id": 100, "account_id": 7, "resource_id": "i-0abc", "severity": "CRITICAL",
            "metric_name": "CPUUtilization", "value": 97.5, "threshold": 90, "account_name": "Prod", "silenced": False}
    base.update(over)
    return base


def _send_row(rule_id=1, addr="ops@example.com", obj=1, event="alert_opened", res="x", age=10, kind="alert"):
    import hashlib
    return {"rule_id": rule_id, "rk": hashlib.sha1(addr.lower().encode()).hexdigest(), "kind": kind, "obj": obj,
            "event": event, "res": res, "age": age}


# ── message ──────────────────────────────────────────────────────────

def test_alert_message_subject_text_and_link():
    mod, *_ = _env()
    m = mod.build_message("alert_opened", _ctx(), "https://cloudops.example.com/")
    assert m["subject"] == "[CRITICAL] CPUUtilization on i-0abc - opened"
    assert "Account: Prod" in m["text"] and "Value 97.5 against threshold 90" in m["text"]
    assert "Open: https://cloudops.example.com/alerts?alert=100" in m["text"]
    assert 'href="https://cloudops.example.com/alerts?alert=100"' in m["html"]


def test_message_escalated_names_the_group_and_priority():
    mod, *_ = _env()
    m = mod.build_message("alert_escalated", _ctx(group_name="L2-platform", priority=1), "https://x")
    assert m["subject"].endswith("- escalated") and "Escalated to group: L2-platform" in m["text"] and "Priority: P1" in m["text"]


def test_incident_message_links_to_the_accounts_incidents():
    mod, *_ = _env()
    m = mod.build_message("incident_resolved", {"kind": "incident", "object_id": 5, "account_id": 7, "title": "DB and API down",
                                                "severity": "warning", "account_name": "Prod"}, "https://x")
    assert m["subject"] == "[WARNING] Incident resolved: DB and API down"
    assert "https://x/accounts/7/incidents" in m["text"]


def test_message_escapes_html_and_keeps_line_breaks_out_of_the_subject():
    mod, *_ = _env()
    m = mod.build_message("alert_opened", _ctx(resource_id="i-1\r\nBcc: x@y.com", metric_name="<script>alert(1)</script>"), "https://x")
    assert "\r" not in m["subject"] and "\n" not in m["subject"]
    assert "<script>" not in m["html"] and "&lt;script&gt;" in m["html"]


def test_message_without_an_app_url_has_no_link():
    mod, *_ = _env()
    m = mod.build_message("alert_opened", _ctx(), "")
    assert "Open:" not in m["text"] and "href" not in m["html"] and "CPUUtilization" in m["html"]


# ── dispatch: who gets mail ──────────────────────────────────────────

def test_a_matching_rule_mails_its_recipients_once_and_records_the_send_after_success():
    mod, st, mails = _env(rules=[_rule()])
    assert mod.dispatch_many([("alert_opened", _ctx())], now=NOW) == 1
    assert [(m["to"], m["label"], m["category"]) for m in mails] == [("ops@example.com", "Alerts", "alert_rule")]
    assert len(st.sends) == 1 and st.sends[0]["event"] == "alert_opened" and st.commits == 1 and st.closed == st.connects == 1


def test_a_send_that_fails_is_not_recorded_so_it_can_be_retried():
    mod, st, mails = _env(rules=[_rule()], smtp_ok=False)
    assert mod.dispatch_many([("alert_opened", _ctx())], now=NOW) == 0
    assert st.sends == [] and len(mails) == 1


@pytest.mark.parametrize("event,ctx", [
    ("alert_resolved", _ctx()),                                  # event not chosen below
    ("alert_opened", _ctx(severity="WARNING")),                  # below the severity floor
    ("alert_opened", _ctx(account_id=8)),                        # other account
    ("alert_opened", _ctx(silenced=True)),                       # silenced
])
def test_events_that_do_not_match_send_nothing(event, ctx):
    mod, st, mails = _env(rules=[_rule(events="alert_opened", min_severity="CRITICAL", account_id=7)])
    assert mod.dispatch_many([(event, ctx)], now=NOW) == 0 and mails == [] and st.sends == []


def test_a_paused_rule_is_never_loaded():
    mod, st, mails = _env(rules=[_rule(enabled=0)])
    assert mod.dispatch_many([("alert_opened", _ctx())], now=NOW) == 0 and mails == []
    assert any("enabled = 1" in sql for sql, _ in st.log)


def test_flag_off_is_a_complete_no_op(monkeypatch):
    monkeypatch.setenv("EMAIL_CENTER_ENABLED", "false")
    mod, st, mails = _env(rules=[_rule()])
    assert mod.dispatch_many([("alert_opened", _ctx())], now=NOW) == 0
    assert mod.sweep() == 0 and st.connects == 0 and mails == []


def test_unconfigured_smtp_sends_nothing_and_does_not_touch_the_database():
    mod, st, mails = _env(rules=[_rule()], configured=False)
    assert mod.dispatch_many([("alert_opened", _ctx())], now=NOW) == 0 and st.connects == 0 and mails == []


def test_recipient_list_members_are_resolved_for_the_alerts_account():
    users = [{"id": 1, "username": "a", "role": "editor", "email": "a@x.com", "active": 1},
             {"id": 2, "username": "b", "role": "viewer", "email": "b@x.com", "active": 1},      # cannot see account 7
             {"id": 3, "username": "c", "role": "editor", "email": "c@x.com", "active": 0}]      # inactive
    mod, st, mails = _env(rules=[_rule(recipient_list_id=3, extra_emails="typed@partner.com")],
                          lists={3: {"user_ids": "[1,2,3]", "group_ids": None, "emails": None}},
                          users=users, scope={1: None, 2: {99}, 3: None})
    assert mod.dispatch_many([("alert_opened", _ctx())], now=NOW) == 2
    assert sorted(m["to"] for m in mails) == ["a@x.com", "typed@partner.com"]


def test_addresses_an_email_channel_already_mails_are_skipped():
    channels = [{"type": "email", "enabled": 1, "events": "opened,escalated", "min_severity": "CRITICAL",
                 "aws_account_id": None, "target": "Ops@Example.com, other@example.com"}]
    mod, st, mails = _env(rules=[_rule(extra_emails="ops@example.com, new@example.com")], channels=channels)
    assert mod.dispatch_many([("alert_opened", _ctx())], now=NOW) == 1
    assert [m["to"] for m in mails] == ["new@example.com"]


def test_a_channel_that_would_not_mail_this_alert_does_not_exclude_anyone():
    channels = [{"type": "email", "enabled": 1, "events": "opened", "min_severity": "CRITICAL",
                 "aws_account_id": None, "target": "ops@example.com"}]
    mod, st, mails = _env(rules=[_rule()], channels=channels)
    assert mod.dispatch_many([("alert_opened", _ctx(severity="WARNING"))], now=NOW) == 1      # channel wants CRITICAL only
    mod, st, mails = _env(rules=[_rule()], channels=channels)
    assert mod.dispatch_many([("alert_resolved", _ctx())], now=NOW) == 1                      # channels never mail resolved


def test_escalation_group_members_are_skipped_for_escalated_only():
    kw = dict(rules=[_rule(extra_emails="member@example.com, boss@example.com")], group_emails={3: ["Member@example.com"]})
    mod, st, mails = _env(**kw)
    assert mod.dispatch_many([("alert_escalated", _ctx(group_id=3, group_name="L2"))], now=NOW) == 1
    assert [m["to"] for m in mails] == ["boss@example.com"]
    mod, st, mails = _env(**kw)
    assert mod.dispatch_many([("alert_opened", _ctx(group_id=3))], now=NOW) == 2            # not an escalation: both


def test_if_the_exclusion_lookup_fails_nothing_is_sent_and_nothing_raises():
    mod, st, mails = _env(rules=[_rule()], fail_on="FROM notification_channels")
    assert mod.dispatch_many([("alert_opened", _ctx())], now=NOW) == 0 and mails == [] and st.sends == []


# ── dispatch: once, throttle, cap ────────────────────────────────────

def test_the_same_event_is_never_mailed_to_the_same_person_twice_even_across_rules():
    mod, st, mails = _env(rules=[_rule(id=1), _rule(id=2, name="r2")])
    assert mod.dispatch_many([("alert_opened", _ctx())], now=NOW) == 1                        # second rule: already mailed
    assert mod.dispatch_many([("alert_opened", _ctx())], now=NOW) == 0
    assert len(mails) == 1


def test_opened_does_not_swallow_the_resolved_mail_for_the_same_alert():
    mod, st, mails = _env(rules=[_rule()])
    assert mod.dispatch_many([("alert_opened", _ctx())], now=NOW) == 1
    assert mod.dispatch_many([("alert_resolved", _ctx())], now=NOW) == 1
    assert [m["subject"][-8:] for m in mails] == ["- opened", "resolved"]


def test_same_resource_and_event_inside_the_window_is_throttled_then_allowed():
    mod, st, mails = _env(rules=[_rule()])
    key = mod.alert_rules.resource_key(7, "alert_opened:i-0abc")
    st.sends.append(_send_row(obj=5, res=key, age=600))                                        # 10 min ago: inside 30 min
    assert mod.dispatch_many([("alert_opened", _ctx(object_id=101))], now=NOW) == 0
    st.sends[0]["age"] = 2400                                                                  # 40 min ago: window over
    assert mod.dispatch_many([("alert_opened", _ctx(object_id=101))], now=NOW) == 1


def test_a_different_resource_is_not_throttled_by_another():
    mod, st, mails = _env(rules=[_rule()])
    st.sends.append(_send_row(obj=5, res=mod.alert_rules.resource_key(7, "alert_opened:i-OTHER"), age=60))
    assert mod.dispatch_many([("alert_opened", _ctx(object_id=101))], now=NOW) == 1


def test_resource_ids_shared_by_two_accounts_do_not_throttle_each_other():
    mod, st, mails = _env(rules=[_rule()])
    st.sends.append(_send_row(obj=5, res=mod.alert_rules.resource_key(8, "alert_opened:i-0abc"), age=60))
    assert mod.dispatch_many([("alert_opened", _ctx(object_id=101))], now=NOW) == 1


def test_hourly_cap_stops_a_rule_for_a_recipient(monkeypatch):
    monkeypatch.setenv("EMAIL_RULE_MAX_PER_RECIPIENT_HOUR", "3")
    mod, st, mails = _env(rules=[_rule()])
    for i in range(3):
        st.sends.append(_send_row(obj=i + 1, res=f"other{i}", age=60 * (i + 1)))
    assert mod.dispatch_many([("alert_opened", _ctx(object_id=500, resource_id="i-new"))], now=NOW) == 0
    st.sends[0]["age"] = 3601                                                                  # one aged out of the hour
    assert mod.dispatch_many([("alert_opened", _ctx(object_id=500, resource_id="i-new"))], now=NOW) == 1


def test_the_cap_is_per_rule():
    mod, st, mails = _env(rules=[_rule(id=2, name="other")])
    for i in range(25):
        st.sends.append(_send_row(rule_id=1, obj=i + 1, res=f"o{i}", age=60))
    assert mod.dispatch_many([("alert_opened", _ctx(object_id=900))], now=NOW) == 1


# ── never raises ─────────────────────────────────────────────────────

def test_a_database_failure_never_raises_out_of_dispatch_or_sweep():
    mod, st, mails = _env(rules=[_rule()], fail_on="FROM email_alert_rules")
    assert mod.dispatch_many([("alert_opened", _ctx())], now=NOW) == 0
    assert mod.sweep() == 0 and mails == []


def test_a_failing_connection_never_raises():
    mod, *_ = _env()

    def boom():
        raise RuntimeError("no connection")
    sys.modules["app.db"].get_connection = boom
    assert mod.dispatch_many([("alert_opened", _ctx())], now=NOW) == 0 and mod.sweep() == 0


def test_one_bad_item_does_not_stop_the_rest():
    mod, st, mails = _env(rules=[_rule()])
    bad = {"kind": "alert", "severity": "CRITICAL"}                                            # no object_id
    assert mod.dispatch_many([("alert_opened", bad), ("alert_opened", _ctx())], now=NOW) == 1


def test_dispatch_with_nothing_to_do_opens_no_connection():
    mod, st, mails = _env()
    assert mod.dispatch_many([], now=NOW) == 0 and st.connects == 0


# ── sweep ────────────────────────────────────────────────────────────

_RES_ALERT = {"id": 31, "aws_account_id": 7, "resource_id": "i-0abc", "metric_name": "CPUUtilization", "severity": "CRITICAL",
              "current_value": 12.0, "threshold": 90.0, "account_name": "Prod"}
_INC = {"id": 8, "aws_account_id": 7, "title": "DB and API down", "severity": "CRITICAL", "account_name": "Prod"}


def test_sweep_mails_resolved_alerts_and_incidents_and_uses_the_writers_clocks():
    mod, st, mails = _env(rules=[_rule()], sweep_alerts=[_RES_ALERT], inc_open=[_INC], inc_res=[dict(_INC, id=9)])
    assert mod.sweep(minutes=30) == 3
    assert sorted(m["subject"] for m in mails) == ["[CRITICAL] CPUUtilization on i-0abc - resolved",
                                                   "[CRITICAL] Incident opened: DB and API down",
                                                   "[CRITICAL] Incident resolved: DB and API down"]
    alert_sql = [s for s, _ in st.log if "FROM alerts a LEFT JOIN" in s][0]
    assert "UTC_TIMESTAMP()" in alert_sql and "a.resolved_at" in alert_sql and "COALESCE(a.silenced, 0) = 0" in alert_sql
    for s, _ in [x for x in st.log if "FROM incidents i" in x[0]]:
        assert "NOW()" in s and "UTC_TIMESTAMP" not in s
    assert [p for s, p in st.log if "FROM alerts a LEFT JOIN" in s][0] == (30, mod.SWEEP_LIMIT)


def test_sweep_only_queries_for_the_events_some_rule_wants():
    mod, st, mails = _env(rules=[_rule(events="incident_resolved")], inc_res=[_INC])
    assert mod.sweep() == 1
    assert not any("FROM alerts a" in s for s, _ in st.log)
    assert sum("FROM incidents i" in s for s, _ in st.log) == 1


def test_sweep_with_no_rules_runs_no_further_queries_and_overlapping_sweeps_do_not_remail():
    mod, st, mails = _env()
    assert mod.sweep() == 0 and st.connects == 1
    mod, st, mails = _env(rules=[_rule()], sweep_alerts=[_RES_ALERT])
    assert mod.sweep() == 1 and mod.sweep() == 0 and len(mails) == 1


def test_sweep_reads_the_look_back_from_the_knob(monkeypatch):
    monkeypatch.setenv("EMAIL_RULE_SWEEP_MINUTES", "45")
    mod, st, _ = _env(rules=[_rule()], sweep_alerts=[])
    mod.sweep()
    assert [p for s, p in st.log if "FROM alerts a LEFT JOIN" in s][0][0] == 45


# ── background worker ────────────────────────────────────────────────

def test_submit_returns_immediately_while_the_handler_is_stuck():
    mod, *_ = _env()
    release, started = threading.Event(), threading.Event()

    def slow(job):
        started.set()
        release.wait(5)
    w = mod._Worker(slow)
    t0 = time.monotonic()
    assert w.submit({"type": "event"}) and started.wait(2) and w.submit({"type": "event"})
    assert time.monotonic() - t0 < 1.0                                  # the caller never waited for the handler
    release.set()
    assert w.join()


def test_a_full_queue_drops_the_job_instead_of_blocking():
    mod, *_ = _env()
    release, started = threading.Event(), threading.Event()
    w = mod._Worker(lambda job: (started.set(), release.wait(5)), maxsize=2)
    assert w.submit({"type": "event"}) and started.wait(2)
    assert w.submit({"type": "event"}) and w.submit({"type": "event"})
    t0 = time.monotonic()
    assert w.submit({"type": "event"}) is False and time.monotonic() - t0 < 0.5
    release.set()
    assert w.join()


def test_a_sweep_still_queued_or_running_is_not_queued_again():
    mod, *_ = _env()
    release, started, ran = threading.Event(), threading.Event(), []

    def handler(job):
        ran.append(job["type"])
        started.set()
        release.wait(5)
    w = mod._Worker(handler)
    assert w.submit_sweep() and started.wait(2)
    assert w.submit_sweep() is False and w.submit_sweep() is False        # running: skipped
    release.set()
    assert w.join() and ran == ["sweep"]
    assert w.submit_sweep() is True and w.join() and ran == ["sweep", "sweep"]     # free again afterwards


def test_a_failing_job_does_not_kill_the_worker_or_leave_a_sweep_stuck():
    mod, *_ = _env()
    seen = []

    def handler(job):
        seen.append(job["type"])
        raise RuntimeError("boom")
    w = mod._Worker(handler)
    assert w.submit_sweep() and w.join()
    assert w.submit({"type": "event"}) and w.join()
    assert w.submit_sweep() and w.join()
    assert seen == ["sweep", "event", "sweep"]


def test_enqueue_never_touches_the_database_or_smtp():
    mod, st, mails = _env(rules=[_rule()])
    queued = []
    mod._worker = type("W", (), {"submit": lambda self, job: queued.append(job) or True,
                                 "submit_sweep": lambda self: queued.append({"type": "sweep"}) or True})()
    assert mod.enqueue_alert_event("alert_opened", {"id": 5, "severity": "CRITICAL", "metric_name": "m", "resource_id": "r",
                                                    "aws_account_id": 7, "silenced": False}) is True
    assert mod.request_sweep() is True
    assert st.connects == 0 and mails == [] and [j["type"] for j in queued] == ["event", "sweep"]
    assert queued[0]["event"] == "alert_opened" and queued[0]["ctx"]["object_id"] == 5 and queued[0]["ctx"]["account_id"] == 7


@pytest.mark.parametrize("event,alert", [("alert_resolved_x", {"id": 1}), ("alert_opened", {"id": None}), ("alert_opened", {"id": 0}),
                                         ("alert_opened", {"id": 3, "silenced": True}), ("alert_opened", None)])
def test_enqueue_ignores_bad_input_without_raising(event, alert):
    mod, *_ = _env()
    mod._worker = type("W", (), {"submit": lambda self, job: pytest.fail("must not queue")})()
    assert mod.enqueue_alert_event(event, alert) is False


def test_hooks_do_nothing_and_start_no_thread_when_the_flag_is_off(monkeypatch):
    monkeypatch.setenv("EMAIL_CENTER_ENABLED", "false")
    mod, *_ = _env()
    assert mod.enqueue_alert_event("alert_opened", {"id": 5, "severity": "CRITICAL"}) is False
    assert mod.request_sweep() is False and mod._worker._thread is None


def test_the_real_worker_end_to_end_mails_a_queued_alert():
    mod, st, mails = _env(rules=[_rule()])
    assert mod.enqueue_alert_event("alert_opened", {"id": 5, "severity": "CRITICAL", "metric_name": "m", "resource_id": "r",
                                                    "aws_account_id": 7, "account_name": "Prod", "silenced": False})
    assert mod._worker.join() and [m["to"] for m in mails] == ["ops@example.com"]


def test_slow_smtp_never_delays_the_hook():
    mod, st, mails = _env(rules=[_rule()])
    release = threading.Event()
    sys.modules["app.email.mailer"].send_email_ex = lambda *a, **k: (release.wait(5), (True, ""))[1]
    t0 = time.monotonic()
    for i in range(1, 6):
        assert mod.enqueue_alert_event("alert_opened", {"id": i, "severity": "CRITICAL", "metric_name": "m",
                                                        "resource_id": f"r{i}", "aws_account_id": 7, "silenced": False})
    assert time.monotonic() - t0 < 1.0
    release.set()
    assert mod._worker.join(10)


# ── hooks ────────────────────────────────────────────────────────────

_ESC_ROW = {"alert_id": 7, "resource_id": "i-1", "metric_name": "cpu", "severity": "CRITICAL", "triggered_at": None,
            "aws_account_id": 5, "policy_id": 1, "ack_sla_minutes": 10, "escalate_to_group_id": 3, "group_name": "L2",
            "account_name": "Prod"}


def _escalation(hook_raises=False):
    log, events, calls = [], [], []
    install_stub("app.db", get_connection=lambda: RecConn(log, [("FROM alerts a JOIN resources r", [_ESC_ROW])], events=events))
    install_stub("app.alert_rules", firing_where=lambda *a, **k: "1=1", base_where=lambda *a, **k: "1=1")
    install_stub("app.collector.op_log", log_event=lambda *a, **k: None)
    install_stub("app.email.mailer", is_configured=lambda: False)
    install_stub("app.notifications.sender", notify_alert_event=lambda *a, **k: events.append("channels"))

    def enqueue(event, alert):
        events.append("rules")
        calls.append((event, alert))
        if hook_raises:
            raise RuntimeError("queue exploded")
    install_stub("app.email.rule_dispatch", enqueue_alert_event=enqueue)
    return load_module("app/collector/escalation.py"), events, calls


def test_escalation_hook_queues_after_the_commit_with_the_group():
    mod, events, calls = _escalation()
    assert mod.evaluate_escalations() == 1
    assert events.index("commit") < events.index("rules")
    event, alert = calls[0]
    assert event == "alert_escalated" and alert["id"] == 7 and alert["aws_account_id"] == 5
    assert alert["group_id"] == 3 and alert["group_name"] == "L2" and alert["silenced"] is False


def test_escalation_still_succeeds_when_the_rule_hook_raises():
    mod, events, calls = _escalation(hook_raises=True)
    assert mod.evaluate_escalations() == 1 and "rollback" not in events


def _src(path):
    return (ROOT / path).read_text()


def test_alert_opened_hook_is_a_post_commit_publish_that_only_queues():
    src = _src("app/collector/alert_evaluator.py")
    block = src.split("    def _rule_mail():", 1)[1].split("    publishes.append(_rule_mail)", 1)[0]
    assert "rule_dispatch.enqueue_alert_event(\"alert_opened\"" in block
    assert "dispatch_many" not in block and "send_email" not in block and "get_connection" not in block
    assert src.index("publishes.append(_notify_channels)") < src.index("publishes.append(_rule_mail)")
    assert src.index("return publishes   # born silenced") < src.index("def _rule_mail")      # silenced alerts never reach it
    assert "for fn in publishes:" in src                                                       # run after the commit, guarded


def test_the_sweep_is_requested_once_per_standard_cycle_after_escalation_and_only_queues():
    src = _src("app/collector/scheduler.py")
    std = src.split('    if tier == "standard":', 1)[1].split('    if tier == "extended":', 1)[0]
    assert std.count("rule_dispatch.request_sweep()") == 1
    assert std.index("evaluate_escalations()") < std.index("rule_dispatch.request_sweep()")
    assert "rule_dispatch.sweep(" not in src and "dispatch_many" not in src


def test_no_collector_file_sends_mail_for_rules_inline():
    for path in ("app/collector/alert_evaluator.py", "app/collector/escalation.py", "app/collector/scheduler.py"):
        src = _src(path)
        assert "rule_dispatch.dispatch_many" not in src and "rule_dispatch.sweep(" not in src, path
        assert "send_email_ex" not in src, path


# ── patch 0007: synthetic checks, cost-jump and the host-agent watchdog ───────────────────────────────────────────────

_SYN = {"metric_name": "synthetic_uptime", "resource_id": "synthetic-12 (Public site)", "severity": "CRITICAL"}
_BOTH = _rule(extra_emails="ops@example.com, Owner@example.com")


def test_a_synthetic_checks_own_contacts_are_not_mailed_again_but_other_rule_recipients_are():
    mod, st, mails = _env(rules=[_BOTH], synth_contacts={(12, "notify_outage"): ["owner@example.com"]})
    assert mod.dispatch_many([("alert_opened", _ctx(**_SYN))], now=NOW) == 1
    assert [m["to"] for m in mails] == ["ops@example.com"]


def test_synthetic_contacts_are_skipped_for_escalated_and_resolved_too():
    for event in ("alert_escalated", "alert_resolved"):
        mod, st, mails = _env(rules=[_BOTH], synth_contacts={(12, "notify_outage"): ["owner@example.com"]})
        mod.dispatch_many([(event, _ctx(**_SYN))], now=NOW)
        assert [m["to"] for m in mails] == ["ops@example.com"], event


def test_the_certificate_alert_uses_the_certificate_contact_topic_not_the_outage_one():
    cert = dict(_SYN, metric_name="synthetic_cert_expiry")
    mod, st, mails = _env(rules=[_BOTH], synth_contacts={(12, "notify_outage"): ["owner@example.com"]})
    mod.dispatch_many([("alert_opened", _ctx(**cert))], now=NOW)
    assert sorted(m["to"] for m in mails) == ["Owner@example.com", "ops@example.com"]      # outage-only contact still gets the rule mail
    mod, st, mails = _env(rules=[_BOTH], synth_contacts={(12, "notify_cert"): ["owner@example.com"]})
    mod.dispatch_many([("alert_opened", _ctx(**cert))], now=NOW)
    assert [m["to"] for m in mails] == ["ops@example.com"]


def test_a_synthetic_alert_whose_check_cannot_be_identified_sends_nothing():
    mod, st, mails = _env(rules=[_BOTH])
    assert mod.dispatch_many([("alert_opened", _ctx(**dict(_SYN, resource_id="somewhere-else")))], now=NOW) == 0
    assert mails == [] and st.sends == []


def test_a_failing_contact_lookup_sends_nothing_rather_than_a_duplicate():
    mod, st, mails = _env(rules=[_BOTH], fail_on="synthetic_check_contacts")
    assert mod.dispatch_many([("alert_opened", _ctx(**_SYN))], now=NOW) == 0
    assert mails == [] and st.sends == []


_CJ = {"metric_name": "cloud_cost_jump", "resource_id": "cost-5", "severity": "WARNING", "account_id": 5}


def _cj_row(scope, users="[]", emails=""):
    return {"scope_key": scope, "enabled": 1, "threshold_pct": None, "min_increase_usd": None,
            "notify_user_ids": users, "notify_emails": emails, "updated_by": "x", "updated_at": None}


def test_the_cost_jump_recipients_are_not_mailed_a_second_time():
    users = [{"id": 31, "username": "fin", "email": "Finance@example.com", "active": 1}]
    rows = [_cj_row(0, users="[31]", emails="cfo@example.com"), _cj_row(5, emails="lead@example.com")]
    rule = _rule(extra_emails="ops@example.com, finance@example.com, cfo@example.com, lead@example.com")
    mod, st, mails = _env(rules=[rule], users=users, cost_jump_rows=rows)
    assert mod.dispatch_many([("alert_opened", _ctx(**_CJ))], now=NOW) == 1
    assert [m["to"] for m in mails] == ["ops@example.com"]


def test_another_accounts_cost_jump_recipients_are_not_excluded():
    mod, st, mails = _env(rules=[_rule(extra_emails="ops@example.com, lead@example.com")],
                          cost_jump_rows=[_cj_row(9, emails="lead@example.com")])
    mod.dispatch_many([("alert_opened", _ctx(**_CJ))], now=NOW)
    assert sorted(m["to"] for m in mails) == ["lead@example.com", "ops@example.com"]


def test_no_cost_jump_settings_means_nobody_is_excluded():
    mod, st, mails = _env(rules=[_rule()])                                       # no cost_jump_settings rows at all
    assert mod.dispatch_many([("alert_opened", _ctx(**_CJ))], now=NOW) == 1


def test_the_extra_lookups_run_only_for_the_alert_sources_that_need_them():
    mod, st, mails = _env(rules=[_rule()])
    mod.dispatch_many([("alert_opened", _ctx())], now=NOW)                       # an ordinary CPU alert
    assert not [q for q, _ in st.log if "synthetic_check_contacts" in q or "cost_jump_settings" in q]
    mod, st, mails = _env(rules=[_rule()])
    mod.dispatch_many([("alert_resolved", _ctx(**_CJ))], now=NOW)                # cost-jump sends no resolve mail
    assert not [q for q, _ in st.log if "cost_jump_settings" in q]


def test_the_watchdog_needs_no_extra_lookup_because_it_only_mails_through_channels():
    wd = {"metric_name": "host_agent_heartbeat_age", "resource_id": "i-0abc", "severity": "CRITICAL"}
    mod, st, mails = _env(rules=[_rule()],
                          channels=[{"type": "email", "enabled": 1, "target": "ops@example.com", "events": "opened",
                                     "min_severity": "WARNING", "account_ids": None}])
    mod.dispatch_many([("alert_opened", _ctx(**wd))], now=NOW)
    assert not [q for q, _ in st.log if "synthetic_check_contacts" in q or "cost_jump_settings" in q]


# enqueue_channel_event: the word the notification channels use -> the rule event

def _queued():
    mod, st, mails = _env(rules=[_rule()])
    queued = []
    mod._worker = type("W", (), {"submit": lambda self, job: queued.append(job) or True})()
    return mod, queued


@pytest.mark.parametrize("kind,event", [("opened", "alert_opened"), ("escalated", "alert_escalated")])
def test_channel_words_map_to_rule_events_and_only_queue(kind, event):
    mod, queued = _queued()
    assert mod.enqueue_channel_event(kind, {"id": 9, "severity": "CRITICAL", "metric_name": "synthetic_uptime",
                                            "resource_id": "synthetic-4 (x)", "aws_account_id": 2, "silenced": False}) is True
    assert queued[0]["event"] == event and queued[0]["ctx"]["object_id"] == 9 and queued[0]["ctx"]["account_id"] == 2


@pytest.mark.parametrize("kind", ["resolved", "recovered", "", None, "alert_opened"])
def test_other_channel_words_are_ignored_the_sweep_mails_resolves(kind):
    mod, queued = _queued()
    assert mod.enqueue_channel_event(kind, {"id": 9, "severity": "CRITICAL", "silenced": False}) is False and queued == []


def test_a_silenced_or_bad_alert_through_the_channel_helper_queues_nothing_and_never_raises():
    mod, queued = _queued()
    assert mod.enqueue_channel_event("opened", {"id": 9, "silenced": True}) is False
    assert mod.enqueue_channel_event("opened", None) is False and queued == []


def test_the_metric_names_the_dispatcher_knows_are_the_ones_the_sources_write():
    mod, *_ = _env()
    assert set(mod._SYNTHETIC_CONTACT_COLUMN) == {"synthetic_uptime", _src("app/collector/synthetic.py").split('CERT_ALERT_METRIC = "', 1)[1].split('"', 1)[0]}
    assert 'METRIC = "%s"' % mod._COST_JUMP_METRIC in _src("app/collector/cost_jump_alert.py")
    assert "'synthetic_uptime'" in _src("app/collector/synthetic.py")
