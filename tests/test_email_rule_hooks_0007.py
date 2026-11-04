# tests/test_email_rule_hooks_0007.py
"""
Email & Reports centre, patch 0007: the three alert sources that already notify channels (synthetic checks, cost-jump,
the host-agent watchdog) now also QUEUE an email-rule job. The hook only calls rule_dispatch.enqueue_channel_event; a
failing hook never changes the source's own result. (Who is skipped is covered in test_email_rule_dispatch.py.)
"""
import importlib
import sys
import types
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import pytest  # noqa: E402

import tests.test_synthetic_notify as tsn  # noqa: E402
from tests.conftest import install_stub, load_module  # noqa: E402
from tests.test_cost_jump_alert import cj  # noqa: E402,F401  (fixture used by name)
from tests.test_host_agent_watchdog import FakeDB, _load, _stub  # noqa: E402
from tests.test_synthetic_notify import _load_notifier  # noqa: E402,F401  (autouse fixture of that file)


def _rule_hook(monkeypatch, order=None, raises=False):
    calls = []

    def enqueue(kind, alert):
        calls.append((kind, alert))
        if order is not None:
            order.append("rules")
        if raises:
            raise RuntimeError("queue exploded")
        return True
    mod = types.ModuleType("app.email.rule_dispatch")
    mod.enqueue_channel_event = enqueue
    monkeypatch.setitem(sys.modules, "app.email.rule_dispatch", mod)
    # `from app.email import rule_dispatch` reads the attribute of the package first, which an earlier test file may have set
    monkeypatch.setattr(importlib.import_module("app.email"), "rule_dispatch", mod, raising=False)
    return calls


ROOT = Path(__file__).resolve().parent.parent


# ── synthetic checks ─────────────────────────────────────────────────

class _OneAlertCursor:
    def execute(self, sql, params=None):
        self.params = params

    def fetchone(self):
        return {"id": 9, "severity": "CRITICAL", "value": 3, "threshold": 3, "account_name": "Prod"}


_CHECK = {"id": 4, "name": "Public site", "aws_account_id": 2}


@pytest.mark.parametrize("metric,event", [("synthetic_uptime", "opened"), ("synthetic_cert_expiry", "opened"),
                                          ("synthetic_cert_expiry", "escalated")])
def test_synthetic_fan_out_queues_the_rule_mail_after_the_channels(monkeypatch, metric, event):
    order, sent = [], []
    calls = _rule_hook(monkeypatch, order)
    install_stub("app.notifications.sender", notify_alert_event=lambda e, a: order.append("channels") or sent.append((e, a)) or 1)
    alert_id, n = tsn.nt._fan_out_channels(_OneAlertCursor(), _CHECK, metric, event)
    assert (alert_id, n) == (9, 1) and order == ["channels", "rules"]
    kind, alert = calls[0]
    assert kind == event and alert == sent[0][1]
    assert alert["metric_name"] == metric and alert["resource_id"] == "synthetic-4 (Public site)"
    assert alert["id"] == 9 and alert["aws_account_id"] == 2 and alert["silenced"] is False


def test_synthetic_fan_out_still_succeeds_when_the_rule_hook_raises(monkeypatch):
    _rule_hook(monkeypatch, raises=True)
    install_stub("app.notifications.sender", notify_alert_event=lambda e, a: 2)
    assert tsn.nt._fan_out_channels(_OneAlertCursor(), _CHECK, "synthetic_uptime", "opened") == (9, 2)


def test_synthetic_alert_that_is_not_found_queues_nothing(monkeypatch):
    calls = _rule_hook(monkeypatch)
    install_stub("app.notifications.sender", notify_alert_event=lambda e, a: 1)

    class Empty(_OneAlertCursor):
        def fetchone(self):
            return None
    assert tsn.nt._fan_out_channels(Empty(), _CHECK, "synthetic_uptime", "opened") == (None, 0) and calls == []


# ── cost-jump ────────────────────────────────────────────────────────

def _cj_env(monkeypatch, cj_mod, order=None, rule_raises=False):
    calls = _rule_hook(monkeypatch, order, rule_raises)
    mod_pub = types.ModuleType("app.ws.publisher")
    mod_pub.publish_alert = lambda **kw: None
    monkeypatch.setitem(sys.modules, "app.ws.publisher", mod_pub)
    sent = []
    mod_sender = types.ModuleType("app.notifications.sender")
    mod_sender.notify_alert_event = lambda e, a: (order.append("channels") if order is not None else None) or sent.append((e, a))
    mod_sender.build_message = lambda e, a: {"headline": "h", "text": "t"}
    monkeypatch.setitem(sys.modules, "app.notifications.sender", mod_sender)
    monkeypatch.setattr(cj_mod, "send_to_recipients", lambda *a, **k: order.append("recipients") if order is not None else None)
    return calls, sent


def test_cost_jump_queues_the_rule_mail_for_an_opened_alert_after_the_channels(cj, monkeypatch):
    order = []
    calls, sent = _cj_env(monkeypatch, cj, order)
    cj._notify(31, "cost-5", 5, "Acme", 90.0, 13.0, "detail", recips=([1], ["a@b.co"]))
    assert order.index("channels") < order.index("rules")
    kind, alert = calls[0]
    assert kind == "opened" and alert == sent[0][1]
    assert alert["metric_name"] == "cloud_cost_jump" and alert["severity"] == "WARNING" and alert["aws_account_id"] == 5


def test_cost_jump_still_mails_its_own_recipients_when_the_rule_hook_raises(cj, monkeypatch):
    order = []
    _cj_env(monkeypatch, cj, order, rule_raises=True)
    cj._notify(31, "cost-5", 5, "Acme", 90.0, 13.0, "detail", recips=([1], ["a@b.co"]))
    assert "recipients" in order


def test_cost_jump_run_never_queues_for_a_refresh_or_a_silenced_alert():
    src = (ROOT / "app/collector/cost_jump_alert.py").read_text()
    assert src.count("enqueue_channel_event") == 1                       # only inside _notify
    assert "if not silence:\n                            opened.append(" in src                 # born silenced: no notification


# ── host-agent watchdog ──────────────────────────────────────────────

_HOST, _TARGET = {"aws_account_id": 3, "id": 1}, {"resource_id": "i-0abc", "tags": None, "resource_type": "ec2"}


@pytest.mark.parametrize("kind", ["opened", "escalated"])
def test_watchdog_queues_the_rule_mail_with_the_same_alert_as_the_channels(monkeypatch, kind):
    mod, calls = _load(FakeDB(), monkeypatch=monkeypatch)
    queued = _rule_hook(monkeypatch)
    mod._notify(kind, 55, _HOST, _TARGET, "CRITICAL", 900, 180, "no heartbeat", "Acme")
    k, alert = queued[0]
    assert k == kind and alert["id"] == 55 and alert["aws_account_id"] == 3 and alert["resource_id"] == "i-0abc"
    assert alert["severity"] == "CRITICAL" and alert["silenced"] is False
    assert calls["notify"][0][0] == kind


def test_watchdog_still_notifies_channels_when_the_rule_hook_raises(monkeypatch):
    mod, calls = _load(FakeDB(), monkeypatch=monkeypatch)
    _rule_hook(monkeypatch, raises=True)
    mod._notify("opened", 55, _HOST, _TARGET, "CRITICAL", 900, 180, "x", "Acme")      # must not raise
    assert len(calls["notify"]) == 1 and len(calls["publish"]) == 1


def test_watchdog_never_queues_for_a_resolve_or_a_silenced_alert():
    src = (ROOT / "app/collector/host_agent_watchdog.py").read_text()
    assert src.count("enqueue_channel_event") == 1                       # only inside _notify, which resolves never reach
    assert 'if item[0] == "resolved":' in src                            # resolves publish the websocket event only
    assert 'if escalated and not existing["silenced"]' in src and "if silence_reason:" in src
