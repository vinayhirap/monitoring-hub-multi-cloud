"""Phase 3 WAF: flag-gated polling model, pure helpers of the dedicated collector, rule discovery parsing."""
import importlib
import os

import pytest

from app.collector import polling_model as pm
from app.collector.discovery.extended import _waf_rule_entries
from app.collector.metrics import waf


def _reload(monkeypatch, **env):
    for k in (pm.WAF_FAST_ENV, pm.WAF_PER_RULE_ENV):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_flags_off_waf_stays_slow(monkeypatch):
    _reload(monkeypatch)
    assert pm.aws_extended_tier("wafv2", "BlockedRequests") == "slow_extended"
    assert not pm.waf_fast_enabled() and not pm.waf_per_rule_enabled()


def test_fast_flag_moves_key_metrics_to_standard_with_60s_period(monkeypatch):
    _reload(monkeypatch, WAF_FAST_POLLING_ENABLED="true")
    for m in ("AllowedRequests", "BlockedRequests", "CountedRequests", "CaptchaRequests", "ChallengeRequests"):
        assert pm.aws_extended_tier("wafv2", m) == "standard"
        assert pm.aws_extended_period_sec("wafv2", m) == 60
    assert pm.aws_extended_tier("wafv2", "RequestsWithValidCaptchaToken") == "low"


def test_per_rule_needs_fast(monkeypatch):
    _reload(monkeypatch, WAF_PER_RULE_ENABLED="true")
    assert not pm.waf_per_rule_enabled()


def test_block_ratio_is_never_requested_from_cloudwatch():
    from app.collector.metrics.extended import EXTENDED_METRICS
    assert "BlockRatio" not in [d[0] for d in EXTENDED_METRICS["wafv2"]]


def test_fill_minutes_zero_fills_gaps():
    assert waf.fill_minutes({60: 5.0}, 0, 180) == {0: 0.0, 60: 5.0, 120: 0.0}


def test_block_ratio_math_and_traffic_floor():
    s = {c: {0: 0.0, 60: 0.0} for c in waf.RATIO_COMPONENTS}
    s["BlockedRequests"] = {0: 10.0, 60: 1.0}
    s["AllowedRequests"] = {0: 70.0, 60: 1.0}
    s["CountedRequests"] = {0: 10.0, 60: 0.0}
    s["PassedRequests"] = {0: 10.0, 60: 0.0}
    assert waf.derive_block_ratio(s, 20) == {0: 10.0}      # minute 60 has 2 requests: no ratio
    assert waf.derive_block_ratio({"BlockedRequests": {0: 1.0}}, 1) == {}   # missing component: no ratio


def test_rule_targets_only_block_and_count(monkeypatch):
    res = {"tags": {"waf_rules": [
        {"metric": "a", "action": "BLOCK"}, {"metric": "b", "action": "ALLOW"},
        {"metric": "c", "action": "COUNT"}, {"metric": "d", "action": "NONE"}, {"metric": "e", "action": "CAPTCHA"}]}}
    keys = {("wafv2", "BlockedRequests"), ("wafv2", "CountedRequests")}
    assert [(k, r["metric"]) for k, r in waf.rule_targets(res, keys)] == [("blocked", "a"), ("counted", "c"), ("blocked", "d")]
    assert waf.rule_targets(res, set()) == []


def test_rule_discovery_skips_metricless_rules_and_uses_metric_name():
    rules = [
        {"Name": "N1", "Priority": 1, "Action": {"Block": {}}, "Statement": {},
         "VisibilityConfig": {"CloudWatchMetricsEnabled": True, "MetricName": "m1"}},
        {"Name": "N2", "Priority": 2, "Action": {"Block": {}}, "Statement": {},
         "VisibilityConfig": {"CloudWatchMetricsEnabled": False, "MetricName": "m2"}},
    ]
    out = _waf_rule_entries(rules)
    assert [(r["name"], r["metric"]) for r in out] == [("N1", "m1")]


def test_rule_db_name_fits_column():
    assert len(waf.rule_db_name("blocked", "x" * 128)) <= 150
