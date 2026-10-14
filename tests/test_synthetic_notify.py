# tests/test_synthetic_notify.py -- who is told when a synthetic check goes down or its certificate nears expiry
# (app/collector/synthetic_notify.py). Before this, those events only created an alert row; nothing was ever sent.
import sys
from datetime import datetime, timedelta

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import pytest
from tests.conftest import load_module, install_stub

nt = None


@pytest.fixture(autouse=True)
def _load_notifier():
    """Loaded per test with stubbed collaborators, so nothing real stays in sys.modules for other test files."""
    global nt
    install_stub("app.email.mailer", is_configured=lambda: False, send_email=lambda *a: False,
                 get_public_app_url=lambda: "")
    install_stub("app.notifications.sender", notify_alert_event=lambda *a, **k: None)
    nt = load_module("app/collector/synthetic_notify.py")
    yield
NOW = datetime(2026, 10, 14, 6, 0, 0)
NOT_AFTER = datetime(2026, 10, 24, 12, 0, 0)


# ── pure decisions ───────────────────────────────────────────────────

def test_cert_stage_boundaries():
    assert nt.cert_stage(None, 30, 7) is None
    assert nt.cert_stage(31, 30, 7) is None
    assert nt.cert_stage(30, 30, 7) == "warn"
    assert nt.cert_stage(8, 30, 7) == "warn"
    assert nt.cert_stage(7, 30, 7) == "crit"
    assert nt.cert_stage(0, 30, 7) == "crit"          # less than a day left is still not expired
    assert nt.cert_stage(-1, 30, 7) == "expired"


def test_cert_stage_honours_a_checks_own_thresholds():
    assert nt.cert_stage(20, 14, 3) is None
    assert nt.cert_stage(14, 14, 3) == "warn"
    assert nt.cert_stage(3, 14, 3) == "crit"


def test_cert_action_first_escalated_reminder_and_quiet():
    assert nt.cert_action(None, "warn", NOW) == "first"
    assert nt.cert_action(None, None, NOW) is None
    prev = {"stage": "warn", "last_sent_at": NOW - timedelta(days=1)}
    assert nt.cert_action(prev, "warn", NOW) is None                                   # sent yesterday: quiet
    assert nt.cert_action(prev, "crit", NOW) == "escalated"                            # worse stage: immediately
    assert nt.cert_action({"stage": "warn", "last_sent_at": NOW - timedelta(days=7)}, "warn", NOW) == "reminder"
    assert nt.cert_action({"stage": "crit", "last_sent_at": NOW - timedelta(hours=25)}, "crit", NOW) == "reminder"
    assert nt.cert_action({"stage": "crit", "last_sent_at": NOW - timedelta(hours=3)}, "crit", NOW) is None
    assert nt.cert_action({"stage": "crit", "last_sent_at": NOW}, "warn", NOW) is None  # a lower stage never re-notifies


def test_messages_name_the_check_the_date_and_the_action_and_nothing_secret():
    check = {"id": 4, "name": "Public site", "target": "https://www.example.com/health", "aws_account_id": 2,
             "environment": "prod"}
    tls = {"cert_not_after": NOT_AFTER, "cert_issuer": "Let's Encrypt", "cert_subject": "www.example.com"}
    subject, body = nt.build_cert_message(check, "crit", "first", 3, tls, "AuroGov")
    assert "CRITICAL" in subject and "3 days" in subject and "Public site" in subject
    assert "2026-10-24 12:00 UTC" in body and "www.example.com" in body and "renew" in body.lower() and "AuroGov" in body
    assert nt.build_cert_message(check, "crit", "reminder", 3, tls)[0].startswith("Reminder: ")
    assert "EXPIRED" in nt.build_cert_message(check, "expired", "first", -2, tls)[0]
    assert "1 day" in nt.build_cert_message(check, "crit", "first", 1, tls)[0]
    down = nt.build_outage_message(dict(check, consecutive_failures=3), True, "Connection refused")
    assert down[0] == "[CloudOps] DOWN: Public site" and "Connection refused" in down[1]
    assert "Recovered" in nt.build_outage_message(check, False, minutes=12)[0]


# ── the state machine against an in-memory database ──────────────────

class FakeDB:
    def __init__(self, contacts):
        self.contacts = contacts            # [(email, notify_outage, notify_cert)]
        self.state = {}                     # (check_id, kind, key) -> {stage, last_sent_at}
        self.log = []
        self.sent = []
        self.committed = 0
        self._rows = []

    # cursor + connection in one object
    def cursor(self, dictionary=True):
        return self

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        self._rows = []
        if "FROM synthetic_check_contacts" in q:
            col = 1 if "notify_outage = 1" in q else 2
            self._rows = [{"email": c[0]} for c in self.contacts if c[col]]
        elif q.startswith("SELECT") and "FROM synthetic_notification_state" in q:
            self._rows = sorted(({"subject_key": k[2], "stage": v["stage"], "last_sent_at": v["last_sent_at"]}
                                 for k, v in self.state.items() if k[0] == params[0]),
                                key=lambda r: r["subject_key"], reverse=True)
        elif q.startswith("DELETE FROM synthetic_notification_state"):
            for k in [k for k in self.state if k[0] == params[0] and k[2] < params[1]]:
                del self.state[k]
        elif q.startswith("INSERT INTO synthetic_notification_state"):
            self.state[(params[0], "cert", params[1])] = {"stage": params[2], "last_sent_at": params[3]}
        elif "FROM aws_accounts" in q:
            self._rows = [{"account_name": "AuroGov"}]
        elif q.startswith("INSERT INTO notification_log"):
            self.log.append(params)
        elif "FROM alerts" in q:
            self._rows = []                 # no alert row: the global-channel fan-out has nothing to send

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeMailer:
    def __init__(self, db, configured=True):
        self.db, self.configured = db, configured

    def is_configured(self):
        return self.configured

    def get_public_app_url(self):
        return "https://cloudops.example"

    def send_email(self, to, subject, body):
        self.db.sent.append((to, subject))
        return True


CHECK = {"id": 4, "name": "Public site", "target": "https://www.example.com", "aws_account_id": 2, "environment": "prod"}


def tls(days, not_after=NOT_AFTER):
    return {"cert_days_left": days, "cert_not_after": not_after, "cert_issuer": "CA", "cert_subject": "www.example.com"}


def run(db, days, now, not_after=NOT_AFTER, silenced=None):
    nt.on_certificate(db, db, CHECK, tls(days, not_after), 30, 7, silenced, now)


def setup(contacts, configured=True):
    db = FakeDB(contacts)
    nt.mailer = FakeMailer(db, configured)
    return db


def test_first_notice_goes_only_to_contacts_who_want_certificates():
    db = setup([("owner@x.com", 1, 1), ("netops@x.com", 1, 0), ("secops@x.com", 0, 1)])
    run(db, 20, NOW)
    assert sorted(t for t, _ in db.sent) == ["owner@x.com", "secops@x.com"]       # netops asked for outages only
    assert (4, "cert", "2026-10-24 12:00:00") in db.state


def test_the_same_stage_is_not_repeated_until_the_reminder_is_due():
    db = setup([("owner@x.com", 1, 1)])
    run(db, 20, NOW)
    run(db, 20, NOW + timedelta(minutes=5))
    run(db, 19, NOW + timedelta(days=3))
    assert len(db.sent) == 1
    run(db, 13, NOW + timedelta(days=7))
    assert len(db.sent) == 2 and db.sent[1][1].startswith("Reminder: ")


def test_a_worse_stage_is_sent_immediately():
    db = setup([("owner@x.com", 1, 1)])
    run(db, 20, NOW)
    run(db, 6, NOW + timedelta(minutes=5))
    assert len(db.sent) == 2 and "CRITICAL" in db.sent[1][1]


def test_a_renewed_certificate_gets_one_all_clear_and_a_fresh_start():
    db = setup([("owner@x.com", 1, 1)])
    run(db, 6, NOW)
    run(db, 365, NOW + timedelta(hours=1), not_after=datetime(2027, 10, 20, 12, 0, 0))
    assert db.sent[-1][1].startswith("[CloudOps] Certificate renewed")
    assert list(db.state) == []                                       # the old certificate's state is gone
    run(db, 364, NOW + timedelta(hours=2), not_after=datetime(2027, 10, 20, 12, 0, 0))
    assert len(db.sent) == 2                                          # and nothing further while it is healthy


def test_nothing_is_marked_sent_when_nobody_could_be_told():
    no_contacts = setup([])
    run(no_contacts, 5, NOW)
    assert no_contacts.sent == [] and no_contacts.state == {}         # a contact added later is told at the next probe
    no_smtp = setup([("owner@x.com", 1, 1)], configured=False)
    run(no_smtp, 5, NOW)
    assert no_smtp.sent == [] and no_smtp.state == {}
    assert any(p[2] == "skipped" for p in no_smtp.log)                # and the log says why


def test_maintenance_windows_silence_notices():
    db = setup([("owner@x.com", 1, 1)])
    run(db, 5, NOW, silenced="Maintenance window: patching")
    assert db.sent == [] and db.state == {}


def test_no_certificate_seen_changes_nothing():
    db = setup([("owner@x.com", 1, 1)])
    nt.on_certificate(db, db, CHECK, {"cert_days_left": None, "cert_not_after": None}, 30, 7, None, NOW)
    assert db.sent == [] and db.state == {}


def test_outage_and_recovery_go_to_outage_contacts_only():
    db = setup([("owner@x.com", 1, 1), ("secops@x.com", 0, 1)])
    nt.on_outage_change(db, db, dict(CHECK, consecutive_failures=3), True, "timeout")
    assert [t for t, _ in db.sent] == ["owner@x.com"] and db.sent[0][1] == "[CloudOps] DOWN: Public site"
    nt.on_outage_change(db, db, dict(CHECK, consecutive_failures=0), False)
    assert [s for _, s in db.sent][-1].startswith("[CloudOps] Recovered")
    nt.on_outage_change(db, db, CHECK, True, "timeout", silenced="Maintenance window: x")
    assert len(db.sent) == 2


def test_a_mail_failure_never_raises_into_the_probe_loop():
    db = setup([("owner@x.com", 1, 1)])

    class Boom(FakeMailer):
        def send_email(self, *a):
            raise RuntimeError("smtp exploded")
    nt.mailer = Boom(db)
    run(db, 5, NOW)                                                  # must not raise
    assert db.state == {}                                            # and the stage is retried at the next probe
