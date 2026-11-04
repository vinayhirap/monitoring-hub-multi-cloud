# tests/test_email_alert_rules.py
"""
Email & Reports centre, patch 0004: migration 136, the pure rule module (validation, matching, throttle) and the
/api/email-center/rules routes. Sending mail for a rule arrives in patch 0005 and is not tested here.
"""
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, contains  # noqa: E402
from tests.test_email_center import (  # noqa: E402,F401  (fixtures are used by name)
    ADMIN, EDITOR, _DbError, _api, _enabled, _pure_modules, _restore_stubs,
)

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "db/migrations/136_email_alert_rules.sql"
NOW = datetime(2026, 11, 4, 12, 0, 0)


def _rules():
    return _pure_modules()[0] and load_module("app/email/alert_rules.py")


def _payload(**over):
    base = {"name": "Prod criticals", "events": ["alert_opened", "alert_escalated"], "min_severity": "critical",
            "account_id": 7, "recipient_list_id": 3}
    base.update(over)
    return base


# ── migration 136 ────────────────────────────────────────────────────

def test_migration_creates_two_tables_idempotently_and_is_mariadb_safe():
    sql = MIGRATION.read_text()
    assert re.findall(r"^CREATE TABLE IF NOT EXISTS (\w+)", sql, re.M) == ["email_alert_rules", "email_rule_sends"]
    assert not re.search(r"^CREATE TABLE (?!IF NOT EXISTS)", sql, re.M)
    assert "0900" not in sql and "COLLATE" not in sql.upper()


def test_migration_adds_no_permission_and_touches_no_existing_table():
    code = "\n".join(l for l in MIGRATION.read_text().splitlines() if not l.strip().startswith("--"))
    assert "permissions" not in code.lower() and "ALTER TABLE" not in code.upper() and "DROP" not in code.upper()


def test_migration_sends_table_keeps_the_once_per_person_key_short():
    sql = MIGRATION.read_text()
    assert "recipient_key  CHAR(40)" in sql
    assert re.search(r"UNIQUE KEY uq_rule_send_once \(recipient_key, kind, object_id, event\)", sql)


def test_migration_number_is_136_and_unique():
    numbers = [p.name.split("_", 1)[0] for p in (ROOT / "db/migrations").glob("*.sql")]
    assert numbers.count("136") == 1


# ── knobs ────────────────────────────────────────────────────────────

def test_knob_defaults(monkeypatch):
    ar = _rules()
    for k in ("EMAIL_RULE_THROTTLE_SECONDS", "EMAIL_RULE_MAX_PER_RECIPIENT_HOUR", "EMAIL_RULE_SWEEP_MINUTES"):
        monkeypatch.delenv(k, raising=False)
    assert (ar.throttle_seconds(), ar.max_per_hour(), ar.sweep_minutes()) == (1800, 20, 30)


@pytest.mark.parametrize("env,fn,value,expected", [
    ("EMAIL_RULE_THROTTLE_SECONDS", "throttle_seconds", "600", 600),
    ("EMAIL_RULE_THROTTLE_SECONDS", "throttle_seconds", "5", 1800),          # below the floor: default
    ("EMAIL_RULE_THROTTLE_SECONDS", "throttle_seconds", "999999", 1800),     # above the ceiling: default
    ("EMAIL_RULE_THROTTLE_SECONDS", "throttle_seconds", "soon", 1800),
    ("EMAIL_RULE_MAX_PER_RECIPIENT_HOUR", "max_per_hour", "0", 20),
    ("EMAIL_RULE_MAX_PER_RECIPIENT_HOUR", "max_per_hour", "5", 5),
    ("EMAIL_RULE_SWEEP_MINUTES", "sweep_minutes", "1441", 30),
    ("EMAIL_RULE_SWEEP_MINUTES", "sweep_minutes", "60", 60),
])
def test_knobs_are_clamped_to_a_safe_range(monkeypatch, env, fn, value, expected):
    ar = _rules()
    monkeypatch.setenv(env, value)
    assert getattr(ar, fn)() == expected


def test_env_examples_list_the_three_knobs():
    for name in (".env.example", ".env.production.example"):
        text = (ROOT / name).read_text()
        for knob in ("EMAIL_RULE_THROTTLE_SECONDS", "EMAIL_RULE_MAX_PER_RECIPIENT_HOUR", "EMAIL_RULE_SWEEP_MINUTES"):
            assert re.search(rf"^{knob}=\d+$", text, re.M), (name, knob)


# ── keys ─────────────────────────────────────────────────────────────

def test_recipient_key_ignores_case_and_spaces_and_is_40_hex():
    ar = _rules()
    assert ar.recipient_key(" Ops@Example.com ") == ar.recipient_key("ops@example.com")
    assert re.fullmatch(r"[0-9a-f]{40}", ar.recipient_key("a@b.co"))
    assert ar.recipient_key("a@b.co") != ar.recipient_key("b@b.co")


def test_resource_key_separates_accounts_that_share_a_resource_id():
    ar = _rules()
    assert ar.resource_key(1, "i-0abc") != ar.resource_key(2, "i-0abc")
    assert ar.resource_key(1, "i-0abc") == ar.resource_key(1, "i-0abc")


# ── validation ───────────────────────────────────────────────────────

def test_normalize_valid_rule():
    ar = _rules()
    out = ar.normalize(_payload())
    assert out == {"name": "Prod criticals", "enabled": 1, "events": "alert_opened,alert_escalated",
                   "min_severity": "CRITICAL", "account_id": 7, "recipient_list_id": 3, "extra_emails": None}


def test_events_come_back_in_a_fixed_order_without_duplicates():
    ar = _rules()
    assert ar.parse_events("incident_resolved, alert_opened,alert_opened") == ["alert_opened", "incident_resolved"]


@pytest.mark.parametrize("bad", ["", [], None, "alert_exploded", ["alert_opened", "nope"]])
def test_events_reject_unknown_and_empty(bad):
    ar = _rules()
    with pytest.raises(ValueError):
        ar.parse_events(bad)


@pytest.mark.parametrize("over", [
    {"name": ""}, {"name": "x" * 121}, {"name": 5}, {"min_severity": "INFO"}, {"account_id": "abc"},
    {"account_id": 0}, {"account_id": True}, {"recipient_list_id": -1}, {"recipient_list_id": "x"},
    {"enabled": "yes"}, {"enabled": 2}, {"extra_emails": "not-an-address"},
    {"extra_emails": "a@b.com\r\nBcc: x@y.com"},
])
def test_normalize_rejects_bad_fields(over):
    ar = _rules()
    with pytest.raises(ValueError):
        ar.normalize(_payload(**over))


def test_a_rule_needs_somebody_to_mail():
    ar = _rules()
    with pytest.raises(ValueError):
        ar.normalize(_payload(recipient_list_id=None))
    out = ar.normalize(_payload(recipient_list_id=None, extra_emails="ops@example.com, OPS@example.com"))
    assert out["recipient_list_id"] is None and out["extra_emails"] == "ops@example.com"


def test_blank_account_means_every_account_and_severity_defaults_to_warning():
    ar = _rules()
    out = ar.normalize({"name": "All", "events": "alert_opened", "account_id": "", "recipient_list_id": 1})
    assert out["account_id"] is None and out["min_severity"] == "WARNING" and out["enabled"] == 1


def test_update_keeps_what_the_payload_omits_and_can_pause():
    ar = _rules()
    stored = {"name": "Prod criticals", "enabled": 1, "events": "alert_opened", "min_severity": "CRITICAL",
              "account_id": 7, "recipient_list_id": 3, "extra_emails": "x@y.co"}
    out = ar.normalize({"enabled": False}, current=stored)
    assert out["enabled"] == 0 and out["events"] == "alert_opened" and out["account_id"] == 7
    assert out["extra_emails"] == "x@y.co"
    cleared = ar.normalize({"extra_emails": ""}, current=stored)          # list is still there, so this is allowed
    assert cleared["extra_emails"] is None
    with pytest.raises(ValueError):                                       # but not leaving nobody to mail
        ar.normalize({"extra_emails": "", "recipient_list_id": None}, current=stored)


# ── matching ─────────────────────────────────────────────────────────

_RULE = {"enabled": 1, "events": "alert_opened,incident_opened", "min_severity": "CRITICAL", "account_id": 7}


def test_rule_matches_event_severity_and_account():
    ar = _rules()
    assert ar.matches(_RULE, "alert_opened", "CRITICAL", 7)
    assert ar.matches(_RULE, "incident_opened", "critical", "7")
    assert not ar.matches(_RULE, "alert_resolved", "CRITICAL", 7)          # event not chosen
    assert not ar.matches(_RULE, "alert_opened", "WARNING", 7)             # below the floor
    assert not ar.matches(_RULE, "alert_opened", "CRITICAL", 8)            # other account
    assert not ar.matches(_RULE, "alert_opened", "CRITICAL", None)         # unknown account never matches a scoped rule


def test_all_account_rule_matches_any_account_and_a_warning_floor_takes_both():
    ar = _rules()
    rule = dict(_RULE, account_id=None, min_severity="WARNING")
    assert ar.matches(rule, "alert_opened", "WARNING", 1) and ar.matches(rule, "alert_opened", "CRITICAL", 99)
    assert ar.matches(rule, "alert_opened", "CRITICAL", None)


def test_a_paused_rule_and_an_unknown_severity_never_match():
    ar = _rules()
    assert not ar.matches(dict(_RULE, enabled=0), "alert_opened", "CRITICAL", 7)
    assert not ar.matches(dict(_RULE, min_severity="WARNING"), "alert_opened", "INFO", 7)


# ── throttle and cap ─────────────────────────────────────────────────

def _sent(minutes_ago, key="r1"):
    return {"sent_at": NOW - timedelta(minutes=minutes_ago), "resource_key": key}


def test_decide_allows_the_first_mail():
    ar = _rules()
    assert ar.decide(NOW, [], "r1", throttle=1800, cap=20) == (True, "")


def test_decide_blocks_the_same_resource_inside_the_window_only():
    ar = _rules()
    ok, why = ar.decide(NOW, [_sent(10)], "r1", throttle=1800, cap=20)
    assert not ok and "throttled" in why
    assert ar.decide(NOW, [_sent(31)], "r1", throttle=1800, cap=20)[0]       # window passed
    assert ar.decide(NOW, [_sent(10, "other")], "r1", throttle=1800, cap=20)[0]   # a different resource is fine


def test_decide_hard_cap_counts_every_resource_in_the_last_hour():
    ar = _rules()
    recent = [_sent(m, f"res{m}") for m in range(1, 6)]
    ok, why = ar.decide(NOW, recent, "new", throttle=1800, cap=5)
    assert not ok and "cap" in why
    assert ar.decide(NOW, recent, "new", throttle=1800, cap=6)[0]
    old = [_sent(m + 60, f"res{m}") for m in range(1, 6)]                    # older than an hour: not counted
    assert ar.decide(NOW, old, "new", throttle=1800, cap=5)[0]


def test_decide_never_mails_the_same_event_twice():
    ar = _rules()
    ok, why = ar.decide(NOW, [], "r1", throttle=1800, cap=20, already_sent=True)
    assert not ok and "already" in why


def test_decide_without_a_resource_key_skips_the_throttle_but_not_the_cap():
    ar = _rules()
    assert ar.decide(NOW, [_sent(1, None)], None, throttle=1800, cap=20)[0]
    assert not ar.decide(NOW, [_sent(1, None)], None, throttle=1800, cap=1)[0]


def test_decide_reads_the_knobs_when_not_given_values(monkeypatch):
    ar = _rules()
    monkeypatch.setenv("EMAIL_RULE_THROTTLE_SECONDS", "60")
    assert ar.decide(NOW, [_sent(2)], "r1")[0]                                # 2 min > 60 s window
    monkeypatch.setenv("EMAIL_RULE_THROTTLE_SECONDS", "1800")
    assert not ar.decide(NOW, [_sent(2)], "r1")[0]


def test_describe_reads_naturally():
    ar = _rules()
    assert ar.describe(_RULE) == "Alert opened, Incident opened at Critical or higher"


# ── API ──────────────────────────────────────────────────────────────

_ROW = {"id": 77, "name": "Prod criticals", "enabled": 1, "events": "alert_opened,alert_escalated",
        "min_severity": "CRITICAL", "account_id": 7, "recipient_list_id": 3, "extra_emails": "x@y.co",
        "created_by": "vinay", "created_at": None, "updated_at": None}


def _create_script(row=_ROW):
    return [(contains("SELECT id FROM email_recipient_lists WHERE id"), [{"id": 3}]),
            (contains("SELECT 1 AS ok FROM aws_accounts"), [{"ok": 1}]),
            (contains("SELECT id FROM email_alert_rules WHERE name"), []),
            (contains("INSERT INTO email_alert_rules"), []),
            (contains("SELECT * FROM email_alert_rules WHERE id"), [row]),
            (contains("SELECT id, name FROM email_recipient_lists"), [{"id": 3, "name": "Ops"}])]


def test_create_rule_saves_audits_and_returns_it():
    mod, log, _, audit = _api(_create_script())
    out = mod.create_rule(request=None, payload=_payload(extra_emails="x@y.co"), current_user=ADMIN)
    assert out["id"] == 77 and out["events"] == ["alert_opened", "alert_escalated"] and out["recipient_list_name"] == "Ops"
    assert out["extra_emails"] == ["x@y.co"] and out["when"].startswith("Alert opened")
    assert audit[0][1] == "Alert email rule created"
    insert = [p for sql, p in log if sql.startswith("INSERT INTO email_alert_rules")][0]
    assert insert[-1] == "vinay" and insert[3] == "CRITICAL"


def test_create_rule_rejects_bad_input_with_400_before_touching_the_database():
    mod, log, *_ = _api()
    with pytest.raises(Exception) as e:
        mod.create_rule(request=None, payload=_payload(events=["bogus"]), current_user=ADMIN)
    assert e.value.status_code == 400 and log == []


def test_create_rule_duplicate_name_is_409():
    script = [(contains("SELECT id FROM email_recipient_lists WHERE id"), [{"id": 3}]),
              (contains("SELECT 1 AS ok FROM aws_accounts"), [{"ok": 1}]),
              (contains("SELECT id FROM email_alert_rules WHERE name"), [{"id": 1}])]
    mod, *_ = _api(script)
    with pytest.raises(Exception) as e:
        mod.create_rule(request=None, payload=_payload(), current_user=ADMIN)
    assert e.value.status_code == 409


def test_create_rule_unknown_list_or_account_is_refused():
    mod, *_ = _api([(contains("SELECT id FROM email_recipient_lists WHERE id"), [])])
    with pytest.raises(Exception) as e:
        mod.create_rule(request=None, payload=_payload(), current_user=ADMIN)
    assert e.value.status_code == 400
    mod, *_ = _api([(contains("SELECT id FROM email_recipient_lists WHERE id"), [{"id": 3}]),
                    (contains("SELECT 1 AS ok FROM aws_accounts"), [])])
    with pytest.raises(Exception) as e:
        mod.create_rule(request=None, payload=_payload(), current_user=ADMIN)
    assert e.value.status_code == 404


def test_create_rule_for_an_account_the_caller_cannot_see_is_403():
    mod, log, *_ = _api(accessible={1, 2})
    with pytest.raises(Exception) as e:
        mod.create_rule(request=None, payload=_payload(account_id=9), current_user=ADMIN)
    assert e.value.status_code == 403 and log == []


def test_create_rule_honours_the_allowed_domains(monkeypatch):
    monkeypatch.setenv("REPORT_EMAIL_ALLOWED_DOMAINS", "example.com")
    mod, log, *_ = _api(_create_script())
    with pytest.raises(Exception) as e:
        mod.create_rule(request=None, payload=_payload(extra_emails="x@elsewhere.org"), current_user=ADMIN)
    assert e.value.status_code == 400 and "example.com" in e.value.detail


def test_update_rule_changes_only_what_was_sent():
    script = [(contains("SELECT * FROM email_alert_rules WHERE id"), [_ROW]),
              (contains("SELECT id FROM email_recipient_lists WHERE id"), [{"id": 3}]),
              (contains("SELECT 1 AS ok FROM aws_accounts"), [{"ok": 1}]),
              (contains("SELECT id FROM email_alert_rules WHERE name"), []),
              (contains("UPDATE email_alert_rules"), []),
              (contains("SELECT id, name FROM email_recipient_lists"), [{"id": 3, "name": "Ops"}])]
    mod, log, _, audit = _api(script)
    out = mod.update_rule(rule_id=77, request=None, payload={"enabled": False}, current_user=ADMIN)
    assert audit[0][1] == "Alert email rule updated" and "paused" in audit[0][2]
    update = [p for sql, p in log if sql.startswith("UPDATE email_alert_rules")][0]
    assert update[1] == 0 and update[2] == "alert_opened,alert_escalated" and update[-1] == 77
    assert out["id"] == 77


def test_update_rule_unknown_id_is_404_and_a_scoped_caller_cannot_touch_another_accounts_rule():
    mod, *_ = _api([(contains("SELECT * FROM email_alert_rules WHERE id"), [])])
    with pytest.raises(Exception) as e:
        mod.update_rule(rule_id=5, request=None, payload={"enabled": False}, current_user=ADMIN)
    assert e.value.status_code == 404
    mod, log, *_ = _api([(contains("SELECT * FROM email_alert_rules WHERE id"), [dict(_ROW, account_id=9)])],
                        accessible={7})
    with pytest.raises(Exception) as e:
        mod.update_rule(rule_id=77, request=None, payload={"enabled": False}, current_user=ADMIN)
    assert e.value.status_code == 403 and not any(sql.startswith("UPDATE") for sql, _ in log)


def test_delete_rule_removes_its_send_memory_and_audits():
    script = [(contains("SELECT name, account_id FROM email_alert_rules"), [{"name": "Prod criticals", "account_id": 7}]),
              (contains("DELETE FROM email_alert_rules"), []), (contains("DELETE FROM email_rule_sends"), [])]
    mod, log, _, audit = _api(script)
    assert mod.delete_rule(rule_id=77, request=None, current_user=ADMIN) == {"status": "deleted"}
    assert audit[0][1] == "Alert email rule deleted"
    assert [sql.split(" WHERE")[0] for sql, _ in log if sql.startswith("DELETE")] == [
        "DELETE FROM email_alert_rules", "DELETE FROM email_rule_sends"]


def test_delete_rule_unknown_is_404_and_foreign_account_is_403():
    mod, *_ = _api([(contains("SELECT name, account_id FROM email_alert_rules"), [])])
    with pytest.raises(Exception) as e:
        mod.delete_rule(rule_id=1, request=None, current_user=ADMIN)
    assert e.value.status_code == 404
    mod, log, *_ = _api([(contains("SELECT name, account_id FROM email_alert_rules"), [{"name": "n", "account_id": 9}])],
                        accessible={7})
    with pytest.raises(Exception) as e:
        mod.delete_rule(rule_id=1, request=None, current_user=ADMIN)
    assert e.value.status_code == 403 and not any(sql.startswith("DELETE") for sql, _ in log)


def _list_script(rows):
    return [(contains("SELECT * FROM email_alert_rules ORDER BY"), rows),
            (contains("SELECT id, name FROM email_recipient_lists"), [{"id": 3, "name": "Ops"}])]


def test_list_rules_hides_recipient_addresses_unless_the_caller_can_manage():
    mod, *_ = _api(_list_script([_ROW]), perms={"emailcenter.view"})
    out = mod.list_rules(current_user=EDITOR)
    assert out[0]["name"] == "Prod criticals" and "extra_emails" not in out[0]
    mod, *_ = _api(_list_script([_ROW]))
    assert mod.list_rules(current_user=ADMIN)[0]["extra_emails"] == ["x@y.co"]


def test_list_rules_gives_a_scoped_user_only_rules_for_accounts_they_can_see():
    rows = [_ROW, dict(_ROW, id=78, account_id=9), dict(_ROW, id=79, account_id=None)]
    mod, *_ = _api(_list_script(rows), perms={"emailcenter.view"}, accessible={7})
    assert [r["id"] for r in mod.list_rules(current_user=EDITOR)] == [77]
    mod, *_ = _api(_list_script(rows), accessible=None)
    assert [r["id"] for r in mod.list_rules(current_user=ADMIN)] == [77, 78, 79]


def test_rules_before_migration_136_answer_503_and_name_it():
    mod, *_ = _api([(contains("FROM email_alert_rules"), _DbError(1146))])
    with pytest.raises(Exception) as e:
        mod.list_rules(current_user=ADMIN)
    assert e.value.status_code == 503 and "136" in e.value.detail


def test_a_list_used_by_a_rule_cannot_be_deleted():
    script = [(contains("SELECT name FROM email_recipient_lists"), [{"name": "Ops"}]),
              (contains("COUNT(*)", "report_schedules"), [{"n": 0}]),
              (contains("COUNT(*)", "email_alert_rules"), [{"n": 1}])]
    mod, log, *_ = _api(script)
    with pytest.raises(Exception) as e:
        mod.delete_list(list_id=1, request=None, current_user=ADMIN)
    assert e.value.status_code == 409 and "rule" in e.value.detail
    assert not any(sql.startswith("DELETE") for sql, _ in log)


def test_deleting_a_list_still_works_before_migration_136():
    script = [(contains("SELECT name FROM email_recipient_lists"), [{"name": "Ops"}]),
              (contains("COUNT(*)", "report_schedules"), [{"n": 0}]),
              (contains("COUNT(*)", "email_alert_rules"), _DbError(1146)),
              (contains("DELETE FROM email_recipient_lists"), [])]
    mod, log, *_ = _api(script)
    assert mod.delete_list(list_id=1, request=None, current_user=ADMIN) == {"status": "deleted"}


def test_rules_routes_need_the_right_permissions():
    mod, *_ = _api()
    src = (ROOT / "app/api/email_center.py").read_text()
    for route, perm in (('@router.get("/rules")', "emailcenter.view"), ('@router.post("/rules")', "emailcenter.manage"),
                        ('@router.put("/rules/{rule_id}")', "emailcenter.manage"),
                        ('@router.delete("/rules/{rule_id}")', "emailcenter.manage")):
        block = src.split(route, 1)[1].split("@router.", 1)[0]
        assert f'require_permission("{perm}")' in block, route


def test_every_rules_route_is_off_with_a_503_until_the_flag_is_on(monkeypatch):
    monkeypatch.setenv("EMAIL_CENTER_ENABLED", "false")
    mod, *_ = _api()
    with pytest.raises(Exception) as e:
        mod._require_enabled()
    assert e.value.status_code == 503
