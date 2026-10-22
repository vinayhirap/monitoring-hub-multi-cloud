# tests/test_env_polling_rollout.py
"""
Phase C -- keeping ENV_POLLING_ENABLED on: invariants of the interval rules over EVERY scheduled AWS metric, a
simulated day of collector ticks for a PROD-shaped fleet, and the logic of scripts/env_polling_verify.py.

No database and no AWS. The real-database run of the verify script is tests/test_env_polling_rollout_integration.py.

Invariants proved here
  * a metric is never polled faster than its base interval, whatever the POLL_* settings (hostile values too)
  * flag OFF (or ENV_AWARE_CLASSIFICATION missing) gives exactly the prod answer for every metric / environment
  * wafv2, daily (>= 24 h) metrics and the free Describe-API metrics are never slowed
  * UAT critical-class metrics keep prod cadence; prod / unknown / ignore / unclassified are never slowed
  * over a simulated 24 h the real select_due() gives the cadence the docs promise, and every slowed poll
    looks back far enough to cover the ticks it skipped
"""
import importlib.util
import os
import random
import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from app.collector import env_polling as ep  # noqa: E402
from app.collector import polling_model as pm  # noqa: E402
from tests.conftest import install_stub  # noqa: E402

ROOT = __file__.rsplit("/tests/", 1)[0]
ENV_VARS = ("ENV_POLLING_ENABLED", "ENV_AWARE_CLASSIFICATION", "POLL_UAT_MULTIPLIER", "POLL_DEV_INTERVAL_SECONDS",
            "POLL_DEV_CRITICAL_INTERVAL_SECONDS", "POLL_DEV_NONCRITICAL_MODE", "POLL_DEV_EVAL_CRITICAL_ONLY")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def inventory():
    return [r for r in _load("polling_audit_rollout", "scripts/polling_audit.py").inventory() if r["provider"] == "aws"]


@pytest.fixture(scope="module")
def verify():
    return _load("env_polling_verify_mod", "scripts/env_polling_verify.py")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    ep.state().clear()
    yield
    ep.state().clear()


def _rows(inventory, describe=False):
    p1 = pm.p1_metric_keys()
    for r in inventory:
        is_desc = pm.base_interval_seconds("aws", r["service"], r["db_metric"].lower()) is None
        if is_desc == describe:
            yield r["service"], r["db_metric"].lower(), int(r["interval_s"]), (r["service"], r["db_metric"].lower()) in p1


ALL_ENVS = ["prod", "uat", "dev", "unknown", "ignore", None, "", "PROD", "weird"]
HOSTILE = [
    {"POLL_UAT_MULTIPLIER": v} for v in ("-5", "0", "1", "3", "12", "99", "abc", "2.7")
] + [
    {"POLL_DEV_INTERVAL_SECONDS": v} for v in ("0", "1", "60", "3600", "999999", "x", "-1")
] + [
    {"POLL_DEV_CRITICAL_INTERVAL_SECONDS": v} for v in ("0", "1", "119", "600", "999999", "x")
] + [
    {"POLL_DEV_NONCRITICAL_MODE": v} for v in ("off", "slow", "OFF", "bogus")
] + [{}]


def _set(monkeypatch, settings, on=True):
    if on:
        monkeypatch.setenv("ENV_POLLING_ENABLED", "true")
        monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    for k, v in settings.items():
        monkeypatch.setenv(k, v)


# ── flags: the exact PROD transition ────────────────────────────────────
def test_prod_today_is_off_and_stays_the_prod_answer(monkeypatch, inventory):
    """PROD .env today: ENV_AWARE_CLASSIFICATION=true, ENV_POLLING_ENABLED unset."""
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    assert ep.requested() is False and ep.enabled() is False
    for rtype, db, base, crit in _rows(inventory):
        for env in ALL_ENVS:
            assert ep.effective_interval(env, base, crit, rtype) == base


def test_setting_the_flag_turns_it_on_for_the_first_time(monkeypatch):
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    monkeypatch.setenv("ENV_POLLING_ENABLED", "true")
    assert ep.enabled() is True
    assert ep.effective_interval("prod", 300, False, "ec2") == 300
    assert ep.effective_interval("uat", 300, False, "ec2") == 900
    assert ep.effective_interval("dev", 300, False, "ec2") == 3600
    assert ep.effective_interval("dev", 120, True, "rds") == 600
    assert ep.effective_interval("uat", 120, True, "rds") == 120


def test_enabled_requires_both_flags(monkeypatch):
    monkeypatch.setenv("ENV_POLLING_ENABLED", "true")
    assert ep.enabled() is False                                 # classification missing: safe answer
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "false")
    assert ep.enabled() is False
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    monkeypatch.setenv("ENV_POLLING_ENABLED", "false")
    assert ep.enabled() is False


@pytest.mark.parametrize("word,expected", [("true", True), ("TRUE", True), (" true ", True), ("1", True),
                                           ("yes", True), ("on", True), ("false", False), ("0", False),
                                           ("", False), ("ture", False), ("enabled", False)])
def test_flag_words(monkeypatch, word, expected):
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    monkeypatch.setenv("ENV_POLLING_ENABLED", word)
    assert ep.enabled() is expected


# ── invariants over every scheduled AWS metric ──────────────────────────
@pytest.mark.parametrize("settings", HOSTILE, ids=lambda s: ",".join(f"{k}={v}" for k, v in s.items()) or "defaults")
def test_never_faster_than_base_and_hard_exemptions_hold(monkeypatch, inventory, settings):
    _set(monkeypatch, settings)
    for rtype, db, base, crit in _rows(inventory):
        for env in ALL_ENVS:
            eff = ep.effective_interval(env, base, crit, rtype)
            where = f"{rtype}/{db} env={env!r} {settings}"
            assert eff is None or eff >= base, where
            if eff is None:
                assert ep.normalize_env(env) == ep.DEV and not crit, where     # only DEV non-critical, mode=off
            if ep.normalize_env(env) == ep.PROD:
                assert eff == base, where
            if rtype in ep.EXEMPT_RESOURCE_TYPES or base >= 86400:
                assert eff == base, where
            if ep.normalize_env(env) == ep.UAT and crit:
                assert eff == base, where
            if ep.normalize_env(env) == ep.DEV and crit and rtype not in ep.EXEMPT_RESOURCE_TYPES and base < 86400:
                assert eff >= ep.dev_critical_interval_seconds(), where
        for fn in (ep.extra_window_minutes, ep.alert_for_scale):
            assert fn("uat", base, crit, rtype) >= (1 if fn is ep.alert_for_scale else 0)


@pytest.mark.parametrize("settings", HOSTILE[:4] + HOSTILE[-1:], ids=lambda s: str(s))
def test_flag_off_is_the_prod_answer_for_every_metric(monkeypatch, inventory, settings):
    _set(monkeypatch, settings, on=False)
    for rtype, db, base, crit in _rows(inventory):
        prod = ep.effective_interval("prod", base, crit, rtype)
        for env in ALL_ENVS:
            assert ep.effective_interval(env, base, crit, rtype) == prod == base
            assert ep.extra_window_minutes(env, base, crit, rtype) == 0
            assert ep.alert_for_scale(env, base, crit, rtype) == 1
            assert ep.evaluate_for_alerts(env, crit, rtype) is True


def test_describe_api_metrics_are_never_asked_about(inventory):
    """The collector, evaluator and metric API skip a Describe-API metric (base_interval_seconds is None)
    before consulting env_polling, so the free 60 s poll is never slowed."""
    desc = list(_rows(inventory, describe=True))
    assert {(t, m) for t, m, _b, _c in desc} == set(pm.AWS_DESCRIBE_METRICS)
    for t, m, _b, _c in desc:
        assert pm.base_interval_seconds("aws", t, m) is None


def test_every_critical_metric_is_p1_and_nothing_else_on_a_slow_tier(inventory):
    p1 = pm.p1_metric_keys()
    for rtype, db, base, crit in _rows(inventory):
        if crit:
            assert base == pm.TIER_SECONDS["critical"], f"{rtype}/{db} is P1 but polled every {base}s"


# ── a simulated day of collector ticks for a PROD-shaped fleet ──────────
FLEET = ([("prod", i) for i in range(89)] + [("uat", 100 + i) for i in range(10)]
         + [("dev", 200 + i) for i in range(26)] + [(None, 300 + i) for i in range(209)])
DAY = 86400


def _fleet():
    return [{"id": pk, "resource_type": "ec2", "env_resolved": env} for env, pk in FLEET]


def _simulate(tier, critical, restart_at=None, seed=7, hours=24):
    """Drive the REAL select_due with jittered ticks every `base` seconds. -> {resource id: [(t, extra_min)]}"""
    base = pm.TIER_SECONDS[tier]
    rnd = random.Random(seed)
    st = ep.PollState()
    res = _fleet()
    log = {r["id"]: [] for r in res}
    t0 = 1_700_000_000.0
    for k in range(int(hours * 3600 // base)):                    # a tick every `base`, +-20 s jitter, no drift
        t = t0 + k * base + rnd.uniform(-20, 20)
        if restart_at is not None and abs(t - (t0 + restart_at)) < base / 2:
            st.clear()
        due, _skipped = ep.select_due(res, tier, base, t, critical=critical, state_obj=st, force_enabled=True)
        for r in due:
            log[r["id"]].append((t, r.get("_poll_extra_min", 0)))
    return log


@pytest.mark.parametrize("tier,critical", [("critical", True), ("standard", False), ("low", False)])
def test_a_day_of_ticks_gives_the_documented_cadence(tier, critical):
    base = pm.TIER_SECONDS[tier]
    log = _simulate(tier, critical)
    per_env = {}
    for env, pk in FLEET:
        per_env.setdefault(ep.normalize_env(env) if env in ("uat", "dev") else "prod-like", []).append(len(log[pk]))
    full = DAY / base
    for n in per_env["prod-like"]:
        assert abs(n - full) <= 2                                          # every tick, untouched
    uat = DAY / (base if critical else base * 3)
    for n in per_env["uat"]:
        assert abs(n - uat) <= 2
    dev = DAY / (max(base, 600) if critical else max(base, 3600))
    for n in per_env["dev"]:
        assert abs(n - dev) <= 2


@pytest.mark.parametrize("tier,critical", [("critical", True), ("standard", False), ("low", False)])
def test_every_slowed_poll_looks_back_over_the_ticks_it_skipped(tier, critical):
    """Same bucket size, same number of stored points: window (base + extra, rounded up to 5 min by the collector)
    must reach back to the previous collection, so slowing leaves no hole in history."""
    base = pm.TIER_SECONDS[tier]
    slowed = {pk for env, pk in FLEET if env in ("uat", "dev")}      # prod-like polls every tick with its own window
    for pk, events in _simulate(tier, critical).items():
        if pk not in slowed:
            continue
        for (t0, _e0), (t1, e1) in zip(events, events[1:]):
            window = base + ep.round_extra(e1) * 60
            # 40 s of slack = the +-20 s tick jitter of the simulation
            assert window >= (t1 - t0) - 41, f"resource {pk}: gap {t1 - t0:.0f}s, window {window}s"


def test_prod_like_resources_are_never_annotated_or_skipped():
    log = _simulate("standard", False)
    for env, pk in FLEET:
        if env not in ("uat", "dev"):
            assert all(extra == 0 for _t, extra in log[pk])
            assert len(log[pk]) >= DAY / 300 - 2


def test_a_restart_costs_at_most_one_extra_collection_per_slowed_resource():
    base_log, restart_log = _simulate("standard", False), _simulate("standard", False, restart_at=12 * 3600)
    for env, pk in FLEET:
        extra = len(restart_log[pk]) - len(base_log[pk])
        assert extra <= 1, (env, pk, extra)
        if env not in ("uat", "dev"):
            assert extra == 0


def test_flag_off_returns_the_input_untouched_on_every_tick():
    res = _fleet()
    for t in range(0, 3600, 300):
        due, skipped = ep.select_due(res, "standard", 300, 1_700_000_000.0 + t, state_obj=ep.PollState(),
                                     force_enabled=False)
        assert due == res and skipped == 0 and all("_poll_extra_min" not in r for r in due)


# ── scripts/env_polling_verify.py: pure logic ───────────────────────────
def test_config_check_passes_on_the_real_catalog(verify, inventory):
    violations, matrix = verify.check_config(inventory, on=True)
    assert violations == []
    row = next(m for m in matrix if m["base"] == 120 and m["critical"])
    assert (row["prod"], row["uat"], row["dev"]) == (120, 120, 600)
    row = next(m for m in matrix if m["base"] == 300 and not m["critical"])
    assert (row["prod"], row["uat"], row["dev"]) == (300, 900, 3600)


@pytest.mark.parametrize("breaker", ["faster", "slows_prod", "slows_uat_critical", "flag_off_changes"])
def test_config_check_detects_a_broken_rule(verify, inventory, monkeypatch, breaker):
    vep = verify.ep                       # the module object the script itself uses (other tests may swap sys.modules)
    real = vep.effective_interval

    def broken(env, base, critical, rtype=None, force_enabled=None):
        v = real(env, base, critical, rtype, force_enabled)
        e = vep.normalize_env(env)
        if breaker == "faster" and e == "uat" and v:
            return v // 2
        if breaker == "slows_prod" and e == "prod" and force_enabled is not False:
            return base * 2
        if breaker == "slows_uat_critical" and e == "uat" and critical and force_enabled is not False:
            return base * 3
        if breaker == "flag_off_changes" and force_enabled is False and e == "dev":
            return base * 2
        return v

    monkeypatch.setattr(vep, "effective_interval", broken)
    violations, _ = verify.check_config(inventory, on=True)
    assert violations, breaker


def test_settings_report_prod_today(verify):
    st = verify.settings_report({"ENV_AWARE_CLASSIFICATION": "true"})
    assert st["flags"] == {"requested": False, "classification": True, "enabled": False}
    assert st["warnings"] == []
    assert next(r for r in st["rows"] if r["name"] == "ENV_POLLING_ENABLED")["status"] == "default"


def test_settings_report_after_the_change(verify):
    st = verify.settings_report({"ENV_AWARE_CLASSIFICATION": "true", "ENV_POLLING_ENABLED": "true"})
    assert st["flags"]["enabled"] is True and st["warnings"] == []
    eff = {r["name"]: r["effective"] for r in st["rows"]}
    assert eff["POLL_UAT_MULTIPLIER"] == 3 and eff["POLL_DEV_INTERVAL_SECONDS"] == 3600
    assert eff["POLL_DEV_CRITICAL_INTERVAL_SECONDS"] == 600 and eff["POLL_DEV_NONCRITICAL_MODE"] == "slow"
    assert eff["POLL_DEV_EVAL_CRITICAL_ONLY"] is True


def test_settings_report_warns_about_every_trap(verify):
    st = verify.settings_report({
        "ENV_POLLING_ENABLED": "ture", "POLL_UAT_MULTIPLIER": "99", "POLL_DEV_INTERVAL_SECONDS": "abc",
        "POLL_DEV_NONCRITICAL_MODE": "none", "POLL_DEV_INTERVALL_SECONDS": "60", "ENV_DIGEST_ENABLED": "true"})
    text = " | ".join(st["warnings"])
    assert "ENV_POLLING_ENABLED='ture'" in text and "counts as FALSE" in text
    assert "POLL_UAT_MULTIPLIER=99" in text and "uses 12" in text
    assert "POLL_DEV_INTERVAL_SECONDS='abc'" in text
    assert "POLL_DEV_NONCRITICAL_MODE='none'" in text
    assert "POLL_DEV_INTERVALL_SECONDS" in text and "nothing reads it" in text
    assert "ENV_DIGEST_ENABLED" not in text                       # unrelated ENV_ variable is not a typo


def test_settings_report_requested_without_classification(verify):
    st = verify.settings_report({"ENV_POLLING_ENABLED": "true"})
    assert st["flags"]["enabled"] is False
    assert any("ENV_AWARE_CLASSIFICATION" in w for w in st["warnings"])


def test_scan_env_file_reads_only_this_features_variables(verify, tmp_path):
    p = tmp_path / ".env"
    p.write_text('DB_PASSWORD=s3cret\nENV_POLLING_ENABLED="true"\n# POLL_UAT_MULTIPLIER=9\nexport POLL_UAT_MULTIPLIER = 4\n'
                 "ENV_POLLING_ENABLED=false\nJWT_SECRET=abc\n")
    got = verify.scan_env_file(str(p))
    assert [(n, v) for _no, n, v in got] == [("ENV_POLLING_ENABLED", "true"), ("POLL_UAT_MULTIPLIER", "4"),
                                              ("ENV_POLLING_ENABLED", "false")]
    assert "s3cret" not in repr(got) and "abc" not in repr(got)
    st = verify.settings_report({}, env_file_entries=got)
    assert any("ENV_POLLING_ENABLED conflicting values" in w and "LAST one wins" in w for w in st["warnings"])
    assert verify.scan_env_file(str(tmp_path / "missing")) == []


@pytest.mark.parametrize("ages,n,base,exp,up,want", [
    ([100, 200], 2, 300, 300, None, "OK"),
    ([100, 900], 2, 300, 300, None, "SLOWER"),                          # 900 s old on a 5-min tier
    ([], 2, 300, 300, None, "NO_DATA"),
    ([100, 800], 8, 300, 900, None, "UNPROVEN"),                        # slowed, restart age unknown
    ([100, 800], 8, 300, 900, 10, "TOO_EARLY"),
    ([100, 200, 300], 3, 300, 900, 600, "FEW"),
    ([60, 120, 200, 250, 300, 340, 400], 7, 300, 900, 600, "FASTER"),   # all younger than half of 15 min
    ([100, 400, 650, 800, 880, 300, 500], 7, 300, 900, 600, "OK"),
    ([100, 1700], 8, 300, 900, 600, "SLOWER"),
    ([4000, 5000], 2, 300, None, 600, "NOT_COLLECTED"),
    ([100, 5000], 2, 300, None, 600, "STILL_COLLECTED"),
    ([100], 1, 300, None, None, "UNPROVEN"),
])
def test_judge_group(verify, ages, n, base, exp, up, want):
    assert verify.judge_group(ages, n, base, exp, up) == want


def _ser(rid, rtype, env, metric, age_s, pk=None):
    return {"resource_pk": pk or rid, "resource_id": rid, "resource_type": rtype, "name": rid, "account": "A",
            "env": env, "metric_name": metric, "age_s": age_s}


def test_analyse_series_groups_expected_and_flags(verify):
    rows = [_ser(f"p{i}", "ec2", "prod", "cpuutilization", 100 + 40 * i) for i in range(6)]
    rows += [_ser(f"u{i}", "ec2", "uat", "cpuutilization", 100 + 130 * i) for i in range(6)]   # up to 750 s
    rows += [_ser("d1", "ec2", "dev", "cpuutilization", 9000)]                                  # > 3600 + slack
    rows += [_ser("x1", "ec2", None, "cpuutilization", 200)]
    rows += [_ser("s1", "ec2", "prod", "statuscheckfailed", 30)]                                # free Describe metric
    groups, late = verify.analyse_series(rows, on=True, uptime_min=600, sparse_fn=lambda t, m: False)
    by = {(g["resource_type"], g["env"], g["base_s"]): g for g in groups}
    assert by[("ec2", "prod", 300)]["expected_s"] == 300 and by[("ec2", "prod", 300)]["verdict"] == "OK"
    assert by[("ec2", "uat", 300)]["expected_s"] == 900 and by[("ec2", "uat", 300)]["verdict"] == "OK"
    assert by[("ec2", "dev", 300)]["expected_s"] == 3600 and by[("ec2", "dev", 300)]["verdict"] == "SLOWER"
    assert by[("ec2", "unclassified", 300)]["expected_s"] == 300
    assert by[("ec2", "prod", 60)]["expected_s"] == 60                      # Describe metric: never slowed
    assert [x["resource_id"] for x in late] == ["d1"]


def test_metric_with_no_datapoint_is_not_a_late_schedule(verify):
    """PROD 2026-10-22: idle EBS volumes had no VolumeQueueLength for weeks while the same volume's other metrics
    (and the other volumes' 5-min metrics) were fresh. The resource was collected; CloudWatch had no datapoint."""
    rows = [_ser(f"v{i}", "ebs", "prod", "volumeidletime", 100 + 10 * i) for i in range(8)]      # 5-min tier, fresh
    rows += [_ser("v0", "ebs", "prod", "volumequeuelength", 3_600_000, pk="v0")]                  # same tier, 60,000 min old
    rows += [_ser("v0", "ebs", "prod", "volumereadops", 5)]                                       # 15-min tier, fresh
    groups, late = verify.analyse_series(rows, on=False, sparse_fn=lambda t, m: False)
    assert [(x["metric"], x["kind"]) for x in late] == [("volumequeuelength", "NO_DATAPOINT")]
    assert all(g["verdict"] == "OK" for g in groups)


def test_a_whole_old_resource_or_tier_is_still_late(verify):
    sf = lambda t, m: False                                                                       # noqa: E731
    # the resource has nothing fresh at all: late
    rows = [_ser(f"v{i}", "ebs", "prod", "volumeidletime", 100 + 10 * i) for i in range(8)]
    rows += [_ser("gone", "ebs", "prod", "volumeidletime", 3_600_000), _ser("gone", "ebs", "prod", "volumequeuelength", 3_600_000)]
    groups, late = verify.analyse_series(rows, on=False, sparse_fn=sf)
    assert {x["kind"] for x in late if x["resource_id"] == "gone"} == {"LATE"}
    assert next(g for g in groups if g["base_s"] == 300)["verdict"] == "SLOWER"
    # the whole 5-min tier is old although the resources' 15-min metrics are fresh: late, not "no datapoint"
    rows = [_ser(f"w{i}", "ebs", "prod", "volumeidletime", 3_600_000) for i in range(8)]
    rows += [_ser(f"w{i}", "ebs", "prod", "volumereadops", 5) for i in range(8)]
    groups, late = verify.analyse_series(rows, on=False, sparse_fn=sf)
    assert {x["kind"] for x in late} == {"LATE"}
    assert next(g for g in groups if g["base_s"] == 300)["verdict"] == "SLOWER"


def test_analyse_series_flags_a_slowdown_that_is_not_applied(verify):
    """flag expected ON but UAT resources are still polled every 5 min (service not restarted / flag not read)."""
    rows = [_ser(f"u{i}", "ec2", "uat", "cpuutilization", 60 + 30 * i) for i in range(8)]
    g, _ = verify.analyse_series(rows, on=True, uptime_min=600, sparse_fn=lambda t, m: False)
    assert g[0]["verdict"] == "FASTER"
    g, _ = verify.analyse_series(rows, on=False, uptime_min=600, sparse_fn=lambda t, m: False)
    assert g[0]["verdict"] == "OK" and g[0]["expected_s"] == 300


def test_analyse_series_does_not_judge_sparse_metrics(verify):
    rows = [_ser("l1", "lambda", "prod", "invocations", 90000)]
    g, late = verify.analyse_series(rows, on=True, sparse_fn=lambda t, m: True)
    assert late == [] and g[0]["sparse_not_judged"] == 1 and g[0]["verdict"] == "SPARSE_ONLY"


def test_alert_findings_dev_daily_metric_is_not_false_stale(verify):
    """DEV ACM DaysToExpiry (daily, never slowed) is still evaluated, so it is not reported as FALSE_STALE."""
    f = verify.alert_findings([_alert(1, "certificatemanager", "dev", "daystoexpiry", stale_on=3000)])
    assert not [x for x in f if x["kind"] == "FALSE_STALE"]


def _alert(i, rtype, env, metric, stale_on=20, status="active", hard=72):
    return {"id": i, "resource_id": f"r{i}", "resource_type": rtype, "name": f"n{i}", "account": "A", "env": env,
            "metric_name": metric, "status": status, "severity": "WARNING", "age_min": 5,
            "stale_on_min": stale_on, "stale_off_min": 20, "hard_expiry_h": hard}


def test_alert_findings(verify):
    rows = [
        _alert(1, "ec2", "dev", "cpuutilization", stale_on=20),                 # DEV non-critical
        _alert(2, "rds", "dev", "cpuutilization", stale_on=30),                 # DEV critical-class: evaluated
        _alert(3, "ec2", "uat", "cpuutilization", stale_on=30),                 # UAT, window grown: ok
        _alert(4, "ec2", "uat", "cpuutilization", stale_on=20),                 # UAT, limit not grown: tight
        _alert(5, "ec2", "prod", "cpuutilization", stale_on=20),                # prod: ignored
        _alert(6, "rds", "uat", "cpuutilization", stale_on=20),                 # UAT critical-class: unchanged
        _alert(7, "ec2", "dev", "statuscheckfailed", stale_on=20),              # Describe metric: never slowed
    ]
    f = {x["alert_id"]: x for x in verify.alert_findings(rows)}
    assert f[1]["kind"] == "FALSE_STALE" and f[1]["level"] == "WARN" and "72 h" in f[1]["message"]
    assert f[2]['kind'] == 'SLOWER_LIFECYCLE' and f[2]['level'] == 'INFO'     # evaluated, just every 10 min
    assert f[3]["kind"] == "SLOWER_LIFECYCLE" and "3x" in f[3]["message"]
    assert f[4]["kind"] == "STALE_LIMIT_TIGHT" and f[4]["level"] == "FAIL"
    assert 5 not in f and 6 not in f and 7 not in f


def test_alert_findings_dev_all_metrics_when_the_setting_says_so(verify, monkeypatch):
    monkeypatch.setenv("POLL_DEV_EVAL_CRITICAL_ONLY", "false")
    kinds = [x["kind"] for x in verify.alert_findings([_alert(1, "ec2", "dev", "cpuutilization", stale_on=200)])]
    assert "FALSE_STALE" not in kinds


def test_dev_not_collected_mode_marks_alerts_false_stale(verify, monkeypatch):
    monkeypatch.setenv("POLL_DEV_NONCRITICAL_MODE", "off")
    monkeypatch.setenv("POLL_DEV_EVAL_CRITICAL_ONLY", "false")
    f = verify.alert_findings([_alert(1, "ec2", "dev", "cpuutilization", stale_on=200)])
    assert f and f[0]["kind"] == "FALSE_STALE"


def test_unknown_advice(verify):
    sib = [{"resource_id": f"i-{i}", "name": f"payroll-batch-0{i}", "tags": {}, "env": "dev", "source": "name",
            "aws_account_id": 1} for i in range(1, 4)]
    rows = [
        {"id": 1, "aws_account_id": 1, "account_name": "A", "resource_id": "i-9", "resource_type": "ec2",
         "name": "payroll-batch-09", "tags": {}, "instance_state": "running"},
        {"id": 2, "aws_account_id": 1, "account_name": "A", "resource_id": "i-10", "resource_type": "ec2",
         "name": "web-dev-prod", "tags": {"Environment": "blue"}, "instance_state": "running"},
        {"id": 3, "aws_account_id": 1, "account_name": "A", "resource_id": "vol-1", "resource_type": "ebs",
         "name": None, "tags": {"parent_ec2": "i-10"}, "instance_state": None},
        {"id": 4, "aws_account_id": 1, "account_name": "A", "resource_id": "vol-2", "resource_type": "ebs",
         "name": "vol-2", "tags": {}, "instance_state": None},
    ]
    parents = {3: [{"resource_type": "ec2", "resource_id": "i-10", "name": "web-dev-prod", "env_resolved": "unknown",
                    "env_source": "unknown"}]}
    out = {a["resource_id"]: a for a in verify.unknown_advice(rows, parents, sib)}
    assert out["i-9"]["suggestion"]["env"] == "dev" and any("Environment=prod|uat|dev" in n for n in out["i-9"]["needs"])
    assert any("holds no single tier word" in n for n in out["i-10"]["needs"])
    assert any("conflicting tier words" in n for n in out["i-10"]["needs"])
    assert any("tag THAT instance" in n for n in out["vol-1"]["needs"])
    assert any("follows its parent" in n for n in out["vol-1"]["needs"])
    assert any("no attached_to edge" in n for n in out["vol-2"]["needs"])
    for a in out.values():
        assert a["if_uat_every_s"] == 900 and a["if_dev_every_s"] == 3600


# ── read-only guarantee ─────────────────────────────────────────────────
class _RecordingCursor:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append(sql)

    def fetchall(self):
        return []


def test_script_issues_only_selects_and_restores_the_environment(verify, monkeypatch):
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    before = dict(os.environ)
    cur = _RecordingCursor()
    verify.fetch_series(cur, 1)
    verify.fetch_alerts(cur, 1)
    verify.fetch_alerts(cur)
    verify.fetch_unknown(cur, 1)
    assert cur.sql and all(s.lstrip().upper().startswith("SELECT") for s in cur.sql)
    for s in cur.sql:
        for bad in (" INSERT ", " UPDATE ", " DELETE ", " DROP ", " ALTER ", "FOR UPDATE", "OUTFILE"):
            assert bad not in s.upper(), (bad, s[:80])
    assert dict(os.environ) == before                                   # flag toggling for SQL building is undone


def test_bound_parameter_queries_double_the_percent_signs(verify, monkeypatch):
    """mysql-connector formats the SQL with % when parameters are bound: the LIKE '...%' of the alert-window
    builders must be escaped then, and left alone when there are no parameters."""
    cur = _RecordingCursor()
    verify.fetch_alerts(cur, 7)
    verify.fetch_alerts(cur)
    with_params, without = cur.sql
    import re
    assert not re.search(r"(?<!%)%(?!%|s)", with_params.replace("%%", ""))      # only %% and %s remain
    assert "%" in without


def test_non_select_is_refused(verify):
    for sql in ("UPDATE alerts SET status='x'", "  delete from alerts", "SET SESSION x=1", "INSERT INTO t VALUES (1)"):
        with pytest.raises(RuntimeError):
            verify._only_select(sql)
    assert verify._only_select("  select 1") == "  select 1"


def test_flag_state_context_restores_on_error(verify, monkeypatch):
    monkeypatch.setenv("ENV_POLLING_ENABLED", "weird")
    monkeypatch.delenv("ENV_AWARE_CLASSIFICATION", raising=False)
    with pytest.raises(ValueError):
        with verify._flag_state(True):
            assert ep.enabled()
            raise ValueError("boom")
    assert os.environ["ENV_POLLING_ENABLED"] == "weird" and "ENV_AWARE_CLASSIFICATION" not in os.environ


def test_stale_resource_hours_matches_the_collector():
    src = open(os.path.join(ROOT, "app/collector/metrics/runner.py")).read()
    import re
    assert int(re.search(r"^STALE_RESOURCE_HOURS = (\d+)", src, re.M).group(1)) == 48
