"""Azure and GCP actual cost of CloudOps's metric calls. Fixtures follow the vendors' documented response shapes; nothing
here talks to Azure, Google or a database. (Not verified against a live account: the page reports each real read.)"""
import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("_cloud_cost_under_test", ROOT / "app/cloud_cost.py")
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)


# ── Azure ─────────────────────────────────────────────────────────────────────
def az_payload(rows, cost_col="CostUSD", next_link=None, currency=False):
    cols = [{"name": cost_col, "type": "Number"}, {"name": "UsageQuantity", "type": "Number"},
            {"name": "UsageDate", "type": "Number"}, {"name": "Meter", "type": "String"},
            {"name": "ResourceLocation", "type": "String"}]
    if currency:
        cols.append({"name": "Currency", "type": "String"})
    return {"properties": {"columns": cols, "rows": rows, "nextLink": next_link}}


class Resp:
    def __init__(self, code, body=None, headers=None):
        self.status_code, self._b, self.headers, self.text = code, body or {}, headers or {}, str(body)[:200]

    def json(self):
        return self._b


class FakeHttp:
    def __init__(self, resps):
        self.resps, self.calls = list(resps), []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.resps.pop(0)


ACCT = {"id": 7, "account_name": "Azure Prod", "subscription_id": "sub-1", "tenant_id": "t", "client_id": "c"}


def test_azure_rows_are_parsed_per_day_meter_and_location():
    p = az_payload([[0.12, 12000.0, 20261015, "Metric Queries", "IN South"], [0.0, 900.0, 20261016, "Metric Queries", "IN South"]])
    rows = cc.parse_azure_query(p, "sub-1")
    assert rows[0] == {"billed_account": "sub-1", "region": "IN South", "sku": "Metric Queries", "usage_date": date(2026, 10, 15),
                       "amount_usd": 0.12, "quantity": 12000.0, "unit": None}
    assert len(rows) == 2


def test_azure_refuses_local_currency_instead_of_showing_it_as_dollars():
    p = az_payload([[100.0, 5.0, 20261015, "Metric Queries", "IN South", "INR"]], cost_col="PreTaxCost", currency=True)
    with pytest.raises(cc.CostFetchError, match="INR"):
        cc.parse_azure_query(p, "sub-1")
    ok = az_payload([[1.0, 5.0, 20261015, "Metric Queries", "IN South", "USD"]], cost_col="PreTaxCost", currency=True)
    assert cc.parse_azure_query(ok, "sub-1")[0]["amount_usd"] == 1.0


def test_azure_fetch_posts_an_actualcost_query_and_follows_next_link():
    http = FakeHttp([Resp(200, az_payload([[0.1, 1.0, 20261015, "Metric Queries", "IN South"]], next_link="https://next")),
                     Resp(200, az_payload([[0.2, 2.0, 20261016, "Metric Queries", "IN South"]]))])
    rows, n, check = cc.fetch_azure(ACCT, "secret", today=date(2026, 10, 25), days=14, http=http, token="tok")
    assert n == 2 and check == 0.0 and [r["amount_usd"] for r in rows] == [0.1, 0.2]
    body = http.calls[0]["json"]
    assert body["type"] == "ActualCost" and body["timePeriod"]["from"] == "2026-10-11T00:00:00Z"
    assert body["dataset"]["filter"]["dimensions"]["values"] == ["Azure Monitor"]
    assert http.calls[0]["headers"]["Authorization"] == "Bearer tok" and http.calls[1]["url"] == "https://next"
    assert "subscriptions/sub-1/providers/Microsoft.CostManagement/query" in http.calls[0]["url"]


def test_azure_tries_the_next_cost_column_when_one_is_rejected():
    http = FakeHttp([Resp(400, {"error": "invalid aggregation CostUSD"}),
                     Resp(200, az_payload([[0.3, 3.0, 20261015, "Metric Queries", "IN South"]], cost_col="PreTaxCostUSD"))])
    rows, n, _ = cc.fetch_azure(ACCT, "s", today=date(2026, 10, 25), http=http, token="t")
    assert http.calls[1]["json"]["dataset"]["aggregation"]["totalCost"]["name"] == "PreTaxCostUSD" and rows[0]["amount_usd"] == 0.3


def test_azure_permission_and_throttle_handling():
    with pytest.raises(cc.CostFetchError) as e:
        cc.fetch_azure(ACCT, "s", http=FakeHttp([Resp(403)]), token="t")
    assert e.value.status == cc.STATUS_PERMISSION and "Cost Management Reader" in str(e.value)
    waits = []
    http = FakeHttp([Resp(429, headers={"Retry-After": "3"}), Resp(200, az_payload([]))])
    rows, n, _ = cc.fetch_azure(ACCT, "s", http=http, token="t", sleep=waits.append)
    assert waits == [3.0] and rows == [] and n == 2


def test_azure_without_a_subscription_id_is_not_configured():
    with pytest.raises(cc.CostFetchError) as e:
        cc.fetch_azure({"id": 1}, "s", token="t", http=FakeHttp([]))
    assert e.value.status == cc.STATUS_NOT_CONFIGURED


# ── GCP ───────────────────────────────────────────────────────────────────────
class FakeBQ:
    def __init__(self, rows, bytes_billed):
        self.rows, self.bytes_billed, self.sql, self.params = rows, bytes_billed, None, None

    def run(self, sql, params):
        self.sql, self.params = sql, params
        return self.rows, self.bytes_billed


def test_gcp_needs_an_export_table_and_validates_its_name(monkeypatch):
    monkeypatch.delenv("GCP_BILLING_EXPORT_TABLE", raising=False)
    with pytest.raises(cc.CostFetchError) as e:
        cc.fetch_gcp({"id": 1, "project_id": "p"}, "{}", client=FakeBQ([], 0))
    assert e.value.status == cc.STATUS_NOT_CONFIGURED and "GCP_BILLING_EXPORT_TABLE" in str(e.value)
    monkeypatch.setenv("GCP_BILLING_EXPORT_TABLE", "p.ds.tbl`; DROP TABLE x; --")
    assert cc.gcp_export_table() == ""                                    # never interpolated into SQL
    with pytest.raises(cc.CostFetchError):
        cc.gcp_query("a b.c.d")


def test_gcp_query_nets_credits_filters_the_service_and_project_and_maps_rows(monkeypatch):
    monkeypatch.setenv("GCP_BILLING_EXPORT_TABLE", "bill-proj.billing.gcp_billing_export_v1_ABC")
    bq = FakeBQ([{"usage_date": date(2026, 10, 15), "region": "asia-south1", "sku": "Monitoring API Requests",
                  "amount_usd": 0.0, "quantity": 120000.0, "unit": "count"},
                 {"usage_date": datetime(2026, 10, 16), "region": None, "sku": "Monitoring API Requests",
                  "amount_usd": 0.02, "quantity": None, "unit": None}], 5 * 2 ** 30)
    rows, n, check = cc.fetch_gcp({"id": 1, "project_id": "my-proj"}, "{}", today=date(2026, 10, 25), days=14, client=bq)
    assert "UNNEST(credits)" in bq.sql and "service.description = 'Cloud Monitoring'" in bq.sql and "project.id = @project" in bq.sql
    assert "`bill-proj.billing.gcp_billing_export_v1_ABC`" in bq.sql
    assert bq.params == {"project": "my-proj", "start": "2026-10-11", "end": "2026-10-26"}
    assert rows[0]["billed_account"] == "my-proj" and rows[0]["quantity"] == 120000.0 and rows[0]["usage_date"] == date(2026, 10, 15)
    assert rows[1]["region"] == "global" and rows[1]["usage_date"] == date(2026, 10, 16)
    assert n == 1 and check == round(5 * 2 ** 30 / 2 ** 40 * 6.25, 6)     # bytes billed, at BigQuery's list price


def test_error_classification_never_raises_and_names_the_fix():
    Forbidden = type("Forbidden", (Exception,), {})
    NotFound = type("NotFound", (Exception,), {})
    assert cc.classify_error(Forbidden("no"))[0] == cc.STATUS_PERMISSION
    assert cc.classify_error(NotFound("tbl"))[0] == cc.STATUS_NOT_CONFIGURED
    assert cc.classify_error(RuntimeError("boom"))[0] == cc.STATUS_ERROR
    assert cc.classify_error(cc.CostFetchError(cc.STATUS_PERMISSION, "x")) == (cc.STATUS_PERMISSION, "x")


# ── what counts as CloudOps ───────────────────────────────────────────────────
def test_only_metric_api_calls_are_attributed_to_cloudops(monkeypatch):
    monkeypatch.delenv("AZURE_CLOUDOPS_METER_REGEX", raising=False)
    monkeypatch.delenv("GCP_CLOUDOPS_SKU_REGEX", raising=False)
    assert cc.cloudops_category("azure", "Metric Queries") == cc.CAT_CLOUDOPS
    assert cc.cloudops_category("azure", "Metrics API Calls") == cc.CAT_CLOUDOPS
    for other in ("Data Ingestion", "Alerts Metric Monitored", "Log Analytics Data Retention", "Custom Metrics Ingestion"):
        assert cc.cloudops_category("azure", other) == cc.CAT_OTHER
    assert cc.cloudops_category("gcp", "Monitoring API Requests") == cc.CAT_CLOUDOPS
    for other in ("Metric Volume Ingested", "Log Volume", "Uptime Checks", "Alerting Policies"):
        assert cc.cloudops_category("gcp", other) == cc.CAT_OTHER


def test_the_patterns_can_be_overridden_per_deployment(monkeypatch):
    monkeypatch.setenv("AZURE_CLOUDOPS_METER_REGEX", "(?i)^platform metric reads$")
    assert cc.cloudops_category("azure", "Platform Metric Reads") == cc.CAT_CLOUDOPS
    assert cc.cloudops_category("azure", "Metric Queries") == cc.CAT_OTHER
    monkeypatch.setenv("AZURE_CLOUDOPS_METER_REGEX", "([unclosed")           # bad pattern: fall back, never crash
    assert cc.cloudops_category("azure", "Metric Queries") == cc.CAT_CLOUDOPS


def test_usage_type_roundtrip_and_adapter(monkeypatch):
    monkeypatch.delenv("AZURE_CLOUDOPS_METER_REGEX", raising=False)
    u = cc.encode_usage_type("IN South", "Metric Queries")
    assert cc.split_usage_type(u) == ("IN South", "Metric Queries")
    ad = cc.explorer_adapter("azure")
    assert ad["region_of"](u) == "IN South" and ad["category_of"](u) == cc.CAT_CLOUDOPS and ad["label_of"](u) == "Metric Queries"
    assert ad["region_of"]("|x") == "global"


# ── refresh: never raises, records the outcome ────────────────────────────────
def test_refresh_account_records_a_missing_credential_without_raising(monkeypatch):
    saved = {}
    monkeypatch.setattr(cc, "_save", lambda provider, acct, status, reqs, rows, err, check, window: saved.update(
        provider=provider, status=status, err=err, rows=rows))
    monkeypatch.setattr(cc, "_backfilled", lambda *a: False)
    fake_cred = type(sys)("app.credentials")
    fake_cred.load_credential = lambda pk: None
    monkeypatch.setitem(sys.modules, "app.credentials", fake_cred)
    r = cc.refresh_account("azure", ACCT)
    assert r["status"] == cc.STATUS_NOT_CONFIGURED and saved["status"] == cc.STATUS_NOT_CONFIGURED and saved["rows"] == []


def test_refresh_uses_the_long_window_once_then_the_short_one(monkeypatch):
    seen = []
    monkeypatch.setattr(cc, "_save", lambda *a: None)
    fake_cred = type(sys)("app.credentials")
    fake_cred.load_credential = lambda pk: "secret"
    monkeypatch.setitem(sys.modules, "app.credentials", fake_cred)
    monkeypatch.setattr(cc, "fetch_azure", lambda acct, secret, days=0: seen.append(days) or ([], 1, 0.0))
    monkeypatch.setattr(cc, "_backfilled", lambda *a: False)
    cc.refresh_account("azure", ACCT)
    monkeypatch.setattr(cc, "_backfilled", lambda *a: True)
    cc.refresh_account("azure", ACCT)
    assert seen == [cc.BACKFILL_DAYS, cc.REFRESH_DAYS]


def test_nothing_runs_unless_enabled(monkeypatch):
    monkeypatch.delenv("ACTUAL_COST_ENABLED", raising=False)
    assert cc.refresh_due(force=True) == []
