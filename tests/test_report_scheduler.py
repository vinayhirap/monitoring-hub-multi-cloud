# tests/test_report_scheduler.py
"""
Email & Reports centre, patch 0002: the calendar maths in app/email/schedules.py, the runner in app/reports/scheduler.py
(claiming, recipients, dev / no-SMTP handling, generation, sending, outcome) and POST /api/email-center/schedules/{id}/run.
"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, contains  # noqa: E402
from tests.test_email_center import (  # noqa: E402,F401  (the fixtures restore every stub after each test)
    _api, _pure_modules, _restore_stubs, _enabled, install_stub, ADMIN, _SCHEDULE_ROW, _DbError, types_ns)

import pytest  # noqa: E402

UTC = timezone.utc
IST = {"enabled": 1, "run_hour": 8, "run_minute": 0, "timezone": "Asia/Kolkata"}


def _row(rtype, **kw):
    base = {"report_type": rtype, **IST}
    base.update({"WEEKLY": {"run_weekday": 0}, "MONTHLY": {"run_day": 1}, "QUARTERLY": {"run_day": 1},
                 "YEARLY": {"run_day": 1, "run_month": 4}}[rtype])
    base.update(kw)
    return base


def _utc(*a):
    return datetime(*a, tzinfo=UTC)


def _naive(*a):
    return datetime(*a)


# ── calendar maths ───────────────────────────────────────────────────

def test_weekly_runs_cover_the_previous_monday_to_sunday_in_the_schedule_timezone():
    _, sch, _ = _pure_modules()
    row = _row("WEEKLY")
    now = _utc(2026, 11, 4, 5, 0)                                    # Wed 4 Nov 10:30 IST
    occ = sch.latest_occurrence(row, now)
    assert occ.isoformat() == "2026-11-02T08:00:00+05:30"
    # Mon 26 Oct 00:00 IST .. Mon 2 Nov 00:00 IST, as naive UTC
    assert sch.period_for(row, occ) == (_naive(2026, 10, 25, 18, 30), _naive(2026, 11, 1, 18, 30))
    assert sch.next_occurrence(row, now).isoformat() == "2026-11-09T08:00:00+05:30"


def test_weekly_before_this_weeks_run_time_still_belongs_to_last_week():
    _, sch, _ = _pure_modules()
    row = _row("WEEKLY")
    assert sch.latest_occurrence(row, _utc(2026, 11, 2, 1, 0)).isoformat() == "2026-10-26T08:00:00+05:30"     # Mon 06:30 IST
    assert sch.latest_occurrence(row, _utc(2026, 11, 2, 2, 30)).isoformat() == "2026-11-02T08:00:00+05:30"    # Mon 08:00 IST exactly


def test_monthly_covers_the_previous_calendar_month_and_crosses_the_year():
    _, sch, _ = _pure_modules()
    row = _row("MONTHLY")
    occ = sch.latest_occurrence(row, _utc(2026, 1, 1, 3, 0))                       # 1 Jan 08:30 IST
    assert occ.isoformat() == "2026-01-01T08:00:00+05:30"
    assert sch.period_for(row, occ) == (_naive(2025, 11, 30, 18, 30), _naive(2025, 12, 31, 18, 30))   # December 2025
    assert sch.latest_occurrence(row, _utc(2026, 1, 1, 1, 0)).isoformat() == "2025-12-01T08:00:00+05:30"
    assert sch.next_occurrence(_row("MONTHLY", run_day=28), _utc(2026, 12, 29, 0, 0)).isoformat() == "2027-01-28T08:00:00+05:30"


def test_quarterly_runs_in_jan_apr_jul_oct_and_covers_the_previous_quarter():
    _, sch, _ = _pure_modules()
    row = _row("QUARTERLY")
    now = _utc(2026, 12, 15, 0, 0)
    occ = sch.latest_occurrence(row, now)
    assert occ.isoformat() == "2026-10-01T08:00:00+05:30"
    assert sch.period_for(row, occ) == (_naive(2026, 6, 30, 18, 30), _naive(2026, 9, 30, 18, 30))     # Q3: 1 Jul .. 1 Oct
    assert sch.next_occurrence(row, now).isoformat() == "2027-01-01T08:00:00+05:30"
    jan = sch.latest_occurrence(row, _utc(2026, 2, 10, 0, 0))
    assert jan.isoformat() == "2026-01-01T08:00:00+05:30"
    assert sch.period_for(row, jan) == (_naive(2025, 9, 30, 18, 30), _naive(2025, 12, 31, 18, 30))    # Q4 of 2025


def test_yearly_covers_the_twelve_months_before_the_run_month():
    _, sch, _ = _pure_modules()
    fy = _row("YEARLY", run_month=4)                                               # Indian financial year
    occ = sch.latest_occurrence(fy, _utc(2026, 11, 4, 0, 0))
    assert occ.isoformat() == "2026-04-01T08:00:00+05:30"
    assert sch.period_for(fy, occ) == (_naive(2025, 3, 31, 18, 30), _naive(2026, 3, 31, 18, 30))     # 1 Apr 2025 .. 1 Apr 2026
    assert sch.latest_occurrence(fy, _utc(2026, 2, 1, 0, 0)).isoformat() == "2025-04-01T08:00:00+05:30"
    cal = _row("YEARLY", run_month=1)                                              # calendar year
    occ = sch.latest_occurrence(cal, _utc(2026, 6, 1, 0, 0))
    assert sch.period_for(cal, occ) == (_naive(2024, 12, 31, 18, 30), _naive(2025, 12, 31, 18, 30))
    assert sch.next_occurrence(cal, _utc(2026, 6, 1, 0, 0)).isoformat() == "2027-01-01T08:00:00+05:30"


def test_period_boundaries_follow_daylight_saving_in_the_schedule_timezone():
    _, sch, _ = _pure_modules()
    row = _row("MONTHLY", timezone="America/New_York")
    occ = sch.latest_occurrence(row, _utc(2026, 4, 1, 15, 0))                       # 1 Apr 08:00 EDT
    start, end = sch.period_for(row, occ)
    assert start == _naive(2026, 3, 1, 5, 0)       # 1 Mar 00:00 EST (UTC-5)
    assert end == _naive(2026, 4, 1, 4, 0)         # 1 Apr 00:00 EDT (UTC-4): March has 743 hours that year, not 744


@pytest.mark.parametrize("rtype", ["WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY"])
def test_every_period_is_non_empty_and_ends_before_the_run(rtype):
    _, sch, _ = _pure_modules()
    row = _row(rtype)
    for day in range(1, 29):
        now = _utc(2026, (day % 12) + 1, day, 12, 0)
        occ = sch.latest_occurrence(row, now)
        start, end = sch.period_for(row, occ)
        assert start < end <= occ.astimezone(UTC).replace(tzinfo=None), (rtype, now)
        assert sch.next_occurrence(row, now) > now.astimezone(occ.tzinfo)


def test_due_runs_inside_the_catch_up_window_only():
    _, sch, _ = _pure_modules()
    row = _row("MONTHLY")
    created = _naive(2026, 1, 1)
    assert sch.due(row, _utc(2026, 11, 1, 3, 0), created) is not None              # 1 Nov 08:30 IST, 30 min after the run time
    assert sch.due(row, _utc(2026, 11, 2, 14, 0), created) is not None             # 35.5 h late (run time is 02:30 UTC): still sent
    assert sch.due(row, _utc(2026, 11, 2, 15, 0), created) is None                 # 36.5 h late: too late
    assert sch.due(row, _utc(2026, 11, 3, 3, 0), created) is None                  # > 36 h late: a stale report is not sent
    assert sch.due(row, _utc(2026, 11, 1, 1, 0), created) is None                  # 06:30 IST, run time not reached; October's 1st was long ago


def test_due_never_sends_the_same_period_twice_or_for_a_time_before_the_schedule_existed():
    _, sch, _ = _pure_modules()
    row, now = _row("MONTHLY"), _utc(2026, 11, 1, 3, 0)
    first = sch.due(row, now, _naive(2026, 1, 1))
    assert first["period_end"] == _naive(2026, 10, 31, 18, 30)
    assert sch.due(row, now, _naive(2026, 1, 1), last_period_end=first["period_end"]) is None
    assert sch.due(row, now, _naive(2026, 1, 1), last_period_end=_naive(2026, 9, 30, 18, 30)) is not None   # last month was sent, this one is owed
    assert sch.due(row, now, created_at=_naive(2026, 11, 1, 2, 59)) is None        # created after the 08:00 IST (02:30 UTC) run time
    assert sch.due(row, now, created_at=_naive(2026, 11, 1, 2, 0)) is not None     # created before it
    assert sch.due({**row, "enabled": 0}, now, _naive(2026, 1, 1)) is None


# ── runner ───────────────────────────────────────────────────────────

class _DB:
    """Scripted database for the runner. Each entry: (predicate, rows, rowcount). First match wins."""
    def __init__(self, script):
        self.script, self.log = list(script), []

    def cursor(self):
        db = self

        class Cur:
            rowcount, lastrowid, _rows = 1, 501, []

            def execute(self, sql, params=None):
                n = " ".join(sql.split())
                db.log.append((n, params))
                for pred, rows, rc in db.script:
                    if pred(n, params or ()):
                        self._rows, self.rowcount = rows, rc
                        return
                raise AssertionError(f"no script entry for {n!r}")

            def fetchone(self):
                return self._rows[0] if self._rows else None

            def fetchall(self):
                return list(self._rows)
        return Cur()


class _Ctx:
    def __init__(self, db):
        self.cur = db.cursor()

    def __enter__(self):
        return None, self.cur

    def __exit__(self, *a):
        return False


SCHED = {"id": 4, "name": "Monthly prod", "enabled": 1, "report_type": "MONTHLY", "scope_type": "ACCOUNT", "scope_id": "7",
         "account_id": 7, "run_day": 1, "run_hour": 8, "run_minute": 0, "timezone": "Asia/Kolkata", "recipient_list_id": 3,
         "extra_emails": "ext@partner.com", "created_at": _naive(2026, 1, 1), "last_period_end": None}
LIST_ROW = {"id": 3, "name": "Ops", "user_ids": None, "group_ids": None, "emails": "ops@aurionpro.com"}
REPORT = {"id": 90, "job_id": 501, "s3_key": "k", "sha256": "h", "report_type": "MONTHLY"}
PERIOD = {"period_start": _naive(2026, 9, 30, 18, 30), "period_end": _naive(2026, 10, 31, 18, 30)}


def _runner(monkeypatch, *, reports=True, smtp=True, job_status="COMPLETE", send=None, run_job=None, extra_script=()):
    """The scheduler module with the database, mailer, delivery log, report worker and S3 replaced. Returns a namespace."""
    _pure_modules()
    script = [(contains("FROM email_recipient_lists"), [LIST_ROW], 1),
              (contains("UPDATE report_schedules SET last_run_at"), [], 1),
              (contains("INSERT INTO report_jobs"), [], 1),
              (contains("FROM report_jobs WHERE id"), [{"status": job_status, "error_message": "renderer crashed"}], 1),
              (contains("FROM reports WHERE job_id"), [REPORT], 1),
              *extra_script]
    db = _DB(script)
    ns = types_ns(db=db, skipped=[], audit=[], mails=[], jobs=[])
    install_stub("app.db", get_db_cursor=lambda dictionary=True, commit=True: _Ctx(db))
    install_stub("app.audit", write_audit=lambda *a, **k: ns.audit.append(a))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)

    def record(category, to, subject, status, error=None, report_id=None, schedule_id=None):
        ns.skipped.append((category, to, status, error, schedule_id))
        return True
    from app.email import delivery_log as real_dl  # the stub registered by _pure_modules
    real_dl.record = record

    def send_ex(to, report, pdf, schedule_id=None):
        ns.mails.append((to, report["id"], pdf, schedule_id))
        return send(to) if send else (True, "")
    install_stub("app.email.mailer", is_configured=lambda: smtp, send_report_email_ex=send_ex)
    install_stub("app.reports.s3_client", get_report_bytes=lambda key, sha: b"%PDF-1")
    install_stub("app.reports.worker", run_job=run_job or (lambda job_id: ns.jobs.append(job_id)))
    monkeypatch.setenv("REPORTS_ENABLED", "true" if reports else "false")
    monkeypatch.delenv("REPORT_EMAIL_ALLOWED_DOMAINS", raising=False)
    mod = load_module("app/reports/scheduler.py")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    ns.mod = mod
    ns.outcome = lambda: [p for sql, p in db.log if sql.startswith("UPDATE report_schedules SET last_run_at")]
    return ns


def test_a_full_run_creates_the_job_as_the_system_user_and_mails_everyone_once_each(monkeypatch):
    r = _runner(monkeypatch)
    out = r.mod.execute(SCHED, PERIOD)
    assert out["status"] == "SENT" and out["sent"] == 2 and out["failed"] == 0
    insert = [p for sql, p in r.db.log if sql.startswith("INSERT INTO report_jobs")][0]
    assert insert == ("MONTHLY", "ACCOUNT", "7", 7, PERIOD["period_start"], PERIOD["period_end"], "scheduler", "admin")
    assert r.jobs == [501]
    assert sorted(m[0] for m in r.mails) == ["ext@partner.com", "ops@aurionpro.com"]
    assert all(m[1] == 90 and m[2] == b"%PDF-1" and m[3] == 4 for m in r.mails)       # one report, tagged with the schedule
    assert r.outcome()[-1] == ("SENT", None, 4) and r.audit[0][1] == "Scheduled report emailed"


def test_a_failed_recipient_makes_the_run_partial_and_all_failing_makes_it_failed(monkeypatch):
    r = _runner(monkeypatch, send=lambda to: (False, "mailbox full") if to.startswith("ext") else (True, ""))
    out = r.mod.execute(SCHED, PERIOD)
    assert out["status"] == "PARTIAL" and out["sent"] == 1 and out["failed"] == 1
    assert "ext@partner.com: mailbox full" in r.outcome()[-1][1]
    r2 = _runner(monkeypatch, send=lambda to: (False, "login rejected"))
    assert r2.mod.execute(SCHED, PERIOD)["status"] == "FAILED"


def test_dev_without_reports_does_everything_but_the_pdf_and_logs_a_skip_per_recipient(monkeypatch):
    r = _runner(monkeypatch, reports=False)
    out = r.mod.execute(SCHED, PERIOD)
    assert out["status"] == "SKIPPED" and "REPORTS_ENABLED" in out["error"]
    assert r.mails == [] and r.jobs == [] and not any(sql.startswith("INSERT INTO report_jobs") for sql, _ in r.db.log)
    assert sorted(s[1] for s in r.skipped) == ["ext@partner.com", "ops@aurionpro.com"]
    assert all(s[0] == "report" and s[2] == "skipped" and s[4] == 4 and "REPORTS_ENABLED" in s[3] for s in r.skipped)


def test_without_smtp_no_pdf_is_generated_and_the_skip_says_why(monkeypatch):
    r = _runner(monkeypatch, smtp=False)
    out = r.mod.execute(SCHED, PERIOD)
    assert out["status"] == "SKIPPED" and "SMTP" in out["error"] and r.jobs == [] and r.mails == []
    assert len(r.skipped) == 2 and "SMTP_HOST" in r.skipped[0][3]


def test_a_schedule_with_nobody_to_send_to_fails_loudly(monkeypatch):
    r = _runner(monkeypatch, extra_script=[])
    r.db.script.insert(0, (contains("FROM email_recipient_lists"), [{"id": 3, "user_ids": None, "group_ids": None, "emails": None}], 1))
    out = r.mod.execute({**SCHED, "extra_emails": None}, PERIOD)
    assert out["status"] == "FAILED" and "no one to send to" in out["error"] and r.mails == []


def test_the_allowed_domain_policy_is_applied_and_logged_as_skipped_by_the_caller(monkeypatch):
    r = _runner(monkeypatch)
    monkeypatch.setenv("REPORT_EMAIL_ALLOWED_DOMAINS", "aurionpro.com")
    out = r.mod.execute(SCHED, PERIOD)
    assert out["status"] == "SENT" and [m[0] for m in r.mails] == ["ops@aurionpro.com"]
    addresses, skipped = r.mod.resolve_recipients(SCHED)
    assert addresses == ["ops@aurionpro.com"] and skipped[0]["who"] == "ext@partner.com"


def test_a_report_that_cannot_be_generated_fails_the_run_without_sending_anything(monkeypatch):
    r = _runner(monkeypatch, job_status="FAILED")
    out = r.mod.execute(SCHED, PERIOD)
    assert out["status"] == "FAILED" and "renderer crashed" in out["error"] and r.mails == []


def test_a_job_that_never_finishes_times_out_instead_of_hanging(monkeypatch):
    r = _runner(monkeypatch, job_status="PROCESSING")
    clock = iter(range(0, 100000, 400))                      # each poll "takes" 400 s
    monkeypatch.setattr(r.mod.time, "monotonic", lambda: next(clock))
    out = r.mod.execute(SCHED, PERIOD)
    assert out["status"] == "FAILED" and "not ready after" in out["error"]


def test_execute_never_raises(monkeypatch):
    def boom(job_id):
        raise RuntimeError("worker exploded")
    r = _runner(monkeypatch, run_job=boom)
    out = r.mod.execute(SCHED, PERIOD)
    assert out["status"] == "FAILED" and "worker exploded" in out["error"]


def _tick_script(sched, claim_rowcount):
    return [(contains("SELECT * FROM report_schedules WHERE enabled"), [sched], 1),
            (contains("UPDATE report_schedules SET last_period_end"), [], claim_rowcount)]


def test_tick_claims_the_period_then_runs_it_once(monkeypatch):
    r = _runner(monkeypatch, extra_script=_tick_script(SCHED, 1))
    ran = []
    monkeypatch.setattr(r.mod, "execute", lambda sched, period: ran.append(period) or {})
    assert r.mod.tick(now=_utc(2026, 11, 1, 3, 0)) == 1
    claim = [p for sql, p in r.db.log if sql.startswith("UPDATE report_schedules SET last_period_end")][0]
    assert claim == (_naive(2026, 10, 31, 18, 30), 4, None) and ran[0]["period_end"] == claim[0]


def test_tick_does_not_run_a_period_another_instance_already_claimed(monkeypatch):
    r = _runner(monkeypatch, extra_script=_tick_script(SCHED, 0))
    ran = []
    monkeypatch.setattr(r.mod, "execute", lambda sched, period: ran.append(period))
    assert r.mod.tick(now=_utc(2026, 11, 1, 3, 0)) == 0 and ran == []


def test_tick_ignores_schedules_that_are_not_due_or_were_already_sent(monkeypatch):
    done = {**SCHED, "last_period_end": _naive(2026, 10, 31, 18, 30)}
    r = _runner(monkeypatch, extra_script=_tick_script(done, 1))
    monkeypatch.setattr(r.mod, "execute", lambda sched, period: pytest.fail("must not run"))
    assert r.mod.tick(now=_utc(2026, 11, 1, 3, 0)) == 0
    r2 = _runner(monkeypatch, extra_script=_tick_script(SCHED, 1))
    monkeypatch.setattr(r2.mod, "execute", lambda sched, period: pytest.fail("must not run"))
    assert r2.mod.tick(now=_utc(2026, 11, 20, 3, 0)) == 0                        # a missed monthly run is not sent three weeks late


def test_tick_survives_a_broken_database_and_a_broken_schedule(monkeypatch):
    r = _runner(monkeypatch, extra_script=[(contains("SELECT * FROM report_schedules WHERE enabled"), [{"id": 9, "enabled": 1, "report_type": "MONTHLY"}], 1)])
    assert r.mod.tick(now=_utc(2026, 11, 1, 3, 0)) == 0                          # the malformed row raises inside due(): logged, not fatal
    install_stub("app.db", get_db_cursor=lambda **k: (_ for _ in ()).throw(RuntimeError("db down")))
    r3 = load_module("app/reports/scheduler.py")
    assert r3.tick() == 0


def test_the_loop_stops_when_leadership_is_lost(monkeypatch):
    r = _runner(monkeypatch)
    import threading
    ev = threading.Event()
    r.mod.run_scheduler_loop(ev)                                                 # not leader: returns at once
    ev.set()
    calls = []
    monkeypatch.setattr(r.mod, "tick", lambda: calls.append(1) or ev.clear())
    r.mod.run_scheduler_loop(ev, interval_seconds=0)
    assert calls == [1]


def test_main_starts_the_scheduler_thread_only_when_the_centre_is_enabled():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app/main.py").read_text()
    assert 'name="report-scheduler"' in src and 'os.getenv("EMAIL_CENTER_ENABLED"' in src


# ── run now ──────────────────────────────────────────────────────────

def _run_script(row=_SCHEDULE_ROW):
    return [(contains("SELECT * FROM report_schedules WHERE id"), [row]),
            (contains("SELECT 1 AS ok FROM aws_accounts"), [{"ok": 1}]),
            (contains("UPDATE report_schedules SET last_status = 'RUNNING'"), [])]


def _run_api(monkeypatch, row=_SCHEDULE_ROW, reports=True, smtp=True):
    mod, log, _, audit = _api(_run_script(row), smtp={} if smtp else {"host": ""})
    started = []
    install_stub("app.reports.scheduler", resolve_recipients=lambda r: (["a@b.co"], [{"who": "bob", "reason": "no email on file"}]),
                 execute=lambda sched, period: started.append((sched["id"], period)))
    monkeypatch.setenv("REPORTS_ENABLED", "true" if reports else "false")
    return mod, log, audit, started


def test_run_now_dry_run_reports_period_recipients_and_outcome_without_touching_anything(monkeypatch):
    from fastapi import BackgroundTasks
    mod, log, audit, started = _run_api(monkeypatch)
    bg = BackgroundTasks()
    out = mod.run_schedule_now(schedule_id=77, background_tasks=bg, request=None, payload={"dry_run": True}, current_user=ADMIN)
    assert out["dry_run"] is True and out["recipients"] == ["a@b.co"] and out["skipped"][0]["who"] == "bob"
    assert out["would"] == "send the report to everyone listed" and out["period_start"].endswith("Z")
    assert bg.tasks == [] and started == [] and not any(sql.startswith("UPDATE") for sql, _ in log) and audit == []


@pytest.mark.parametrize("reports,smtp,expect", [(False, True, "REPORTS_ENABLED"), (True, False, "SMTP")])
def test_run_now_dry_run_says_why_nothing_would_be_sent(monkeypatch, reports, smtp, expect):
    from fastapi import BackgroundTasks
    mod, *_ = _run_api(monkeypatch, reports=reports, smtp=smtp)
    out = mod.run_schedule_now(schedule_id=77, background_tasks=BackgroundTasks(), request=None, payload={"dry_run": True}, current_user=ADMIN)
    assert out["would"].startswith("not send") and expect in out["would"]


def test_run_now_marks_the_schedule_running_queues_the_work_and_audits(monkeypatch):
    from fastapi import BackgroundTasks
    mod, log, audit, started = _run_api(monkeypatch)
    bg = BackgroundTasks()
    out = mod.run_schedule_now(schedule_id=77, background_tasks=bg, request=None, payload=None, current_user=ADMIN)
    assert out["status"] == "started" and len(bg.tasks) == 1 and audit[0][1] == "Report schedule run now"
    assert any(sql.startswith("UPDATE report_schedules SET last_status = 'RUNNING'") for sql, _ in log)
    assert not any("last_period_end" in sql for sql, _ in log)                  # run now never uses up the scheduled period
    bg.tasks[0].func(*bg.tasks[0].args)
    assert started[0][0] == 77 and started[0][1]["period_start"] < started[0][1]["period_end"]


def test_run_now_refuses_a_schedule_that_is_already_running_and_a_missing_one(monkeypatch):
    from fastapi import BackgroundTasks
    busy = {**_SCHEDULE_ROW, "last_status": "RUNNING", "last_run_at": datetime.utcnow() - timedelta(minutes=2)}
    mod, *_ = _run_api(monkeypatch, row=busy)
    with pytest.raises(Exception) as e:
        mod.run_schedule_now(schedule_id=77, background_tasks=BackgroundTasks(), request=None, payload={}, current_user=ADMIN)
    assert e.value.status_code == 409
    stale = {**busy, "last_run_at": datetime.utcnow() - timedelta(hours=3)}      # a crashed run does not block forever
    mod2, *_ = _run_api(monkeypatch, row=stale)
    assert mod2.run_schedule_now(schedule_id=77, background_tasks=BackgroundTasks(), request=None, payload={}, current_user=ADMIN)["status"] == "started"
    mod3, *_ = _api([(contains("SELECT * FROM report_schedules WHERE id"), [])])
    install_stub("app.reports.scheduler", resolve_recipients=lambda r: ([], []), execute=lambda *a: None)
    with pytest.raises(Exception) as e3:
        mod3.run_schedule_now(schedule_id=5, background_tasks=BackgroundTasks(), request=None, payload={}, current_user=ADMIN)
    assert e3.value.status_code == 404


def test_schedule_listing_shows_the_next_run_for_enabled_schedules_only():
    script = [(contains("SELECT * FROM report_schedules"), [_SCHEDULE_ROW, {**_SCHEDULE_ROW, "id": 78, "name": "Off", "enabled": 0}]),
              (contains("SELECT id, name FROM email_recipient_lists"), [{"id": 3, "name": "Ops"}])]
    mod, *_ = _api(script)
    out = mod.list_schedules(current_user=ADMIN)
    assert out[0]["next_run_at"].endswith("Z") and datetime.fromisoformat(out[0]["next_run_at"][:-1]) > datetime.utcnow()
    assert out[1]["next_run_at"] is None
