# tests/test_escalation_email.py
"""
Coverage for app/collector/escalation.py's _notify_escalation() --
wired to app/email/mailer.py's already-functional SMTP sender
(2026-09-14), previously a stub that never actually emailed anyone.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub, FakeCursor, FakeConn


def _install_stub(member_rows, is_configured=True, send_results=None):
    """send_results: dict email -> bool (send_email return value),
    defaults to True (success) for every recipient not explicitly
    listed."""
    send_results = send_results or {}
    sent_calls = []

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT DISTINCT u.email"):
                self._pending = member_rows
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    install_stub("app.collector.op_log", log_event=lambda *a, **k: None)

    def _fake_send_email(to_addr, subject, body):
        sent_calls.append((to_addr, subject, body))
        return send_results.get(to_addr, True)

    install_stub(
        "app.email.mailer",
        is_configured=lambda: is_configured,
        get_public_app_url=lambda: "https://cloudops.example.com",
        send_email=_fake_send_email,
    )
    return sent_calls


def test_emails_every_group_member_with_an_address():
    members = [{"email": "l2-oncall@example.com"}, {"email": "platform-lead@example.com"}]
    sent = _install_stub(members)
    mod = load_module("app/collector/escalation.py")

    mod._notify_escalation(101, 5, "L2-Platform", "CRITICAL", "CPUUtilization", "i-abc")

    assert len(sent) == 2
    addrs = {call[0] for call in sent}
    assert addrs == {"l2-oncall@example.com", "platform-lead@example.com"}
    # Subject/body should carry the actual alert context, not a canned
    # placeholder string.
    assert "CRITICAL" in sent[0][1]
    assert "L2-Platform" in sent[0][1]
    assert "i-abc" in sent[0][2]
    assert "https://cloudops.example.com/alerts" in sent[0][2]


def test_skips_sending_when_smtp_not_configured():
    sent = _install_stub(member_rows=[{"email": "a@example.com"}], is_configured=False)
    mod = load_module("app/collector/escalation.py")

    mod._notify_escalation(101, 5, "L2-Platform", "CRITICAL", "CPUUtilization", "i-abc")

    assert sent == []  # never even looked up members / attempted a send


def test_no_members_with_email_sends_nothing():
    sent = _install_stub(member_rows=[])
    mod = load_module("app/collector/escalation.py")

    mod._notify_escalation(101, 5, "L2-Platform", "CRITICAL", "CPUUtilization", "i-abc")

    assert sent == []


def test_partial_send_failure_does_not_raise():
    members = [{"email": "good@example.com"}, {"email": "bad@example.com"}]
    sent = _install_stub(members, send_results={"bad@example.com": False})
    mod = load_module("app/collector/escalation.py")

    # Must not raise even though one recipient's send failed.
    mod._notify_escalation(101, 5, "L2-Platform", "CRITICAL", "CPUUtilization", "i-abc")

    assert len(sent) == 2
