# tests/test_email_center.py
"""
Email & Reports centre, foundation (patch 0001): migration 135, recipient lists, schedule validation, the delivery log,
the mailer hook, the YEARLY report type and the /api/email-center routes.

Pure modules (recipients, schedules, delivery_log) are loaded by path; the API module is loaded with the database, audit
and auth layers stubbed, and its endpoint functions are called directly (every argument passed, as the other API tests do).
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, contains  # noqa: E402

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "db/migrations/135_email_center.sql"


# ── helpers ──────────────────────────────────────────────────────────

_RESTORE = []
_MISSING = object()


def install_stub(name, **attrs):
    """Like tests.conftest.install_stub, but it ALWAYS installs a fresh module and puts back exactly what was there
    afterwards (module in sys.modules and the attribute on its parent package). conftest's version mutates a real module
    that another test already imported, which leaked into test_email_templates / test_section_reports."""
    import types as _types
    mod = _types.ModuleType(name)
    mod.__dict__.update(attrs)
    parts = name.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            pm = _types.ModuleType(parent)
            pm.__path__ = []
            _RESTORE.append(("module", parent, _MISSING))
            sys.modules[parent] = pm
    _RESTORE.append(("module", name, sys.modules.get(name, _MISSING)))
    sys.modules[name] = mod
    parent_mod = sys.modules.get(".".join(parts[:-1])) if len(parts) > 1 else None
    if parent_mod is not None:
        _RESTORE.append(("attr", (parent_mod, parts[-1]), getattr(parent_mod, parts[-1], _MISSING)))
        setattr(parent_mod, parts[-1], mod)
    return mod


@pytest.fixture(autouse=True)
def _restore_stubs():
    yield
    for kind, key, old in reversed(_RESTORE):
        if kind == "module":
            if old is _MISSING:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = old
        else:
            holder, attr = key
            if old is _MISSING:
                if hasattr(holder, attr):
                    delattr(holder, attr)
            else:
                setattr(holder, attr, old)
    _RESTORE.clear()


def _pure_modules():
    """recipients + schedules + delivery_log, loaded by path and registered so `from app.email import X` resolves."""
    rec = load_module("app/email/recipients.py")
    install_stub("app.email.recipients", **{k: v for k, v in vars(rec).items() if not k.startswith("__")})
    sch = load_module("app/email/schedules.py")
    install_stub("app.email.schedules", **{k: v for k, v in vars(sch).items() if not k.startswith("__")})
    dl = load_module("app/email/delivery_log.py")
    install_stub("app.email.delivery_log", **{k: v for k, v in vars(dl).items() if not k.startswith("__")})
    ar = load_module("app/email/alert_rules.py")                 # patch 0004: the API module imports it
    install_stub("app.email.alert_rules", **{k: v for k, v in vars(ar).items() if not k.startswith("__")})
    return rec, sch, dl


class _Cur:
    """Scripted dictionary cursor: (predicate, rows | Exception). First match wins, no match fails loudly."""
    lastrowid = 77

    def __init__(self, script, log):
        self.script, self.log, self._rows = script, log, []

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        self.log.append((norm, params))
        for pred, result in self.script:
            if pred(norm, params or ()):
                if isinstance(result, Exception):
                    raise result
                self._rows = result
                return
        raise AssertionError(f"no script entry for: {norm!r} params={params!r}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Ctx:
    def __init__(self, script, log):
        self.cur = _Cur(script, log)

    def __enter__(self):
        return (None, self.cur)

    def __exit__(self, *exc):
        return False


class _DbError(Exception):
    def __init__(self, errno):
        super().__init__(f"db error {errno}")
        self.errno = errno


def _api(script=(), smtp=None, perms=None, accessible=None, registry=None, public_url_problem=""):
    """The API module with everything it touches stubbed. Returns (module, executed-sql log, mails sent, audit rows)."""
    _pure_modules()
    log, mails, audit = [], [], []
    script = list(script)
    install_stub("app.db", get_db_cursor=lambda dictionary=True, commit=True: _Ctx(script, log))
    install_stub("app.audit", write_audit=lambda *a, **k: audit.append(a))
    granted = set(perms) if perms is not None else None
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None),
                 has_permission=lambda user, code: user.get("role") == "admin" or (granted is not None and code in granted))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: accessible)
    cfg = {"host": "smtp.example.com", "port": 587, "security": "starttls", "username": "u", "password": "secret-pw",
           "mail_from": "Ops <ops@example.com>", "timeout": 15, "ehlo": None}
    cfg.update(smtp or {})

    def send_email_ex(*a, **k):
        mails.append((a, k))
        return True, ""
    install_stub("app.email.mailer", _settings=lambda: cfg, is_configured=lambda: bool(cfg["host"]),
                 config_problems=lambda: [], get_public_app_url=lambda: "https://cloudops.example.com",
                 public_url_problem=lambda: public_url_problem, send_email_ex=send_email_ex)
    install_stub("app.email.templates",
                 test_notification_email=lambda url, name: {"subject": "Test", "text": "t", "html": "<p>t</p>"})
    reg = registry or types_ns(is_section=lambda s: s in ("COST", "HOST", "ENVIRONMENT", "SECURITY", "SLO"),
                               validate=lambda stype, scope_id, acct, user: acct)
    install_stub("app.reports.registry", **vars(reg))
    install_stub("app.reports.synthetic_report", parse_scope=lambda s: (int(str(s).split(":")[0]), None))
    return load_module("app/api/email_center.py"), log, mails, audit


def types_ns(**kw):
    import types
    ns = types.SimpleNamespace(**kw)
    return ns


ADMIN = {"id": 1, "username": "vinay", "role": "admin"}
EDITOR = {"id": 2, "username": "priya", "role": "editor"}


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("EMAIL_CENTER_ENABLED", "true")
    monkeypatch.delenv("REPORT_EMAIL_ALLOWED_DOMAINS", raising=False)


# ── migration 135 ────────────────────────────────────────────────────

def test_migration_is_idempotent_and_mariadb_safe():
    sql = MIGRATION.read_text()
    assert len(re.findall(r"^CREATE TABLE IF NOT EXISTS", sql, re.M)) == 3
    assert not re.search(r"^CREATE TABLE (?!IF NOT EXISTS)", sql, re.M)
    assert "INSERT IGNORE INTO permissions" in sql
    assert "0900" not in sql and "COLLATE" not in sql.upper()      # a MySQL-8-only collation would break MariaDB


def test_migration_adds_yearly_to_both_report_tables_at_the_end_of_the_enum():
    sql = MIGRATION.read_text()
    for table in ("report_jobs", "reports"):
        m = re.search(rf"ALTER TABLE {table}\s+MODIFY COLUMN report_type ENUM\(([^)]*)\) NOT NULL;", sql)
        assert m, table
        assert re.findall(r"'(\w+)'", m.group(1)) == ["WEEKLY", "MONTHLY", "QUARTERLY", "CUSTOM", "YEARLY"]


def test_migration_grants_view_to_editor_and_admin_and_manage_to_admin_only():
    sql = MIGRATION.read_text()
    legacy = re.findall(r"INSERT IGNORE INTO role_permissions \(role, permission_id\)\s+SELECT '(\w+)', id FROM permissions "
                        r"WHERE code IN \(([^)]*)\)", sql)
    got = {role: set(re.findall(r"'([a-z_.]+)'", codes)) for role, codes in legacy}
    assert got == {"editor": {"emailcenter.view"}, "admin": {"emailcenter.view", "emailcenter.manage"}}
    v2 = re.findall(r"WHERE r\.role_key = '(\w+)' AND p\.code IN \(([^)]*)\)", sql)
    assert {role: set(re.findall(r"'([a-z_.]+)'", codes)) for role, codes in v2} == got


# ── recipients ───────────────────────────────────────────────────────

def test_parse_emails_dedupes_case_insensitively_and_keeps_first_spelling():
    rec = load_module("app/email/recipients.py")
    assert rec.parse_emails("Ops@Example.com, ops@example.com; a@b.co\nc@d.io") == ["Ops@Example.com", "a@b.co", "c@d.io"]
    assert rec.parse_emails(None) == [] and rec.parse_emails("") == [] and rec.parse_emails([]) == []


@pytest.mark.parametrize("bad", ["nobody", "a@b", "a b@c.com", "a@b.com\r\nBcc: x@y.com", "<a@b.com>", 'a"@b.com'])
def test_parse_emails_rejects_bad_and_header_injection(bad):
    rec = load_module("app/email/recipients.py")
    with pytest.raises(ValueError):
        rec.parse_emails(bad)


def test_parse_emails_caps_the_typed_addresses():
    rec = load_module("app/email/recipients.py")
    with pytest.raises(ValueError):
        rec.parse_emails([f"u{i}@example.com" for i in range(rec.MAX_EMAILS + 1)])


def test_parse_ids_only_accepts_positive_numeric_ids():
    rec = load_module("app/email/recipients.py")
    assert rec.parse_ids([3, "5", 3], "user_ids") == [3, 5]
    for bad in ([0], [-2], ["x"], [True], "3", [1.5]):
        with pytest.raises(ValueError):
            rec.parse_ids(bad, "user_ids")


def test_load_ids_tolerates_garbage():
    rec = load_module("app/email/recipients.py")
    assert rec.load_ids("[1, 2]") == [1, 2] and rec.load_ids(None) == [] and rec.load_ids("{bad") == []
    assert rec.load_ids('["a", 4, true]') == [4]


def test_normalize_list_needs_a_name_and_at_least_one_member():
    rec = load_module("app/email/recipients.py")
    with pytest.raises(ValueError):
        rec.normalize_list({"emails": "a@b.co"})
    with pytest.raises(ValueError):
        rec.normalize_list({"name": "Ops"})
    out = rec.normalize_list({"name": " Ops ", "emails": "a@b.co", "user_ids": [4]})
    assert out["name"] == "Ops" and out["emails"] == "a@b.co" and out["user_ids"] == "[4]" and out["group_ids"] is None


def test_normalize_list_update_keeps_what_the_payload_omits():
    rec = load_module("app/email/recipients.py")
    cur = {"name": "Ops", "description": "d", "user_ids": "[4]", "group_ids": "[9]", "emails": "a@b.co"}
    out = rec.normalize_list({"emails": ""}, current=cur)           # typed addresses cleared, users and groups stay
    assert out["emails"] is None and out["user_ids"] == "[4]" and out["group_ids"] == "[9]" and out["name"] == "Ops"
    with pytest.raises(ValueError):                                  # clearing the last member is refused
        rec.normalize_list({"user_ids": [], "group_ids": [], "emails": ""}, current=cur)


class _ResolveCur:
    def __init__(self, users):
        self.users, self.q = users, []

    def execute(self, sql, params=None):
        self.q.append(" ".join(sql.split()))

    def fetchall(self):
        return self.users if self.q else []


def test_resolve_skips_inactive_no_email_and_other_tenants_but_keeps_typed_addresses():
    rec = load_module("app/email/recipients.py")
    users = [{"id": 1, "username": "a", "role": "editor", "email": "a@x.com", "active": 1},
             {"id": 2, "username": "b", "role": "editor", "email": "b@x.com", "active": 0},
             {"id": 3, "username": "c", "role": "editor", "email": "", "active": 1},
             {"id": 4, "username": "d", "role": "viewer", "email": "d@x.com", "active": 1},
             {"id": 1, "username": "a", "role": "editor", "email": "a@x.com", "active": 1}]       # also in a group: once
    scope = {1: None, 4: {99}}                                  # user 1 sees everything, user 4 only account 99
    row = {"user_ids": "[1,2,3,4]", "group_ids": "[7]", "emails": "ext@partner.com, A@x.com"}
    out = rec.resolve(row, extra_emails="more@y.com", account_id=10, cursor=_ResolveCur(users),
                      accessible_fn=lambda u: scope[u["id"]])
    assert out["addresses"] == ["a@x.com", "ext@partner.com", "more@y.com"]      # A@x.com is a duplicate of a@x.com
    reasons = {s["who"]: s["reason"] for s in out["skipped"]}
    assert reasons == {"b": "user is inactive", "c": "no email on file", "d": "may not see this account"}


def test_resolve_without_an_account_does_not_filter_by_scope_and_a_failing_lookup_excludes_the_user():
    rec = load_module("app/email/recipients.py")
    users = [{"id": 4, "username": "d", "role": "viewer", "email": "d@x.com", "active": 1}]
    assert rec.resolve({"user_ids": "[4]"}, cursor=_ResolveCur(users), accessible_fn=lambda u: {1})["addresses"] == ["d@x.com"]

    def boom(u):
        raise RuntimeError("scope lookup failed")
    out = rec.resolve({"user_ids": "[4]"}, account_id=1, cursor=_ResolveCur(users), accessible_fn=boom)
    assert out["addresses"] == [] and out["skipped"][0]["reason"] == "may not see this account"


def test_resolve_with_only_typed_addresses_never_touches_the_database():
    rec = load_module("app/email/recipients.py")
    assert rec.resolve({"emails": "a@b.co"}, accessible_fn=lambda u: None)["addresses"] == ["a@b.co"]


# ── schedules ────────────────────────────────────────────────────────

def _sched(**over):
    base = {"name": "Monthly prod", "report_type": "MONTHLY", "scope_type": "ACCOUNT", "account_id": 7, "run_day": 1,
            "recipient_list_id": 3}
    base.update(over)
    return base


def test_schedule_weekly_needs_a_weekday_and_clears_the_other_day_fields():
    _, sch, _ = _pure_modules()
    with pytest.raises(ValueError):
        sch.normalize(_sched(report_type="WEEKLY"))
    out = sch.normalize(_sched(report_type="WEEKLY", run_weekday=0, run_day=5, run_month=3))
    assert (out["run_weekday"], out["run_day"], out["run_month"]) == (0, None, None)


def test_schedule_yearly_needs_month_and_day_and_day_is_capped_at_28():
    _, sch, _ = _pure_modules()
    with pytest.raises(ValueError):
        sch.normalize(_sched(report_type="YEARLY", run_day=1))                    # month missing
    with pytest.raises(ValueError):
        sch.normalize(_sched(report_type="YEARLY", run_day=29, run_month=1))      # not every month has a 29th
    out = sch.normalize(_sched(report_type="YEARLY", run_day=1, run_month=4))
    assert (out["run_month"], out["run_day"], out["run_weekday"]) == (4, 1, None)


def test_schedule_rejects_custom_incident_and_unknown_values():
    _, sch, _ = _pure_modules()
    for over in ({"report_type": "CUSTOM"}, {"scope_type": "INCIDENT"}, {"run_hour": 24}, {"run_minute": 60},
                 {"timezone": "Mars/Base"}, {"name": ""}, {"run_day": 0}):
        with pytest.raises(ValueError):
            sch.normalize(_sched(**over))


def test_schedule_scope_rules_account_forced_and_org_wide_scopes_have_no_account():
    _, sch, _ = _pure_modules()
    assert sch.normalize(_sched(scope_id="999"))["scope_id"] == "7"               # ACCOUNT: the scope is the account
    cost = sch.normalize(_sched(scope_type="COST", scope_id="all", account_id=5))
    assert cost["account_id"] is None and cost["scope_id"] == "all"
    with pytest.raises(ValueError):
        sch.normalize(_sched(scope_type="HOST", scope_id="7", account_id=None))   # needs an account
    with pytest.raises(ValueError):
        sch.normalize(_sched(scope_type="COST", scope_id=""))


def test_schedule_needs_somebody_to_send_to_and_defaults_are_ist_0800():
    _, sch, _ = _pure_modules()
    with pytest.raises(ValueError):
        sch.normalize(_sched(recipient_list_id=None))
    out = sch.normalize(_sched(recipient_list_id=None, extra_emails="a@b.co, A@B.co"))
    assert out["extra_emails"] == "a@b.co" and out["timezone"] == "Asia/Kolkata"
    assert (out["run_hour"], out["run_minute"], out["enabled"]) == (8, 0, 1)


def test_schedule_update_merges_with_the_stored_row_and_can_disable():
    _, sch, _ = _pure_modules()
    stored = {"name": "S", "report_type": "MONTHLY", "scope_type": "ACCOUNT", "scope_id": "7", "account_id": 7,
              "run_day": 2, "run_hour": 9, "run_minute": 30, "timezone": "UTC", "recipient_list_id": 3, "enabled": 1}
    out = sch.normalize({"enabled": False, "run_day": 3}, current=stored)
    assert (out["enabled"], out["run_day"], out["run_hour"], out["run_minute"], out["timezone"]) == (0, 3, 9, 30, "UTC")


def test_schedule_describe_reads_naturally():
    _, sch, _ = _pure_modules()
    base = {"run_hour": 8, "run_minute": 5, "timezone": "Asia/Kolkata"}
    assert sch.describe({**base, "report_type": "WEEKLY", "run_weekday": 0}) == "Weekly on Monday at 08:05 (Asia/Kolkata)"
    assert sch.describe({**base, "report_type": "MONTHLY", "run_day": 1}) == "Monthly on day 1 at 08:05 (Asia/Kolkata)"
    assert "first month of each quarter" in sch.describe({**base, "report_type": "QUARTERLY", "run_day": 1})
    assert sch.describe({**base, "report_type": "YEARLY", "run_day": 1, "run_month": 4}).startswith("Yearly on day 1 of month 4")


# ── delivery log ─────────────────────────────────────────────────────

class _Conn:
    def __init__(self, store):
        self.store = store

    def cursor(self, *a, **k):
        return self

    def execute(self, sql, params=None):
        self.store.append(params)

    def commit(self):
        self.store.append("commit")

    def close(self):
        self.store.append("close")


def test_delivery_log_is_a_no_op_unless_enabled(monkeypatch):
    rows = []
    install_stub("app.db", get_connection=lambda: _Conn(rows))
    dl = load_module("app/email/delivery_log.py")
    monkeypatch.setenv("EMAIL_CENTER_ENABLED", "false")
    assert dl.record("alert", "a@b.co", "s", "sent") is False and rows == []


def test_delivery_log_writes_one_cleaned_row_and_closes_the_connection():
    rows = []
    install_stub("app.db", get_connection=lambda: _Conn(rows))
    dl = load_module("app/email/delivery_log.py")
    assert dl.record("alert", "a@b.co", "Line one\r\nBcc: x " + "y" * 400, "failed", "login rejected\n(535)", report_id=9)
    params = rows[0]
    assert params[0] == "alert" and params[1] == "a@b.co" and "\r" not in params[2] and "\n" not in params[2]
    assert len(params[2]) == 255 and params[3] == "failed" and params[4] == "login rejected (535)" and params[5] == 9
    assert rows[1:] == ["commit", "close"]


def test_delivery_log_never_raises_and_rejects_unknown_status():
    def boom():
        raise RuntimeError("db down")
    install_stub("app.db", get_connection=boom)
    dl = load_module("app/email/delivery_log.py")
    assert dl.record("alert", "a@b.co", "s", "sent") is False
    assert dl.record("alert", "a@b.co", "s", "exploded") is False


def test_retention_prunes_the_email_log_and_the_flags_are_documented():
    src = (ROOT / "app/collector/retention.py").read_text()
    assert '("email_delivery_log", prune_email_delivery_log)' in src
    assert "EMAIL_LOG_RETENTION_DAYS" in src
    assert "EMAIL_LOG_RETENTION_DAYS" in (ROOT / ".env.example").read_text()
    assert "EMAIL_CENTER_ENABLED=false" in (ROOT / ".env.example").read_text()
    assert "EMAIL_CENTER_ENABLED=false" in (ROOT / ".env.production.example").read_text()


# ── mailer hook ──────────────────────────────────────────────────────

def _mailer(monkeypatch, smtp=True, deliver=(True, "")):
    calls = []
    install_stub("app.email.delivery_log", record=lambda *a, **k: calls.append((a, k)) or True)
    mod = load_module("app/email/mailer.py")
    if smtp:
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    else:
        monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setattr(mod, "_deliver", lambda c, to, msg: deliver)
    return mod, calls


def test_mailer_logs_a_sent_mail_under_the_category_of_its_label(monkeypatch):
    mod, calls = _mailer(monkeypatch)
    assert mod.send_email("a@b.co", "Subj", "body", None, "Alerts") is True
    assert calls[0][0][:4] == ("alert", "a@b.co", "Subj", "sent")
    mod.send_email("a@b.co", "Subj", "body")
    assert calls[1][0][0] == "system"
    mod.send_email("a@b.co", "Subj", "body", None, "", "test")
    assert calls[2][0][0] == "test"


def test_mailer_logs_failures_with_the_reason_and_skips_when_smtp_is_unset(monkeypatch):
    mod, calls = _mailer(monkeypatch, deliver=(False, "login rejected by the mail server (535)"))
    assert mod.send_email("a@b.co", "S", "b") is False
    assert calls[0][0][3] == "failed" and "535" in calls[0][0][4]
    mod2, calls2 = _mailer(monkeypatch, smtp=False)
    assert mod2.send_email("a@b.co", "S", "b") is False
    assert calls2[0][0][3] == "skipped"


def test_mailer_logs_a_header_injection_refusal_and_still_refuses(monkeypatch):
    mod, calls = _mailer(monkeypatch)
    ok, err = mod.send_email_ex("a@b.co\r\nBcc: x@y.com", "S", "b")
    assert ok is False and "line break" in err and calls[0][0][3] == "failed"


def test_a_broken_delivery_log_never_breaks_a_send(monkeypatch):
    install_stub("app.email.delivery_log", record=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("log down")))
    mod = load_module("app/email/mailer.py")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(mod, "_deliver", lambda c, to, msg: (True, ""))
    assert mod.send_email("a@b.co", "S", "b") is True


def test_report_email_ex_reports_the_reason_and_tags_the_schedule(monkeypatch):
    mod, calls = _mailer(monkeypatch, deliver=(False, "connection refused -- wrong host/port"))
    install_stub("app.email.templates", report_attachment_name=lambda r: "r.pdf",
                 report_email=lambda *a, **k: {"subject": "CloudOps Weekly Report", "text": "t", "html": "<p>t</p>"})
    ok, err = mod.send_report_email_ex("a@b.co", {"id": 12}, b"%PDF", schedule_id=4)
    assert ok is False and "refused" in err
    args, kw = calls[0]
    assert args[0] == "report" and args[3] == "failed" and kw == {"report_id": 12, "schedule_id": 4}
    assert mod.send_report_email("a@b.co", {"id": 12}, b"%PDF") is False       # the old bool contract still holds


# ── YEARLY report type ───────────────────────────────────────────────

def test_yearly_is_a_known_type_everywhere_report_types_are_listed():
    for rel, needle in (("app/api/reports.py", '"YEARLY"'), ("app/report_names.py", '"YEARLY": "Yearly"'),
                        ("app/email/templates.py", '"YEARLY": "Yearly"'), ("app/reports/engine.py", '"YEARLY": "Annual Operations Review"'),
                        ("frontend/src/utils/reportPlan.js", 'key: "YEARLY"'), ("frontend/src/pages/Reports.jsx", '"YEARLY"')):
        assert needle in (ROOT / rel).read_text(), rel


def test_a_manual_yearly_report_covers_the_last_365_days():
    install_stub("app.db", get_db_cursor=lambda **k: None)
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    install_stub("app.email.mailer", is_configured=lambda: False)
    install_stub("app.reports.s3_client")
    install_stub("app.reports.worker", run_job=lambda job_id: None)
    install_stub("app.report_names", periodic_report_filename=lambda r: "x.pdf")
    mod = load_module("app/api/reports.py")
    start, end = mod._resolve_period("YEARLY", None, None)
    assert (end - start).days == 365 and "YEARLY" in mod._VALID_REPORT_TYPES


# ── API ──────────────────────────────────────────────────────────────

def test_every_route_is_off_with_a_503_until_the_flag_is_on(monkeypatch):
    mod, *_ = _api()
    monkeypatch.setenv("EMAIL_CENTER_ENABLED", "false")
    with pytest.raises(Exception) as e:
        mod._require_enabled()
    assert e.value.status_code == 503 and "EMAIL_CENTER_ENABLED" in e.value.detail
    monkeypatch.setenv("EMAIL_CENTER_ENABLED", "true")
    assert mod._require_enabled() is None


def test_status_never_returns_credentials_or_infrastructure_details_and_reports_counts():
    script = [(contains("GROUP BY status"), [{"status": "sent", "n": 5}, {"status": "failed", "n": 1}]),
              (contains("status = 'failed'"), [{"created_at": None, "category": "report", "to_addr": "a@b.co", "error": "boom"}])]
    mod, *_ = _api(script)
    out = mod.status(current_user=ADMIN)
    flat = repr(out)
    for secret in ("secret-pw", "smtp.example.com", "ops@example.com", "cloudops.example.com", "'username'", "'password'", "'host'", "'port'"):
        assert secret not in flat, secret
    assert set(out["smtp"]) == {"configured", "problems", "public_url_warning", "recipient_domains_restricted"}
    assert out["smtp"]["configured"] is True and out["smtp"]["recipient_domains_restricted"] is False
    assert out["delivery"]["24h"]["sent"] == 5 and out["delivery"]["7d"]["failed"] == 1
    assert out["last_failure"]["error"] == "boom" and out["migration_pending"] is False and out["can_test"] is True


def test_status_failure_details_need_the_log_permission():
    script = [(contains("GROUP BY status"), []),
              (contains("status = 'failed'"), [{"created_at": None, "category": "report", "to_addr": "a@b.co", "error": "550 mailbox x"}])]
    mod, *_ = _api(script, perms={"emailcenter.view"})
    out = mod.status(current_user=EDITOR)
    assert out["last_failure"] == {"at": None, "category": "report"}          # no address, no mail-server answer
    assert "a@b.co" not in repr(out) and "550" not in repr(out) and out["can_test"] is False
    mod2, *_ = _api(script, perms={"emailcenter.view", "emailcenter.log"})
    assert mod2.status(current_user=EDITOR)["last_failure"]["to"] == "a@b.co"


def test_status_says_whether_recipient_domains_are_restricted_without_naming_them(monkeypatch):
    script = [(contains("GROUP BY status"), []), (contains("status = 'failed'"), [])]
    monkeypatch.setenv("REPORT_EMAIL_ALLOWED_DOMAINS", "aurionpro.com")
    mod, *_ = _api(script)
    out = mod.status(current_user=ADMIN)
    assert out["smtp"]["recipient_domains_restricted"] is True and "aurionpro" not in repr(out)


def test_status_reports_the_plain_http_warning_as_its_own_field():
    script = [(contains("GROUP BY status"), []), (contains("status = 'failed'"), [])]
    mod, *_ = _api(script, public_url_problem="PUBLIC_APP_URL is plain http")
    out = mod.status(current_user=ADMIN)
    assert out["smtp"]["public_url_warning"] == "PUBLIC_APP_URL is plain http"
    mod2, *_ = _api(script)
    assert mod2.status(current_user=ADMIN)["smtp"]["public_url_warning"] is None


_ACCOUNTS = [{"id": 1, "account_name": "U4RAD", "account_id": "111", "provider": "aws"},
             {"id": 2, "account_name": "AuroGov", "account_id": "222", "provider": "aws"},
             {"id": 3, "account_name": None, "account_id": "333", "provider": "azure"}]
_USERS = [{"id": 5, "username": "asha", "role": "editor", "email": "asha@example.com"}]
_GROUPS = [{"id": 9, "name": "NOC", "level": "L1"}]


def _options_script():
    return [(contains("FROM aws_accounts"), _ACCOUNTS), (contains("FROM email_recipient_lists"), [{"id": 3, "name": "Ops"}]),
            (contains("FROM users"), _USERS), (contains("FROM org_groups"), _GROUPS)]


def test_options_gives_a_manager_every_picker():
    mod, *_ = _api(_options_script())
    out = mod.options(current_user=ADMIN)
    assert [a["id"] for a in out["accounts"]] == [1, 2, 3] and out["accounts"][2]["name"] == "333"
    assert out["users"] == [{"id": 5, "username": "asha", "role": "editor", "email": "asha@example.com"}]
    assert out["groups"] == [{"id": 9, "name": "NOC", "level": "L1"}] and out["can_manage"] is True
    assert out["recipient_lists"] == [{"id": 3, "name": "Ops"}] and out["can_manage_recipients"] is True
    assert out["report_types"] == ["WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY"] and out["default_timezone"] == "Asia/Kolkata"
    assert {s["scope_type"] for s in out["scopes"]} == {"ACCOUNT", "RESOURCE", "SYNTHETIC", "HOST", "ENVIRONMENT", "SECURITY", "SLO", "COST", "CLIENT"}
    assert all(s["allowed"] for s in out["scopes"])


def test_options_only_lists_accounts_the_caller_can_see():
    mod, *_ = _api(_options_script(), accessible={2})
    assert [a["id"] for a in mod.options(current_user=ADMIN)["accounts"]] == [2]


def test_options_hides_users_and_groups_from_a_read_only_viewer_and_never_queries_them():
    mod, log, _, _ = _api(_options_script(), perms={"emailcenter.view"})
    out = mod.options(current_user=EDITOR)
    assert out["users"] == [] and out["groups"] == [] and out["can_manage"] is False
    assert out["recipient_lists"] == []                      # a read-only viewer does not even get the list names
    assert not any("FROM users" in sql or "FROM org_groups" in sql or "FROM email_recipient_lists" in sql for sql, _ in log)
    assert "asha@example.com" not in repr(out)


def test_options_gives_a_schedule_manager_list_names_but_never_people():
    mod, log, _, _ = _api(_options_script(), perms={"emailcenter.view", "emailcenter.manage"})
    out = mod.options(current_user=EDITOR)
    assert out["recipient_lists"] == [{"id": 3, "name": "Ops"}] and out["users"] == [] and out["groups"] == []
    assert out["can_manage"] is True and out["can_manage_recipients"] is False
    assert not any("FROM users" in sql or "FROM org_groups" in sql for sql, _ in log)
    assert "asha@example.com" not in repr(out)


def test_options_marks_scopes_the_caller_may_not_schedule():
    mod, *_ = _api(_options_script(), perms={"emailcenter.view", "synthetic.view"})
    allowed = {s["scope_type"]: s["allowed"] for s in mod.options(current_user=EDITOR)["scopes"]}
    assert allowed["ACCOUNT"] and allowed["RESOURCE"] and allowed["SYNTHETIC"]
    assert not allowed["COST"] and not allowed["CLIENT"] and not allowed["HOST"] and not allowed["SLO"]


def test_options_names_the_migration_when_a_table_is_missing():
    mod, *_ = _api([(contains("FROM aws_accounts"), _DbError(1146))])
    with pytest.raises(Exception) as e:
        mod.options(current_user=ADMIN)
    assert e.value.status_code == 503


def test_permissions_me_reports_the_email_center_flag(monkeypatch):
    src = (ROOT / "app/api/permissions.py").read_text()
    assert '"email_center": _email_center_enabled()' in src
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.auth.deps", get_current_user=lambda: None)
    install_stub("app.auth.permissions", get_role_permissions=lambda r: set(), require_permission=lambda c: (lambda: None),
                 denied_permission_codes=lambda u: set())
    mod = load_module("app/api/permissions.py")
    monkeypatch.setenv("EMAIL_CENTER_ENABLED", "false")
    assert mod._email_center_enabled() is False
    monkeypatch.setenv("EMAIL_CENTER_ENABLED", "true")
    assert mod._email_center_enabled() is True


def test_status_before_the_migration_says_so_instead_of_failing():
    script = [(contains("email_delivery_log"), _DbError(1146))]
    mod, *_ = _api(script)
    out = mod.status(current_user=ADMIN)
    assert out["migration_pending"] is True and "135" in out["migration_note"] and out["smtp"]["configured"] is True


def test_test_email_sends_to_one_valid_address_and_audits_it():
    mod, _, mails, audit = _api()
    out = mod.send_test(request=None, payload={"to": "me@example.com"}, current_user=ADMIN)
    assert out["ok"] is True and mails[0][0][0] == "me@example.com" and mails[0][0][4:] == ("Alerts", "test")
    assert audit and audit[0][1] == "Test email sent"


@pytest.mark.parametrize("payload", [{}, {"to": ""}, {"to": "not-an-address"}, {"to": "a@b.co, c@d.co"}, {"to": "a@b.co\r\nBcc: x@y.co"}])
def test_test_email_rejects_missing_bad_and_multiple_recipients(payload):
    mod, _, mails, _ = _api()
    with pytest.raises(Exception) as e:
        mod.send_test(request=None, payload=payload, current_user=ADMIN)
    assert e.value.status_code == 400 and mails == []


def test_test_email_honours_the_allowed_domains_and_says_501_when_smtp_is_unset(monkeypatch):
    mod, _, mails, _ = _api()
    monkeypatch.setenv("REPORT_EMAIL_ALLOWED_DOMAINS", "aurionpro.com")
    with pytest.raises(Exception) as e:
        mod.send_test(request=None, payload={"to": "x@gmail.com"}, current_user=ADMIN)
    assert e.value.status_code == 400 and "@aurionpro.com" in e.value.detail and mails == []
    monkeypatch.delenv("REPORT_EMAIL_ALLOWED_DOMAINS")
    mod2, _, mails2, _ = _api(smtp={"host": ""})
    with pytest.raises(Exception) as e2:
        mod2.send_test(request=None, payload={"to": "x@gmail.com"}, current_user=ADMIN)
    assert e2.value.status_code == 501 and mails2 == []


def test_create_list_validates_checks_the_name_and_audits():
    script = [(contains("SELECT id FROM email_recipient_lists WHERE name"), []),
              (contains("INSERT INTO email_recipient_lists"), []),
              (contains("SELECT * FROM email_recipient_lists WHERE id"),
               [{"id": 77, "name": "Ops", "description": "", "user_ids": "[4]", "group_ids": None, "emails": "a@b.co",
                 "created_by": "vinay", "created_at": None, "updated_at": None}])]
    mod, log, _, audit = _api(script)
    out = mod.create_list(request=None, payload={"name": "Ops", "user_ids": [4], "emails": "a@b.co"}, current_user=ADMIN)
    assert out["id"] == 77 and out["user_ids"] == [4] and out["emails"] == ["a@b.co"]
    assert audit[0][1] == "Recipient list created"
    insert = [p for sql, p in log if sql.startswith("INSERT INTO email_recipient_lists")][0]
    assert insert[-1] == "vinay"
    with pytest.raises(Exception) as e:
        mod.create_list(request=None, payload={"name": "Empty"}, current_user=ADMIN)
    assert e.value.status_code == 400


def test_create_list_rejects_a_duplicate_name_with_409():
    mod, *_ = _api([(contains("SELECT id FROM email_recipient_lists WHERE name"), [{"id": 1}])])
    with pytest.raises(Exception) as e:
        mod.create_list(request=None, payload={"name": "Ops", "emails": "a@b.co"}, current_user=ADMIN)
    assert e.value.status_code == 409


def test_delete_list_is_refused_while_a_schedule_uses_it():
    script = [(contains("SELECT name FROM email_recipient_lists"), [{"name": "Ops"}]),
              (contains("COUNT(*)", "report_schedules"), [{"n": 2}])]
    mod, log, _, _ = _api(script)
    with pytest.raises(Exception) as e:
        mod.delete_list(list_id=1, request=None, current_user=ADMIN)
    assert e.value.status_code == 409 and not any(sql.startswith("DELETE") for sql, _ in log)


def test_a_missing_table_becomes_a_503_that_names_the_migration():
    mod, *_ = _api([(contains("FROM email_recipient_lists"), _DbError(1146))])
    with pytest.raises(Exception) as e:
        mod.list_lists(current_user=ADMIN)
    assert e.value.status_code == 503 and "135" in e.value.detail


def test_preview_refuses_an_account_the_caller_cannot_see():
    mod, *_ = _api(accessible={1, 2})
    with pytest.raises(Exception) as e:
        mod.preview_list(list_id=1, account_id=9, current_user=EDITOR)
    assert e.value.status_code == 403


_SCHEDULE_ROW = {"id": 77, "name": "Monthly prod", "enabled": 1, "report_type": "MONTHLY", "scope_type": "ACCOUNT",
                 "scope_id": "7", "account_id": 7, "run_weekday": None, "run_month": None, "run_day": 1, "run_hour": 8,
                 "run_minute": 0, "timezone": "Asia/Kolkata", "recipient_list_id": 3, "extra_emails": "x@y.co",
                 "created_by": "vinay", "created_at": None, "last_run_at": None, "last_status": None, "last_error": None}


def _schedule_script():
    return [(contains("SELECT id FROM email_recipient_lists WHERE id"), [{"id": 3}]),
            (contains("SELECT id FROM report_schedules WHERE name"), []),
            (contains("INSERT INTO report_schedules"), []),
            (contains("SELECT * FROM report_schedules WHERE id"), [_SCHEDULE_ROW]),
            (contains("SELECT 1 AS ok FROM aws_accounts"), [{"ok": 1}]),
            (contains("SELECT id, name FROM email_recipient_lists"), [{"id": 3, "name": "Ops"}])]


def test_create_schedule_happy_path_describes_it_and_audits_it():
    mod, log, _, audit = _api(_schedule_script())
    out = mod.create_schedule(request=None, payload=_sched(extra_emails="x@y.co"), current_user=ADMIN)
    assert out["when"] == "Monthly on day 1 at 08:00 (Asia/Kolkata)" and out["recipient_list_name"] == "Ops"
    assert out["extra_emails"] == ["x@y.co"] and audit[0][1] == "Report schedule created"


def test_create_schedule_refuses_an_account_outside_the_callers_scope_and_a_missing_list():
    mod, log, _, _ = _api(_schedule_script(), accessible={1})
    with pytest.raises(Exception) as e:
        mod.create_schedule(request=None, payload=_sched(), current_user=EDITOR)
    assert e.value.status_code == 403 and not any(sql.startswith("INSERT") for sql, _ in log)
    script = [(contains("SELECT id FROM email_recipient_lists WHERE id"), []), (contains("SELECT 1 AS ok FROM aws_accounts"), [{"ok": 1}])]
    mod2, *_ = _api(script)
    with pytest.raises(Exception) as e2:
        mod2.create_schedule(request=None, payload=_sched(), current_user=ADMIN)
    assert e2.value.status_code == 400 and "does not exist" in e2.value.detail


def test_create_schedule_a_section_report_needs_the_sections_own_permission():
    import fastapi

    def deny(stype, scope_id, acct, user):
        raise fastapi.HTTPException(status_code=403, detail="Missing permission: hostagents.view")
    reg = types_ns(is_section=lambda s: True, validate=deny)
    mod, log, _, _ = _api(_schedule_script(), registry=reg)
    with pytest.raises(Exception) as e:
        mod.create_schedule(request=None, payload=_sched(scope_type="HOST", scope_id="7"), current_user=EDITOR)
    assert e.value.status_code == 403 and not any(sql.startswith("INSERT") for sql, _ in log)


def test_client_scope_schedules_are_admin_only():
    mod, *_ = _api(_schedule_script())
    with pytest.raises(Exception) as e:
        mod.create_schedule(request=None, payload=_sched(scope_type="CLIENT", scope_id="Acme", account_id=None), current_user=EDITOR)
    assert e.value.status_code == 403


def test_viewers_of_the_centre_see_schedules_but_not_the_recipients():
    script = [(contains("SELECT * FROM report_schedules"), [_SCHEDULE_ROW, {**_SCHEDULE_ROW, "id": 78, "account_id": None, "scope_type": "COST"}]),
              (contains("SELECT id, name FROM email_recipient_lists"), [{"id": 3, "name": "Ops"}])]
    mod, *_ = _api(script, perms={"emailcenter.view"}, accessible={7})
    out = mod.list_schedules(current_user=EDITOR)
    assert [s["id"] for s in out] == [77]                    # the org-wide COST schedule is not shown to a scoped user
    assert "extra_emails" not in out[0] and out[0]["recipient_list_name"] == "Ops"
    mod2, *_ = _api(script, accessible=None)
    assert [s["id"] for s in mod2.list_schedules(current_user=ADMIN)] == [77, 78]
    assert mod2.list_schedules(current_user=ADMIN)[0]["extra_emails"] == ["x@y.co"]


def test_log_filters_are_validated_and_parameterised():
    script = [(contains("FROM email_delivery_log"), [{"id": 5, "created_at": None, "category": "alert", "to_addr": "a@b.co",
                                                       "subject": "s", "status": "sent", "error": None,
                                                       "report_id": None, "schedule_id": None}])]
    mod, log, _, _ = _api(script)
    out = mod.delivery_log_rows(status="sent", category="alert", before_id=10, limit=50, current_user=ADMIN)
    sql, params = log[-1]
    assert out[0]["id"] == 5 and "status = %s" in sql and "category = %s" in sql and "id < %s" in sql
    assert params == ("sent", "alert", 10, 50) and "sent" not in sql
    for bad in ({"status": "exploded"}, {"category": "x'; DROP TABLE y;--"}):
        with pytest.raises(Exception) as e:
            mod.delivery_log_rows(status=bad.get("status"), category=bad.get("category"), before_id=None, limit=10, current_user=ADMIN)
        assert e.value.status_code == 400


# ── RBAC: who may do what (migration 137) ────────────────────────────

_NEW_CODES = {"emailcenter.recipients", "emailcenter.log", "emailcenter.test"}


def test_every_route_needs_exactly_the_permission_that_matches_its_risk():
    src = (ROOT / "app/api/email_center.py").read_text()
    table = {}
    for chunk in src.split("@router.")[1:]:                       # one chunk per route: decorator, signature, body
        m = re.match(r'(get|post|put|delete)\("([^"]+)"\)', chunk)
        perm = re.search(r'require_permission\("([a-z.]+)"\)', chunk)
        table[(m.group(1), m.group(2))] = perm.group(1) if perm else None
    want = {("get", "/status"): "emailcenter.view", ("get", "/options"): "emailcenter.view",
            ("get", "/schedules"): "emailcenter.view", ("get", "/rules"): "emailcenter.view",
            ("post", "/test"): "emailcenter.test", ("get", "/log"): "emailcenter.log",
            ("get", "/lists"): "emailcenter.recipients", ("post", "/lists"): "emailcenter.recipients",
            ("put", "/lists/{list_id}"): "emailcenter.recipients", ("delete", "/lists/{list_id}"): "emailcenter.recipients",
            ("get", "/lists/{list_id}/preview"): "emailcenter.recipients",
            ("post", "/schedules"): "emailcenter.manage", ("put", "/schedules/{schedule_id}"): "emailcenter.manage",
            ("delete", "/schedules/{schedule_id}"): "emailcenter.manage", ("post", "/schedules/{schedule_id}/run"): "emailcenter.manage",
            ("post", "/rules"): "emailcenter.manage", ("put", "/rules/{rule_id}"): "emailcenter.manage",
            ("delete", "/rules/{rule_id}"): "emailcenter.manage"}
    assert table == want


def test_migration_137_adds_three_permissions_and_backfills_from_manage_without_removing_anything():
    sql = re.sub(r"--[^\n]*", "", (ROOT / "db/migrations/137_email_center_permissions.sql").read_text())     # statements only
    assert set(re.findall(r"\('(emailcenter\.[a-z]+)',\s+'Operations'", sql)) == _NEW_CODES
    assert "DELETE FROM" not in sql.upper() and "DROP " not in sql.upper() and "REVOKE" not in sql.upper()
    # every INSERT is idempotent, and both resolvers are backfilled from whoever holds manage
    assert all(i.startswith("INSERT IGNORE") for i in re.findall(r"^INSERT[^\n]*", sql, flags=re.M))
    assert sql.count("mp.code = 'emailcenter.manage'") == 2
    assert "FROM role_permissions rp" in sql and "FROM role_permissions_v2 rp" in sql
    # nobody below admin is given any of the new codes by name
    assert "'editor'" not in sql and "'viewer'" not in sql


def test_test_email_is_rate_limited_per_person_and_in_total():
    mod, _, mails, _ = _api()
    for _ in range(mod._TEST_PER_PERSON):
        assert mod.send_test(request=None, payload={"to": "me@example.com"}, current_user=ADMIN)["ok"]
    with pytest.raises(Exception) as e:
        mod.send_test(request=None, payload={"to": "me@example.com"}, current_user=ADMIN)
    assert e.value.status_code == 429 and e.value.headers["Retry-After"] and len(mails) == mod._TEST_PER_PERSON
    other = {"id": 3, "username": "someone-else", "role": "admin"}      # the limit is per person, not global to one account
    assert mod.send_test(request=None, payload={"to": "me@example.com"}, current_user=other)["ok"]


def test_a_refused_test_email_does_not_use_up_the_allowance(monkeypatch):
    mod, *_ = _api()
    monkeypatch.setenv("REPORT_EMAIL_ALLOWED_DOMAINS", "aurionpro.com")
    for _ in range(mod._TEST_PER_PERSON + 2):
        with pytest.raises(Exception) as e:
            mod.send_test(request=None, payload={"to": "x@gmail.com"}, current_user=ADMIN)
        assert e.value.status_code == 400


_SCHED_OTHER_ACCOUNT = {**_SCHEDULE_ROW, "account_id": 9}


def test_a_scoped_manager_cannot_edit_or_delete_a_schedule_for_an_account_they_cannot_see():
    script = [(contains("SELECT * FROM report_schedules WHERE id"), [_SCHED_OTHER_ACCOUNT]),
              (contains("SELECT name, account_id FROM report_schedules"), [{"name": "N", "account_id": 9}])]
    mod, log, _, audit = _api(script, perms={"emailcenter.manage"}, accessible={7})
    with pytest.raises(Exception) as e:
        mod.update_schedule(9, request=None, payload={"name": "renamed"}, current_user=EDITOR)
    assert e.value.status_code == 403
    with pytest.raises(Exception) as e2:
        mod.delete_schedule(9, request=None, current_user=EDITOR)
    assert e2.value.status_code == 403
    assert not any(sql.startswith(("UPDATE", "DELETE")) for sql, _ in log) and audit == []
