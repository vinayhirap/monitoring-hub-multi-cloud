"""Phase F: cloud API cost endpoint. Pure tests with fixture rows: no database, no cloud, no network."""
import json
import os
import sys
import time
from datetime import datetime, timezone

import pytest

import app  # noqa: F401  (real package first, so install_stub never replaces it with a bare stub)
from tests.conftest import install_stub, load_module


class _HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


class _Router:
    def __init__(self, *a, **k):
        pass

    def get(self, *a, **k):
        return lambda f: f


class _FakeCur:
    def __init__(self, rows_by_call):
        self.calls, self._rows = [], list(rows_by_call)

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchall(self):
        return self._rows.pop(0) if self._rows else []

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self, *a, **k):
        return self._cur

    def close(self):
        pass


@pytest.fixture
def usage():
    install_stub("fastapi", APIRouter=_Router, Depends=lambda f=None: f, Query=lambda d=None, **k: d,
                 HTTPException=_HTTPException)
    install_stub("app.auth.deps", get_current_user=lambda: {"id": 1})
    install_stub("app.db", get_connection=lambda: None)
    return load_module("app/api/usage.py")


def row(provider, tier, calls, units, price=None, end=None, source=None):
    unit_price = {"aws": 0.01 / 1000, "azure": 0.01 / 1000, "gcp": 0.50 / 1_000_000}.get(provider, 0.0)
    d = {"provider": provider, "tier": tier, "calls": calls, "units": units,
         "list_price_usd": round(units * unit_price, 6) if price is None else price,
         "window_start": (end or 1_700_000_000) - 3600, "window_end": end or 1_700_000_000}
    if source:
        d["source"] = source
    return json.dumps(d)


# The PROD baseline measured 2026-10-19 (7 days, AWS GetMetricData), one row per tier.
PROD = [row("aws", "critical", 9430, 70725), row("aws", "standard", 28142, 330466),
        row("aws", "low", 9493, 126801), row("aws", "extended", 1099, 4867),
        row("aws", "slow_extended", 60, 1830), row("aws", "tag_sync", 632, 0)]


def test_aggregation_matches_prod_baseline(usage):
    r = usage.build_report(PROD, 7, now=1_700_000_100)
    aws = r["providers"][0]
    assert aws["provider"] == "aws" and aws["unit"] == "metrics_requested"
    assert aws["units"] == 534_689
    assert aws["calls"] == 9430 + 28142 + 9493 + 1099 + 60 + 632
    assert aws["list_price_usd"] == pytest.approx(5.34689, abs=1e-3)
    by_tier = {t["tier"]: t for t in aws["tiers"]}
    assert by_tier["standard"]["share_of_units_pct"] == pytest.approx(61.8, abs=0.1)
    assert aws["tiers"][0]["tier"] == "standard"            # largest first


def test_tag_sync_is_free_and_has_no_units(usage):
    aws = usage.build_report(PROD, 7, now=1_700_000_100)["providers"][0]
    t = next(t for t in aws["tiers"] if t["tier"] == "tag_sync")
    assert t["free"] is True and t["units"] == 0 and t["calls"] == 632 and t["list_price_usd"] == 0


def test_rows_of_the_same_tier_from_two_flushes_are_summed(usage):
    r = usage.build_report([row("aws", "critical", 10, 100), row("aws", "critical", 5, 50)], 1, now=1_700_000_100)
    t = r["providers"][0]["tiers"][0]
    assert (t["calls"], t["units"], t["rows"]) == (15, 150, 2)


def test_empty_window(usage):
    r = usage.build_report([], 7, now=1_700_000_100)
    assert r["providers"] == [] and r["totals"] == {"calls": 0, "list_price_usd": 0.0, "providers": 0}
    assert r["freshness"]["status"] == "none" and r["freshness"]["last_flush_at"] is None
    assert r["freshness"]["minutes_since"] is None


def test_unknown_provider_is_shown_not_dropped_and_not_priced(usage):
    r = usage.build_report([row("oracle", "standard", 3, 30)], 1, now=1_700_000_100)
    p = r["providers"][0]
    assert p["provider"] == "oracle" and p["known_provider"] is False and p["unit"] == "units"
    assert p["units"] == 30 and p["list_price_usd"] == 0 and p["free_allowance"] is None


def test_unreadable_rows_are_counted_not_summed(usage):
    rows = ["not json", None, json.dumps({"tier": "x"}), json.dumps([1]), row("aws", "low", 1, 10)]
    r = usage.build_report(rows, 1, now=1_700_000_100)
    assert r["providers"][0]["units"] == 10
    assert r["freshness"]["rows_skipped"] == 4
    assert any("could not be read" in n for n in r["notes"])


def test_negative_and_non_numeric_values_count_as_zero(usage):
    bad = json.dumps({"provider": "aws", "tier": "low", "calls": "x", "units": -5, "list_price_usd": "nan"})
    r = usage.build_report([bad], 1, now=1_700_000_100)
    assert r["providers"][0]["units"] == 0 and r["providers"][0]["calls"] == 0


def test_detail_may_arrive_as_dict_or_bytes(usage):
    d = json.loads(row("aws", "low", 2, 20))
    r = usage.build_report([d, json.dumps(d).encode()], 1, now=1_700_000_100)
    assert r["providers"][0]["units"] == 40


def test_clouds_are_never_added_together(usage):
    r = usage.build_report([row("aws", "low", 1, 100), row("azure", "low", 2, 7)], 1, now=1_700_000_100)
    assert {p["provider"]: p["units"] for p in r["providers"]} == {"aws": 100, "azure": 7}
    assert r["totals"]["calls"] == 3 and "units" not in r["totals"]


# ── free allowance ──────────────────────────────────────────────────────
MID_OCT = datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc).timestamp()      # 15 of 31 days elapsed


def test_free_allowance_percent_and_projection(usage):
    pos = usage.free_allowance_position("azure", 400_000, now=MID_OCT)
    assert pos["limit"] == 1_000_000 and pos["per"] == "subscription"
    assert pos["percent_used"] == 40.0 and pos["over_single_allowance"] is False
    assert pos["projected_month_end"] == pytest.approx(400_000 * 31 / 15, rel=1e-6)
    assert pos["month_start"] == "2026-10-01"


def test_free_allowance_over_the_limit_and_gcp_unit(usage):
    pos = usage.free_allowance_position("gcp", 1_200_000, now=MID_OCT)
    assert pos["per"] == "billing account" and pos["unit"] == "time_series_returned"
    assert pos["over_single_allowance"] is True and pos["percent_used"] == 120.0


def test_free_allowance_none_for_aws_and_unknown(usage):
    assert usage.free_allowance_position("aws", 5, now=MID_OCT) is None
    assert usage.free_allowance_position("oracle", 5, now=MID_OCT) is None


def test_free_allowance_zero_usage_and_december_rollover(usage):
    dec = datetime(2026, 12, 31, 12, 0, tzinfo=timezone.utc).timestamp()
    pos = usage.free_allowance_position("azure", 0, now=dec)
    assert pos["percent_used"] == 0 and pos["projected_month_end"] == 0 and pos["month_start"] == "2026-12-01"


def test_report_uses_month_rows_for_the_allowance_not_the_window(usage):
    week = [row("azure", "standard", 100, 100, end=int(MID_OCT) - 60)]
    month = week + [row("azure", "standard", 900, 900, end=int(MID_OCT) - 86400)]
    r = usage.build_report(week, 7, month_rows=month, now=MID_OCT)
    az = r["providers"][0]
    assert az["units"] == 100 and az["free_allowance"]["month_to_date"] == 1000


def test_rows_since_drops_previous_month(usage):
    start = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
    keep = row("azure", "low", 1, 1, end=int(start) + 10)
    old = row("azure", "low", 1, 1, end=int(start) - 10)
    assert usage.rows_since([keep, old, "junk"], start) == [keep]


# ── sources, freshness, polling state ───────────────────────────────────
def test_unmetered_sources_reported_until_rows_carry_them(usage):
    r = usage.build_report(PROD, 7, now=1_700_000_100)
    assert r["not_metered"] == ["CloudWatch ListMetrics", "CloudWatch GetMetricStatistics"]
    assert all(t["source"] is None for t in r["providers"][0]["tiers"])
    later = PROD + [row("aws", "ui", 4, 4, source="list_metrics"), row("aws", "ui", 2, 2, source="GetMetricStatistics")]
    r2 = usage.build_report(later, 7, now=1_700_000_100)
    assert r2["not_metered"] == []
    assert {t["source"] for t in r2["providers"][0]["tiers"]} == {None, "list_metrics", "GetMetricStatistics"}


def test_not_metered_is_never_rendered_as_a_zero_row(usage):
    r = usage.build_report(PROD, 7, now=1_700_000_100)
    assert not any("ListMetrics" in str(t) for t in r["providers"][0]["tiers"])


def test_freshness_ok_and_delayed(usage):
    end = 1_700_000_000
    ok = usage.build_report([row("aws", "low", 1, 1, end=end)], 1, now=end + 30 * 60)["freshness"]
    assert ok["status"] == "ok" and ok["minutes_since"] == 30
    late = usage.build_report([row("aws", "low", 1, 1, end=end)], 1, now=end + 5 * 3600)
    assert late["freshness"]["status"] == "delayed"
    assert any("No usage flush" in n for n in late["notes"])


def test_flush_gap_counts_hours_with_and_without_rows(usage):
    now = 1_700_000_000 + 10 * 3600
    rows = [row("aws", "low", 1, 1, end=now - h * 3600) for h in (0, 1, 2, 5, 9)]
    f = usage.build_report(rows, 1, now=now)["freshness"]
    assert f["hours_with_flush"] == 5 and f["hours_in_window"] == 10


def test_label_and_counter_honesty_notes_always_present(usage):
    r = usage.build_report(PROD, 7, now=1_700_000_100)
    assert r["label"] == "List-price estimate, not the invoice"
    joined = " ".join(r["notes"])
    assert "not the invoice" in joined and "restarts" in joined and "never added across clouds" in joined


@pytest.mark.parametrize("env,enabled,fragment", [
    ({"ENV_AWARE_CLASSIFICATION": "true", "ENV_POLLING_ENABLED": "true"}, True, "ON:"),
    ({"ENV_AWARE_CLASSIFICATION": "true"}, False, "OFF:"),
    ({"ENV_POLLING_ENABLED": "true"}, False, "no effect"),
    ({}, False, "OFF:"),
])
def test_env_polling_state_follows_env_polling_enabled(usage, monkeypatch, env, enabled, fragment):
    for k in ("ENV_AWARE_CLASSIFICATION", "ENV_POLLING_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    st = usage.env_polling_state()
    assert st["enabled"] is enabled and fragment in st["note"]


# ── endpoint ────────────────────────────────────────────────────────────
def test_endpoint_rejects_days_outside_the_allowed_set(usage):
    for bad in (0, 2, 31, -1):
        with pytest.raises(_HTTPException) as e:
            usage.cloud_cost(days=bad, current_user={"id": 1})
        assert e.value.status_code == 400


def test_endpoint_queries_op_events_read_only_and_builds_report(usage, monkeypatch):
    now = time.time()
    rows = [(row("aws", "critical", 2, 20, end=int(now) - 600),)]
    cur = _FakeCur([rows, rows])
    monkeypatch.setattr(usage, "get_connection", lambda: _FakeConn(cur))
    out = usage.cloud_cost(days=7, current_user={"id": 1})
    assert out["days"] == 7 and out["providers"][0]["units"] == 20
    for sql, _params in cur.calls:
        s = sql.strip().upper()
        assert s.startswith("SELECT") and "METRIC_API_USAGE" in s and "INSERT" not in s and "UPDATE" not in s


def test_endpoint_database_error_is_a_clean_500(usage, monkeypatch):
    class Boom(_FakeCur):
        def execute(self, *a, **k):
            raise RuntimeError("db down")
    monkeypatch.setattr(usage, "get_connection", lambda: _FakeConn(Boom([])))
    with pytest.raises(_HTTPException) as e:
        usage.cloud_cost(days=1, current_user={"id": 1})
    assert e.value.status_code == 500 and "db down" not in str(e.value.detail)


def test_no_price_is_hard_coded_in_the_module(usage):
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "api", "usage.py")).read()
    assert "0.01" not in src and "0.50" not in src and "$" not in src
