# tests/test_audit_p6_notifications.py
"""Audit C9: notification channels - selection, safety, isolation, and hook wiring."""
import sys
from pathlib import Path

import app          # noqa: F401
import app.auth     # noqa: F401

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_imports():
    """sender lazily imports the real SSRF guard (app.collector.synthetic) and the real mailer. Both bind to whatever
    app.db stub is installed, and importing app.email.mailer also sets a `mailer` attribute on the real app.email
    package, which makes a later `from app.email import mailer` ignore a test's sys.modules stub. Restore all of it."""
    had_attr = hasattr(sys.modules.get("app.email"), "mailer")
    had_mod = {n: n in sys.modules for n in ("app.email.mailer", "app.collector.synthetic")}
    yield
    for name in ("app.notifications.sender", "app.notifications"):
        sys.modules.pop(name, None)
    for name, was_there in had_mod.items():
        if not was_there:
            sys.modules.pop(name, None)
    email_pkg = sys.modules.get("app.email")
    if email_pkg is not None and not had_attr and hasattr(email_pkg, "mailer"):
        delattr(email_pkg, "mailer")


class _Cur:
    def __init__(self, channels, log):
        self.channels, self.log, self._rows = channels, log, []
    def execute(self, sql, params=None):
        if sql.lstrip().upper().startswith("SELECT"):
            self._rows = list(self.channels)
        elif "INSERT INTO notification_log" in sql:
            self.log.append(params)
    def fetchall(self):
        return self._rows
    def close(self):
        pass


class _Conn:
    def __init__(self, channels, log):
        self.channels, self.log, self.closed, self.committed = channels, log, False, False
    def cursor(self, dictionary=False):
        return _Cur(self.channels, self.log)
    def commit(self):
        self.committed = True
    def close(self):
        self.closed = True


def _sender(channels=None, log=None):
    log = log if log is not None else []
    conn = _Conn(channels or [], log)
    install_stub("app.db", get_connection=lambda: conn)
    mod = load_module("app/notifications/sender.py")
    return mod, conn, log


def _ch(**kw):
    base = {"id": 1, "name": "ops-slack", "type": "slack", "target": "https://hooks.slack.com/services/T0/B0/SECRET",
            "min_severity": "CRITICAL", "events": "opened,escalated", "aws_account_id": None, "enabled": 1}
    base.update(kw)
    return base


def _alert(**kw):
    base = {"id": 7, "severity": "CRITICAL", "metric_name": "CPUUtilization", "resource_id": "i-1",
            "account_name": "AuroGov", "aws_account_id": 7, "value": 93.4, "threshold": 90, "silenced": False}
    base.update(kw)
    return base


# ── matching ─────────────────────────────────────────────────────────

def test_severity_event_and_account_filters():
    m, _, _ = _sender()
    assert m.channel_matches(_ch(), "opened", "CRITICAL", 7)
    assert not m.channel_matches(_ch(), "opened", "WARNING", 7)                    # critical-only channel
    assert m.channel_matches(_ch(min_severity="WARNING"), "opened", "WARNING", 7)
    assert not m.channel_matches(_ch(events="escalated"), "opened", "CRITICAL", 7)
    assert not m.channel_matches(_ch(aws_account_id=10), "opened", "CRITICAL", 7)  # scoped to another account
    assert m.channel_matches(_ch(aws_account_id=7), "opened", "CRITICAL", 7)
    assert not m.channel_matches(_ch(enabled=0), "opened", "CRITICAL", 7)


# ── validation / SSRF ────────────────────────────────────────────────

def test_validation_rejects_bad_targets():
    m, _, _ = _sender()
    for ctype, target in [("slack", "http://hooks.slack.com/x"),            # not https
                          ("slack", "https://user:pw@hooks.slack.com/x"),   # credentials in URL
                          ("webhook", "https://127.0.0.1/hook"),            # loopback
                          ("webhook", "https://169.254.169.254/latest"),    # cloud metadata
                          ("webhook", "https://10.0.0.5/hook"),             # private range
                          ("webhook", "https://localhost/hook"),
                          ("email", "not-an-address"),
                          ("email", "a@b.com\r\nBcc: x@y.com"),
                          ("sms", "x"), ("slack", "")]:
        try:
            m.validate_channel(ctype, target)
            assert False, (ctype, target)
        except ValueError:
            pass


def test_validation_accepts_good_targets():
    m, _, _ = _sender()
    assert m.validate_channel("email", "a@x.com, b@y.org") == "a@x.com, b@y.org"
    assert m.validate_channel("slack", "https://hooks.slack.com/services/T0/B0/X").startswith("https://")


def test_secret_url_never_exposed():
    m, _, _ = _sender()
    masked = m.mask_target("slack", "https://hooks.slack.com/services/T0/B0/SECRET")
    assert "SECRET" not in masked and "hooks.slack.com" in masked
    assert m.mask_target("email", "ops@x.com") == "ops@x.com"
    assert "SECRET" not in m._short_error(RuntimeError("POST https://hooks.slack.com/services/T0/B0/SECRET failed"))


# ── fan-out behaviour ────────────────────────────────────────────────

def test_one_failing_channel_does_not_block_the_others_and_is_logged():
    chans = [_ch(id=1, name="bad"), _ch(id=2, name="good"), _ch(id=3, name="warn-only", min_severity="CRITICAL", events="escalated")]
    m, conn, log = _sender(chans)
    calls = []

    def fake_deliver(ch, msg):
        calls.append(ch["name"])
        if ch["name"] == "bad":
            raise RuntimeError("POST https://hooks.slack.com/services/T0/B0/SECRET timed out")
    m.deliver = fake_deliver
    sent = m.notify_alert_event("opened", _alert())
    assert sent == 1 and calls == ["bad", "good"]                 # 'warn-only' does not subscribe to 'opened'
    # log rows are (channel_id, channel_name, alert_id, event, status, detail)
    by_name = {row[1]: (row[4], row[5]) for row in log}
    assert by_name["good"][0] == "sent" and by_name["bad"][0] == "failed"
    assert "SECRET" not in (by_name["bad"][1] or "")
    assert conn.committed and conn.closed


def test_silenced_alerts_are_not_notified():
    m, _, log = _sender([_ch()])
    m.deliver = lambda ch, msg: (_ for _ in ()).throw(AssertionError("must not send"))
    assert m.notify_alert_event("opened", _alert(silenced=True)) == 0 and log == []


def test_fan_out_never_raises_even_if_the_database_is_down():
    install_stub("app.db", get_connection=lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    m = load_module("app/notifications/sender.py")
    assert m.notify_alert_event("opened", _alert()) == 0


def test_message_content_is_minimal_and_useful():
    m, _, _ = _sender()
    msg = m.build_message("opened", _alert())
    assert "CRITICAL" in msg["headline"] and "CPUUtilization" in msg["headline"] and "AuroGov" in msg["headline"]
    assert "93.4" in msg["text"] and "90" in msg["text"]
    esc = m.build_message("escalated", _alert(group_name="L2-platform"))
    assert "L2-platform" in esc["text"]


# ── wiring ───────────────────────────────────────────────────────────

def test_hooks_are_wired_after_commit_and_failures_are_contained():
    ev = (ROOT / "app/collector/alert_evaluator.py").read_text()
    assert 'notify_alert_event("opened"' in ev and "publishes.append(_notify_channels)" in ev
    assert ev.index("if silence_reason:") < ev.index("_notify_channels")       # silenced alerts return before the hook
    esc = (ROOT / "app/collector/escalation.py").read_text()
    assert 'notify_alert_event("escalated"' in esc


def test_api_is_permission_gated_and_registered():
    api = (ROOT / "app/api/notifications.py").read_text()
    # literal code strings (not a variable) so tests/test_permission_catalog_drift.py can see the enforcement
    assert api.count('require_permission("notifications.manage")') >= 5 and "_MANAGE" not in api
    assert "target_preview" in api and '"target": row["target"]' not in api
    main = (ROOT / "app/main.py").read_text()
    assert "notifications_router" in main


def test_migration_080_defines_tables_and_permission():
    sql = (ROOT / "db/migrations/080_notification_channels.sql").read_text()
    for needle in ("CREATE TABLE IF NOT EXISTS notification_channels", "CREATE TABLE IF NOT EXISTS notification_log",
                   "'notifications.manage'", "uq_notification_channel_name"):
        assert needle in sql
