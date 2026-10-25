"""Cost by polling tier and this-month / last-month totals: pure tests, no database, no cloud, no network."""
import json
from datetime import date

import pytest

from tests.test_usage_cost_api import usage, row  # noqa: F401  (the `usage` fixture loads app/api/usage.py with stubs)


# ── cost by polling tier ────────────────────────────────────────────────────────
def test_tiers_are_ranked_by_cost_and_shares_add_to_100(usage):
    rows = [row("aws", "critical", 10, 600_000), row("aws", "standard", 10, 300_000), row("aws", "extended", 5, 100_000)]
    r = usage.build_tier_cost(rows, 7, {"enabled": True})
    assert [t["tier"] for t in r["tiers"]] == ["critical", "standard", "extended"]
    assert r["total_usd"] == pytest.approx(10.0)
    assert [t["share_pct"] for t in r["tiers"]] == [60.0, 30.0, 10.0]
    assert sum(t["share_pct"] for t in r["tiers"]) == pytest.approx(100.0)


def test_halving_saving_is_half_the_tier_cost_and_only_for_scheduled_polling(usage):
    rows = [row("aws", "critical", 1, 200_000), row("aws", "ui", 1, 100_000, source="ui"), row("aws", "tag_sync", 1, 0)]
    t = {x["tier"]: x for x in usage.build_tier_cost(rows, 7)["tiers"]}
    assert t["critical"]["halve_saving_usd"] == pytest.approx(t["critical"]["usd"] / 2)
    assert t["ui"]["halve_saving_usd"] is None and t["ui"]["user_triggered"] is True       # a person opening a page is not a schedule
    assert t["tag_sync"]["halve_saving_usd"] is None and t["tag_sync"]["free"] is True


def test_list_metrics_requests_are_not_added_to_metrics_requested_or_price(usage):
    rows = [row("aws", "standard", 1, 1000), row("aws", "standard", 1, 50, operation="ListMetrics", price=None)]
    d = json.loads(rows[1]); d["list_price_usd"] = None; rows[1] = json.dumps(d)
    t = usage.build_tier_cost(rows, 7)["tiers"][0]
    assert t["units"] == 1000 and t["request_units"] == 50
    assert t["unpriced_units"] == 50 and t["usd"] == pytest.approx(0.01)


def test_levers_name_the_largest_tier_and_env_polling_only_when_off(usage):
    rows = [row("aws", "critical", 1, 500_000), row("aws", "ui", 1, 100_000, source="ui")]
    off = usage.build_tier_cost(rows, 7, {"enabled": False})["levers"]
    assert [l["kind"] for l in off] == ["largest_tier", "user_triggered", "env_polling_off"]
    assert "Critical (2 min)" in off[0]["text"]
    on = usage.build_tier_cost(rows, 7, {"enabled": True})["levers"]
    assert "env_polling_off" not in [l["kind"] for l in on]
    assert "arithmetic" in off[0]["text"]                                                 # it must not read as a forecast


def test_no_usage_gives_an_empty_honest_report(usage):
    r = usage.build_tier_cost([], 7, {"enabled": True})
    assert r["tiers"] == [] and r["levers"] == [] and r["total_usd"] == 0 and r["freshness"]["status"] == "none"


# ── this month and last month ───────────────────────────────────────────────────
def _rep(daily, state="ok", through=None):
    keys = sorted(daily)
    return {"state": state, "periods": [{"key": k} for k in keys],
            "groups": [{"values": [daily[k] for k in keys]}],
            "through_date": through or (keys[-1] if keys else None)}


def _days(y, m, first, last, usd):
    return {date(y, m, d).isoformat(): usd for d in range(first, last + 1)}


def test_this_month_last_month_and_like_for_like_comparison(usage):
    daily = {**_days(2026, 9, 1, 30, 2.0), **_days(2026, 10, 1, 23, 3.0)}
    r = usage.build_monthly_totals({"aws": _rep(daily)}, date(2026, 10, 25))
    assert r["this_month"]["usd"] == pytest.approx(69.0)
    assert r["last_month"]["usd"] == pytest.approx(60.0) and r["last_month"]["label"] == "2026-09"
    assert r["same_period_last_month_usd"] == pytest.approx(46.0)             # Sept 1..23, the same 23 days
    assert r["providers"][0]["last_month_partial"] is False


def test_there_is_no_forecast_in_the_response(usage):
    r = usage.build_monthly_totals({"aws": _rep(_days(2026, 10, 1, 20, 1.0))}, date(2026, 10, 25))
    text = json.dumps(r).lower()
    assert "forecast" in text and "no forecast" in text                         # only the disclaimer
    assert not any(k for k in r if "project" in k or "forecast" in k or "predict" in k)
    assert not any(k for k in r["this_month"] if "project" in k or "forecast" in k)


def test_history_that_starts_mid_month_flags_last_month_as_understated(usage):
    daily = {**_days(2026, 9, 18, 30, 2.0), **_days(2026, 10, 1, 20, 1.0)}
    r = usage.build_monthly_totals({"gcp": _rep(daily)}, date(2026, 10, 25))
    assert r["providers"][0]["last_month_partial"] is True and r["providers"][0]["data_starts"] == "2026-09-18"
    assert r["last_month"]["partial_coverage"] is True
    assert any("starts after the first day of last month" in n for n in r["notes"])


def test_unavailable_clouds_are_listed_not_counted_as_zero(usage):
    reps = {"aws": _rep(_days(2026, 10, 1, 10, 1.0)), "azure": {"state": "disabled"}, "gcp": {"state": "no_accounts"}}
    r = usage.build_monthly_totals(reps, date(2026, 10, 12))
    avail = {p["provider"]: p["available"] for p in r["providers"]}
    assert avail == {"aws": True, "azure": False, "gcp": False}
    assert r["this_month"]["usd"] == pytest.approx(10.0)


def test_first_days_of_a_month_before_any_data_has_no_like_for_like(usage):
    daily = _days(2026, 9, 1, 30, 2.0)                                            # billing data lags: nothing in October yet
    r = usage.build_monthly_totals({"aws": _rep(daily)}, date(2026, 10, 1))
    assert r["this_month"]["usd"] == 0 and r["providers"][0]["same_period_last_month_usd"] is None
    assert r["last_month"]["usd"] == pytest.approx(60.0)


def test_january_looks_back_to_december(usage):
    r = usage.build_monthly_totals({"aws": _rep({**_days(2025, 12, 1, 31, 1.0), **_days(2026, 1, 1, 5, 1.0)})}, date(2026, 1, 6))
    assert r["last_month"]["label"] == "2025-12" and r["last_month"]["usd"] == pytest.approx(31.0)
