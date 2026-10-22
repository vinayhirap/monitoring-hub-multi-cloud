"""Actual AWS cost (Cost Explorer): pure tests with a fake boto session. No database, no AWS, no network."""
from datetime import date

import pytest

import app  # noqa: F401
from app.aws import cost_explorer as ce


class FakeCE:
    def __init__(self, pages=None, error=None):
        self.calls, self._pages, self._error = [], list(pages or [{}]), error

    def get_cost_and_usage(self, **kw):
        self.calls.append(dict(kw))
        if self._error:
            raise self._error
        return self._pages.pop(0)


class FakeSession:
    def __init__(self, client):
        self.client_obj, self.requested = client, []

    def client(self, name, **kw):
        self.requested.append((name, kw.get("region_name")))
        return self.client_obj


def page(groups, start="2026-10-20", estimated=False, token=None):
    p = {"ResultsByTime": [{"TimePeriod": {"Start": start, "End": "2026-10-21"}, "Estimated": estimated,
                            "Groups": [{"Keys": [u, a], "Metrics": {"UnblendedCost": {"Amount": amt, "Unit": "USD"}}}
                                       for u, a, amt in groups]}]}
    if token:
        p["NextPageToken"] = token
    return p


def test_the_query_is_cloudwatch_only_daily_unblended_in_us_east_1():
    fake = FakeCE([page([("APS3-CW:GMD-Metrics", "111", "1.25")])])
    sess = FakeSession(fake)
    rows, reqs = ce.fetch_cloudwatch_cost(sess, today=date(2026, 10, 22))
    kw = fake.calls[0]
    assert sess.requested == [("ce", "us-east-1")] and reqs == 1
    assert kw["Granularity"] == "DAILY" and kw["Metrics"] == ["UnblendedCost"]
    assert kw["Filter"] == {"Dimensions": {"Key": "SERVICE", "Values": ["AmazonCloudWatch"]}}
    assert [g["Key"] for g in kw["GroupBy"]] == ["USAGE_TYPE", "LINKED_ACCOUNT"]
    assert kw["TimePeriod"] == {"Start": "2026-09-21", "End": "2026-10-23"}      # End is exclusive, includes today
    assert rows == [{"usage_type": "APS3-CW:GMD-Metrics", "billed_account_id": "111",
                     "usage_date": date(2026, 10, 20), "amount_usd": 1.25, "estimated": False}]


def test_only_the_read_only_cost_call_is_ever_made():
    fake = FakeCE([page([])])
    ce.fetch_cloudwatch_cost(FakeSession(fake), today=date(2026, 10, 22))
    assert [c for c in dir(fake) if c.startswith(("put_", "create_", "delete_", "update_"))] == []
    assert len(fake.calls) == 1


def test_pagination_is_followed_and_every_page_is_counted_as_a_billed_request():
    fake = FakeCE([page([("u", "1", "1")], token="t2"), page([("u", "1", "2")], start="2026-10-21")])
    rows, reqs = ce.fetch_cloudwatch_cost(FakeSession(fake), today=date(2026, 10, 22))
    assert reqs == 2 and len(rows) == 2 and fake.calls[1]["NextPageToken"] == "t2"


def test_pagination_is_capped():
    fake = FakeCE([page([("u", "1", "1")], token="t")] * 10)
    _, reqs = ce.fetch_cloudwatch_cost(FakeSession(fake), today=date(2026, 10, 22))
    assert reqs == ce.MAX_PAGES


def test_parse_keeps_credits_and_skips_unreadable_amounts():
    rows = ce.parse_cost_response([page([("u", "1", "-0.5"), ("u", "1", "bad"), ("u", "1", "nan")])])
    assert [r["amount_usd"] for r in rows] == [-0.5]
    assert ce.parse_cost_response([{"ResultsByTime": [{"TimePeriod": {"Start": "junk"}, "Groups": []}]}, None, {}]) == []


def test_estimated_flag_is_carried():
    assert ce.parse_cost_response([page([("u", "1", "1")], estimated=True)])[0]["estimated"] is True


class _Err(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


@pytest.mark.parametrize("code,status", [("AccessDeniedException", ce.STATUS_PERMISSION),
                                         ("DataUnavailableException", ce.STATUS_NOT_ENABLED),
                                         ("ThrottlingException", ce.STATUS_ERROR)])
def test_error_classes(code, status):
    assert ce.classify_error(_Err(code))[0] == status


def test_error_without_a_response_is_still_classified():
    assert ce.classify_error(RuntimeError("boom"))[0] == ce.STATUS_ERROR


def test_usage_type_labels_are_best_effort_and_never_drop_unknowns():
    assert "GetMetricData" in ce.classify_usage_type("APS3-CW:GMD-Metrics")
    assert "API requests" in ce.classify_usage_type("APS3-CW:Requests")
    assert ce.classify_usage_type("APS3-CW:SomethingNew") == "Other CloudWatch charge"


@pytest.mark.parametrize("v,on", [("1", True), ("true", True), ("ON", True), ("", False), ("0", False), ("no", False)])
def test_flag(monkeypatch, v, on):
    monkeypatch.setenv("ACTUAL_COST_ENABLED", v)
    assert ce.enabled() is on


def test_disabled_means_no_aws_call_and_no_database_read(monkeypatch):
    monkeypatch.delenv("ACTUAL_COST_ENABLED", raising=False)
    monkeypatch.setattr(ce, "_accounts", lambda: pytest.fail("must not read accounts when off"))
    assert ce.refresh_due() == []
    ce.maybe_refresh_async()          # must not start a thread or raise


def test_refresh_skips_accounts_fetched_recently_and_force_uses_the_short_cooldown(monkeypatch):
    monkeypatch.setenv("ACTUAL_COST_ENABLED", "1")
    accts = [{"id": 1, "account_name": "A"}, {"id": 2, "account_name": "B"}]
    monkeypatch.setattr(ce, "_accounts", lambda: accts)
    last = {1: 1_000_000 - 2 * 3600, 2: None}          # A was read 2h ago, B never
    monkeypatch.setattr(ce, "_last_attempt", lambda pk: last[pk])
    done = []
    monkeypatch.setattr(ce, "refresh_account", lambda a: done.append(a["id"]) or {"account": a["account_name"]})
    ce.refresh_due(now=1_000_000)
    assert done == [2]                                  # A is inside the 24h window
    done.clear()
    ce.refresh_due(force=True, now=1_000_000)
    assert done == [1, 2]                               # 2h > the 1h manual cooldown


def test_refresh_account_never_raises_and_records_a_permission_failure(monkeypatch):
    saved = {}
    monkeypatch.setattr(ce, "_save", lambda acct, status, reqs, rows, err: saved.update(status=status, reqs=reqs, rows=rows))
    import app.aws.sts as sts
    monkeypatch.setattr(sts, "get_boto3_session", lambda a: FakeSession(FakeCE(error=_Err("AccessDeniedException"))))
    out = ce.refresh_account({"id": 1, "account_name": "A"})
    assert out["status"] == ce.STATUS_PERMISSION and saved["status"] == ce.STATUS_PERMISSION and saved["rows"] == []


def test_the_documented_policy_grants_exactly_one_read_action():
    import json, pathlib
    doc = json.loads(pathlib.Path("deploy/iam/monitoring-hub-cost-read.json").read_text())
    stmts = doc["Statement"]
    assert len(stmts) == 1 and stmts[0]["Effect"] == "Allow" and stmts[0]["Action"] == "ce:GetCostAndUsage"


# ── admin-only: RBAC and Help ───────────────────────────────────────────
def test_only_the_admin_role_is_granted_usage_manage():
    import pathlib, re
    sql = pathlib.Path("db/migrations/110_help_and_usage_permissions.sql").read_text()
    granted = re.findall(r"role_key = '(\w+)' AND p\.code IN \(([^)]*)\)|SELECT '(\w+)', id FROM permissions WHERE code IN \(([^)]*)\)", sql)
    roles = {(a or c) for a, b, c, d in granted if "usage.manage" in (b or d)}
    assert roles == {"admin"}                       # editors and viewers never get it


def test_help_section_is_visible_only_with_usage_manage():
    from app.help_content import get_sections
    editor = {s["id"] for s in get_sections({"help.view", "usage.view", "alerts.view"}, "editor")}
    admin = {s["id"] for s in get_sections({"help.view", "usage.view", "usage.manage", "system.admin"}, "admin")}
    assert "cloud-actual-cost" not in editor
    assert "cloud-actual-cost" in admin and "cloud-api-cost" in admin


def test_help_names_the_permission_the_cost_and_the_status_values():
    from app.help_content import all_sections
    sec = next(s for s in all_sections() if s["id"] == "cloud-actual-cost")
    text = str(sec["blocks"])
    assert sec["requires"] == "usage.manage"
    for needle in ("ce:GetCostAndUsage", "ACTUAL_COST_ENABLED", "once a day", "permission missing", "docs/AWS_ACTUAL_COST.md"):
        assert needle in text
