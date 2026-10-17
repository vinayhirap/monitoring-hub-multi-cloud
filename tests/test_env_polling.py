# tests/test_env_polling.py
"""Phase 4c -- environment-based polling (ENV_POLLING_ENABLED).

Pure decision logic (app/collector/env_polling.py), the collector gate in runner.py /
extended.py, the alert-window SQL in alert_rules.py and the evaluator hooks. No database:
the real-DB checks are in tests/test_env_polling_integration.py.

Acceptance criteria covered here:
  * flag off  -> the set of (resource, metric) collected per tick is identical to before
  * flag on   -> prod / unknown / ignore / unclassified are collected exactly as before and
                 NO P1 metric is ever slowed on any prod-like environment
  * a reclassified resource changes cadence on the next tick, no restart
  * windows / staleness / required cycles follow the effective interval
"""
import math
import os
import re
import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app.alert_rules as alert_rules  # noqa: E402  (real package first, see conftest notes)
from app.collector import env_polling as ep  # noqa: E402
from app.collector import polling_model as pm  # noqa: E402
from tests.conftest import load_module, install_stub  # noqa: E402

PROD_LIKE = ["prod", "unknown", "ignore", None, "", "PROD", "weird-label"]
ALL_BASES = sorted(set(pm.TIER_SECONDS.values()))
ENV_VARS = ("ENV_POLLING_ENABLED", "ENV_AWARE_CLASSIFICATION", "POLL_UAT_MULTIPLIER",
            "POLL_DEV_INTERVAL_SECONDS", "POLL_DEV_CRITICAL_INTERVAL_SECONDS",
            "POLL_DEV_NONCRITICAL_MODE", "POLL_DEV_EVAL_CRITICAL_ONLY")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    ep.state().clear()
    alert_rules._ENV_EXTRA_CACHE.clear()
    yield
    ep.state().clear()
    alert_rules._ENV_EXTRA_CACHE.clear()


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv("ENV_POLLING_ENABLED", "true")
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")


# ── flags ───────────────────────────────────────────────────────────────
def test_default_is_off():
    assert ep.enabled() is False


def test_needs_both_flags(monkeypatch):
    monkeypatch.setenv("ENV_POLLING_ENABLED", "true")
    assert ep.requested() and not ep.enabled()          # no classification flag -> safe: off
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    assert ep.enabled()


@pytest.mark.parametrize("env", ["uat", "dev", "prod", "unknown", None])
@pytest.mark.parametrize("base", ALL_BASES)
@pytest.mark.parametrize("critical", [True, False])
def test_flag_off_nothing_is_ever_slowed(env, base, critical):
    assert ep.effective_interval(env, base, critical) == base
    assert ep.extra_window_minutes(env, base, critical) == 0
    assert ep.alert_for_scale(env, base, critical) == 1
    assert ep.evaluate_for_alerts(env, critical) is True


# ── HARD RULE: prod-like is never slowed ────────────────────────────────
@pytest.mark.parametrize("env", PROD_LIKE)
@pytest.mark.parametrize("base", ALL_BASES)
@pytest.mark.parametrize("critical", [True, False])
def test_prod_like_environments_are_never_slowed_with_flag_on(on, env, base, critical):
    assert ep.effective_interval(env, base, critical) == base
    assert ep.extra_window_minutes(env, base, critical) == 0
    assert ep.alert_for_scale(env, base, critical) == 1
    assert ep.evaluate_for_alerts(env, critical) is True
    assert not ep.is_slowed(env, base, critical)


def test_no_p1_metric_is_slowed_on_prod_even_with_hostile_settings(on, monkeypatch):
    monkeypatch.setenv("POLL_UAT_MULTIPLIER", "12")
    monkeypatch.setenv("POLL_DEV_INTERVAL_SECONDS", "86400")
    monkeypatch.setenv("POLL_DEV_CRITICAL_INTERVAL_SECONDS", "86400")
    p1 = pm.p1_metric_keys()
    assert p1, "the P1 set must not be empty"
    for rtype, metric in sorted(p1):
        base = pm.base_interval_seconds("aws", rtype, metric)
        if base is None:                                  # free Describe metric: never in GetMetricData
            continue
        for env in PROD_LIKE:
            assert ep.effective_interval(env, base, True, rtype) == base, (rtype, metric, env)


def test_every_core_p1_metric_is_on_the_critical_tier_and_nothing_else_is():
    """runner._tier_is_critical() relies on this equivalence."""
    p1 = pm.p1_metric_keys()
    for m in pm.AWS_CORE_METRICS:
        assert ((m.resource_type, m.db_name) in p1) == (m.tier == "critical"), m
    assert pm.CWAGENT_MEM_TIER != "critical" and pm.CWAGENT_DISK_TIER != "critical"
    for (svc, metric), tier in pm.AWS_EXTENDED_TIER_OVERRIDES.items():
        assert tier != "critical"


# ── the decision table ──────────────────────────────────────────────────
def test_uat_non_critical_is_three_times_base(on):
    assert ep.effective_interval("uat", 300, False) == 900
    assert ep.effective_interval("uat", 900, False) == 2700
    assert ep.effective_interval("uat", 3600, False) == 10800


def test_uat_critical_class_keeps_prod_cadence(on):
    assert ep.effective_interval("uat", 120, True) == 120
    assert ep.alert_for_scale("uat", 120, True) == 1
    assert ep.extra_window_minutes("uat", 120, True) == 0


def test_dev_intervals(on):
    assert ep.effective_interval("dev", 120, True) == 600       # critical class: 10 min
    assert ep.effective_interval("dev", 300, False) == 3600     # everything else: hourly
    assert ep.effective_interval("dev", 900, False) == 3600


def test_never_shorter_than_base(on, monkeypatch):
    monkeypatch.setenv("POLL_DEV_INTERVAL_SECONDS", "300")
    assert ep.effective_interval("dev", 3600, False) == 3600     # hourly base is not made faster
    monkeypatch.setenv("POLL_DEV_CRITICAL_INTERVAL_SECONDS", "120")
    assert ep.effective_interval("dev", 120, True) == 120


def test_daily_metrics_are_left_alone(on):
    for env in ("uat", "dev"):
        assert ep.effective_interval(env, 86400, False) == 86400


def test_wafv2_is_exempt(on):
    for env in ("uat", "dev"):
        assert ep.effective_interval(env, 300, False, "wafv2") == 300


def test_dev_noncritical_off_mode_not_collected(on, monkeypatch):
    monkeypatch.setenv("POLL_DEV_NONCRITICAL_MODE", "off")
    assert ep.effective_interval("dev", 300, False) is None
    assert ep.effective_interval("dev", 120, True) == 600        # critical class still collected
    assert ep.multiplier("dev", 300, False) is None
    assert ep.extra_window_minutes("dev", 300, False) == 0
    assert ep.alert_for_scale("dev", 300, False) == 1


def test_env_var_overrides_and_clamps(on, monkeypatch):
    monkeypatch.setenv("POLL_UAT_MULTIPLIER", "2")
    assert ep.effective_interval("uat", 300, False) == 600
    monkeypatch.setenv("POLL_UAT_MULTIPLIER", "9999")
    assert ep.uat_multiplier() == ep.MAX_MULTIPLIER
    monkeypatch.setenv("POLL_UAT_MULTIPLIER", "0")
    assert ep.uat_multiplier() == 1                              # 1 = no slowdown, never below base
    monkeypatch.setenv("POLL_UAT_MULTIPLIER", "banana")
    assert ep.uat_multiplier() == ep.DEFAULT_UAT_MULTIPLIER
    monkeypatch.setenv("POLL_DEV_INTERVAL_SECONDS", "10")
    assert ep.dev_interval_seconds() == ep.MIN_INTERVAL_SECONDS
    monkeypatch.setenv("POLL_DEV_NONCRITICAL_MODE", "nonsense")
    assert ep.dev_noncritical_mode() == "slow"


def test_extra_window_and_alert_for_scale(on):
    assert ep.extra_window_minutes("uat", 300, False) == 10      # 900-300 s
    assert ep.extra_window_minutes("uat", 900, False) == 30
    assert ep.extra_window_minutes("dev", 300, False) == 55
    assert ep.extra_window_minutes("dev", 120, True) == 8
    assert ep.alert_for_scale("uat", 300, False) == 3
    assert ep.alert_for_scale("uat", 900, False) == 3
    assert ep.alert_for_scale("dev", 120, True) == 2             # 600 s / one 5-min cycle
    assert ep.alert_for_scale("dev", 300, False) == 12


def test_dev_non_critical_not_evaluated_by_default(on, monkeypatch):
    assert ep.evaluate_for_alerts("dev", False) is False
    assert ep.evaluate_for_alerts("dev", True) is True
    assert ep.evaluate_for_alerts("uat", False) is True
    assert ep.evaluate_for_alerts("prod", False) is True
    monkeypatch.setenv("POLL_DEV_EVAL_CRITICAL_ONLY", "false")
    assert ep.evaluate_for_alerts("dev", False) is True
    assert ep.evaluate_for_alerts("dev", False, "wafv2") is True


# ── scheduling: simulate a day of ticks ─────────────────────────────────
def _simulate(resources, tier, base, seconds=86400, jitter=0, force=True, state=None):
    """Run select_due on every tick of a day; returns {resource id: [collection times]}."""
    st = state or ep.PollState()
    seen = {r["id"]: [] for r in resources}
    t = 1_700_000_000.0
    end = t + seconds
    n = 0
    while t < end:
        due, _ = ep.select_due(resources, tier, base, t, critical=(tier == "critical"),
                               state_obj=st, force_enabled=force)
        for r in due:
            seen[r["id"]].append(t)
        n += 1
        t += base + (jitter if n % 2 else -jitter)
    return seen


def _res(i, env, rtype="ec2"):
    return {"id": i, "resource_type": rtype, "env_resolved": env}


def test_flag_off_returns_the_same_objects_in_order():
    rs = [_res(1, "uat"), _res(2, "dev"), _res(3, "prod"), _res(4, None)]
    due, skipped = ep.select_due(rs, "standard", 300, 1000.0, force_enabled=False)
    assert due == rs and all(a is b for a, b in zip(due, rs)) and skipped == 0
    assert all("_poll_extra_min" not in r for r in due)


def test_flag_off_through_real_env_reading_is_also_identity():
    rs = [_res(1, "uat")]
    due, skipped = ep.select_due(rs, "standard", 300, 1000.0)       # env flag unset
    assert due == rs and skipped == 0


def test_prod_like_resources_are_collected_every_tick_unannotated(on):
    rs = [_res(i, e) for i, e in enumerate(PROD_LIKE)]
    for tick in range(50):
        due, skipped = ep.select_due(rs, "standard", 300, 1000.0 + 300 * tick, state_obj=ep.PollState())
        assert [r["id"] for r in due] == [r["id"] for r in rs] and skipped == 0
        assert all("_poll_extra_min" not in r for r in due)


def test_uat_standard_tier_collects_a_third_as_often(on):
    seen = _simulate([_res(1, "uat"), _res(2, "prod")], "standard", 300)
    assert len(seen[2]) == 288                                       # prod: every tick
    assert 94 <= len(seen[1]) <= 98                                  # uat: 288 / 3
    gaps = [b - a for a, b in zip(seen[1], seen[1][1:])]
    assert max(gaps) <= 900 + 300 and min(gaps) >= 600               # a tick of slack either side


def test_uat_cadence_survives_tick_jitter(on):
    seen = _simulate([_res(1, "uat")], "standard", 300, jitter=60)
    assert 90 <= len(seen[1]) <= 100


def test_uat_critical_tier_is_never_slowed(on):
    seen = _simulate([_res(1, "uat")], "critical", 120)
    assert len(seen[1]) == 720


def test_dev_standard_tier_is_hourly(on):
    seen = _simulate([_res(1, "dev")], "standard", 300)
    assert 22 <= len(seen[1]) <= 26


def test_dev_non_critical_off_mode_collects_nothing(on, monkeypatch):
    monkeypatch.setenv("POLL_DEV_NONCRITICAL_MODE", "off")
    seen = _simulate([_res(1, "dev")], "standard", 300, seconds=7200)
    assert seen[1] == []


def test_dev_critical_tier_every_ten_minutes(on):
    seen = _simulate([_res(1, "dev")], "critical", 120)
    assert 140 <= len(seen[1]) <= 148                                # 144 per day


def test_first_collection_is_immediate_and_covers_the_whole_window(on):
    st = ep.PollState()
    due, _ = ep.select_due([_res(1, "uat")], "standard", 300, 1000.0, state_obj=st)
    assert [r["id"] for r in due] == [1]
    assert due[0]["_poll_extra_min"] == 10                           # (900 - 300) s


def test_second_poll_looks_back_over_every_skipped_tick(on):
    st = ep.PollState()
    ep.select_due([_res(5, "uat")], "standard", 300, 0.0, state_obj=st)
    last = st.get((5, "standard", False))
    t = last
    got = None
    for _ in range(10):
        t += 300
        due, _ = ep.select_due([_res(5, "uat")], "standard", 300, t, state_obj=st)
        if due:
            got = (t, due[0]["_poll_extra_min"], t - last)
            break
    assert got, "a uat resource must come due again"
    _t, extra, gap = got
    assert extra * 60 >= gap - 300                                   # window covers gap + base lookback
    assert gap >= 600


def test_missed_runs_widen_the_window_but_stay_bounded(on):
    st = ep.PollState()
    st.set((9, "standard", False), 0.0)
    due, _ = ep.select_due([_res(9, "uat")], "standard", 300, 100_000.0, state_obj=st)
    assert due and due[0]["_poll_extra_min"] <= math.ceil((2 * 900 - 300) / 60.0)


def test_reclassification_changes_cadence_on_the_next_tick(on):
    st = ep.PollState()
    t = 1_700_000_000.0
    r = _res(7, "uat")
    ep.select_due([r], "standard", 300, t, state_obj=st)               # collected, state recorded
    due, skipped = ep.select_due([r], "standard", 300, t + 300, state_obj=st)
    assert due == [] and skipped == 1                                  # uat: skipped
    r2 = _res(7, "prod")                                               # an operator reclassifies it
    due, skipped = ep.select_due([r2], "standard", 300, t + 600, state_obj=st)
    assert [x["id"] for x in due] == [7] and skipped == 0              # prod: next tick, no restart
    r3 = _res(7, "dev")
    due, _ = ep.select_due([r3], "standard", 300, t + 900, state_obj=st)
    assert due == []                                                   # and back down again


def test_restart_makes_every_slowed_resource_due_once(on):
    st = ep.PollState()
    rs = [_res(i, "uat") for i in range(6)]
    ep.select_due(rs, "standard", 300, 0.0, state_obj=st)
    st.clear()                                                         # process restart
    due, _ = ep.select_due(rs, "standard", 300, 300.0, state_obj=st)
    assert len(due) == 6


def test_slowed_fleet_is_staggered_not_bunched(on):
    rs = [_res(i, "uat") for i in range(300)]
    seen = _simulate(rs, "standard", 300, seconds=3600)
    per_tick = {}
    for times in seen.values():
        for t in times:
            per_tick[t] = per_tick.get(t, 0) + 1
    # after the first tick the load is spread over the 3-tick cycle, never all on one tick
    later = [n for t, n in sorted(per_tick.items())[3:]]
    assert max(later) < 300 and min(later) > 0


def test_state_is_per_tier_and_per_criticality(on):
    st = ep.PollState()
    ep.select_due([_res(1, "uat")], "standard", 300, 0.0, state_obj=st)
    due, _ = ep.select_due([_res(1, "uat")], "low", 900, 100.0, state_obj=st)
    assert due, "another tier has its own clock"


def test_describe_and_unmatched_resource_types_do_not_break(on):
    rs = [{"id": 1, "resource_type": None, "env_resolved": "uat"}, {"id": 2}]
    due, _ = ep.select_due(rs, "standard", 300, 0.0, state_obj=ep.PollState())
    assert len(due) == 2


def test_round_extra_groups_lookbacks_into_five_minute_steps():
    assert ep.round_extra(0) == 0 and ep.round_extra(None) == 0
    assert ep.round_extra(1) == 5 and ep.round_extra(10) == 10 and ep.round_extra(11) == 15
    assert ep.round_extra(-3) == 0


def test_is_due_tolerance():
    assert ep.is_due(900, 300, None, 0)
    assert ep.is_due(900, 300, 0.0, 750.0)                             # 900 - 150 s tolerance
    assert not ep.is_due(900, 300, 0.0, 700.0)
    assert ep.is_due(300, 300, 0.0, 1.0)                               # unchanged cadence: always
    assert not ep.is_due(None, 300, 0.0, 10_000.0)


def test_polling_model_base_interval_lookup():
    assert pm.base_interval_seconds("aws", "rds", "cpuutilization") == 120
    assert pm.base_interval_seconds("aws", "ec2", "cpuutilization") == 300
    assert pm.base_interval_seconds("aws", "ebs", "volumereadops") == 900
    assert pm.base_interval_seconds("aws", "ec2", "mem_used_percent") == 300
    assert pm.base_interval_seconds("aws", "ec2", "disk_used_percent__data") == 900
    assert pm.base_interval_seconds("aws", "s3", "bucketsizebytes") == 86400
    assert pm.base_interval_seconds("aws", "ec2", "statuscheckfailed") is None


# ── runner gate (stubbed boto/DB) ───────────────────────────────────────
def _load_runner():
    install_stub("app.db", get_connection=lambda: _FakeConn())
    install_stub("app.aws.sts", get_boto3_session=lambda *a, **k: _FakeSession())
    install_stub("app.aws.boto_config", STANDARD_RETRY={})
    install_stub("app.collector.metrics_writer",
                 write_metrics_batch=lambda rows: None,
                 write_metric_history_batch=lambda rows, **kw: None)
    install_stub("app.collector.disk_mounts",
                 all_cwagent_disk_dims=lambda *a, **k: {},
                 ensure_disk_mount_metric_registered=lambda *a, **k: None)
    install_stub("app.collector.api_usage", record=lambda *a, **k: None)
    real_pm = load_module("app/collector/polling_model.py")
    real_ep = load_module("app/collector/env_polling.py")
    sys.modules["app.collector.polling_model"] = real_pm
    sys.modules["app.collector.env_polling"] = real_ep
    setattr(sys.modules["app.collector"], "polling_model", real_pm)
    setattr(sys.modules["app.collector"], "env_polling", real_ep)
    return load_module("app/collector/metrics/runner.py"), real_ep


class _FakeCur:
    def execute(self, *a, **k): pass
    def close(self): pass
    def fetchall(self): return []


class _FakeConn:
    def cursor(self, *a, **k): return _FakeCur()
    def commit(self): pass
    def close(self): pass


class _FakeSession:
    def client(self, *a, **k):
        return object()


ACCOUNT = {"id": 1, "account_name": "t", "default_region": "ap-south-1"}


def _fleet():
    out = []
    i = 0
    for env in ("prod", "uat", "dev", None, "unknown", "ignore"):
        for rt in ("ec2", "rds", "ebs"):
            i += 1
            out.append({"id": i, "resource_id": f"{rt}-{i}", "resource_type": rt,
                        "name": f"n{i}", "region": "ap-south-1", "tags": "{}", "env_resolved": env})
    return out


def _run_ticks(monkeypatch, tier, ticks, flag, fleet=None, base_now=1_700_000_000.0):
    runner, real_ep = _load_runner()
    fleet = fleet or _fleet()
    calls = []
    clock = [base_now]

    def grouped_for(account_id, t):
        g = {}
        for r in fleet:
            row = dict(r)
            if not flag:
                row.pop("env_resolved")                    # flag off never selects the column
            g.setdefault((row["resource_type"], row["region"]), []).append(row)
        return g

    orig_gate = runner._env_gate
    monkeypatch.setattr(runner, "_get_resources_for_account", grouped_for)
    monkeypatch.setattr(runner, "_env_gate", lambda g, t: orig_gate(g, t, now=clock[0]))
    monkeypatch.setattr(runner, "_collect_core",
                        lambda cw, rs, rt, t, extra=0: calls.append(("core", rt, t, extra, tuple(sorted(x["id"] for x in rs)))))
    monkeypatch.setattr(runner, "_collect_ec2_cwagent_mem",
                        lambda cw, rs, extra=0: calls.append(("mem", extra, tuple(sorted(x["id"] for x in rs)))))
    monkeypatch.setattr(runner, "_collect_ec2_cwagent_disk",
                        lambda cw, rs, aid, extra=0: calls.append(("disk", extra, tuple(sorted(x["id"] for x in rs)))))
    if flag:
        monkeypatch.setenv("ENV_POLLING_ENABLED", "true")
        monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    real_ep.state().clear()
    per_tick = []
    base = runner.polling_model.TIER_SECONDS[tier]
    for k in range(ticks):
        calls.clear()
        clock[0] = base_now + k * base
        runner._collect_account(ACCOUNT, tier)
        per_tick.append(list(calls))
    return per_tick, fleet


def _collected_ids(per_tick):
    return [sorted({i for c in tick for i in (c[-1] if c[0] != "core" else c[4])}) for tick in per_tick]


@pytest.mark.parametrize("tier", ["critical", "standard", "low"])
def test_runner_flag_off_collects_every_resource_on_every_tick(monkeypatch, tier):
    per_tick, fleet = _run_ticks(monkeypatch, tier, 6, flag=False)
    core_types = {"ec2", "ebs", "rds"}
    for tick in per_tick:
        ids = {i for c in tick if c[0] == "core" for i in c[4]}
        assert ids == {r["id"] for r in fleet if r["resource_type"] in core_types}
        assert all(c[3] == 0 for c in tick if c[0] == "core")        # no extra look-back


def test_runner_flag_off_and_flag_on_agree_for_prod_like_resources(monkeypatch):
    off, fleet = _run_ticks(monkeypatch, "standard", 8, flag=False)
    on_, _ = _run_ticks(monkeypatch, "standard", 8, flag=True)
    prod_like = {r["id"] for r in fleet if r["env_resolved"] in (None, "prod", "unknown", "ignore")}
    for t_off, t_on in zip(off, on_):
        ids_off = {i for c in t_off if c[0] == "core" for i in c[4]} & prod_like
        ids_on = {i for c in t_on if c[0] == "core" for i in c[4]} & prod_like
        assert ids_on == ids_off and ids_on
        # prod-like resources are only ever in a group with no extra look-back
        for c in t_on:
            if c[0] == "core" and set(c[4]) & prod_like:
                assert c[3] == 0 and not (set(c[4]) - prod_like)


def test_runner_flag_on_slows_uat_and_dev_only(monkeypatch):
    per_tick, fleet = _run_ticks(monkeypatch, "standard", 12, flag=True)
    by_env = {}
    for r in fleet:
        by_env.setdefault(r["env_resolved"], set()).add(r["id"])
    counts = {}
    for tick in per_tick:
        got = {i for c in tick if c[0] == "core" for i in c[4]}
        for env, ids in by_env.items():
            counts[env] = counts.get(env, 0) + len(got & ids)
    n = lambda env: len(by_env[env])                                  # noqa: E731
    assert counts["prod"] == 12 * n("prod")
    assert counts[None] == 12 * n(None) and counts["unknown"] == 12 * n("unknown") and counts["ignore"] == 12 * n("ignore")
    assert counts["uat"] < 12 * n("uat") * 0.5                       # about one in three
    assert counts["dev"] < 12 * n("dev") * 0.25                      # about one in twelve (+ first tick)


def test_runner_critical_tier_flag_on_never_slows_uat_and_slows_dev(monkeypatch):
    per_tick, fleet = _run_ticks(monkeypatch, "critical", 10, flag=True)
    by_env = {}
    for r in fleet:
        by_env.setdefault(r["env_resolved"], set()).add(r["id"])
    totals = {env: 0 for env in by_env}
    for tick in per_tick:
        got = {i for c in tick if c[0] == "core" for i in c[4]}
        for env, ids in by_env.items():
            totals[env] += len(got & ids)
    assert totals["uat"] == 10 * len(by_env["uat"])                  # P1-class on UAT: unchanged
    assert totals["prod"] == 10 * len(by_env["prod"])
    assert totals["dev"] < 10 * len(by_env["dev"])                   # dev critical: 10 min, not 2


def test_slowed_group_gets_extra_lookback_and_prod_group_does_not(monkeypatch):
    per_tick, fleet = _run_ticks(monkeypatch, "standard", 6, flag=True)
    prod_ids = {r["id"] for r in fleet if r["env_resolved"] == "prod"}
    saw_extra = False
    for tick in per_tick:
        for c in tick:
            if c[0] != "core":
                continue
            if c[3] > 0:
                saw_extra = True
                assert not (set(c[4]) & prod_ids), "a prod resource was put in a widened-window group"
    assert saw_extra


def test_collect_core_widens_the_lookback_by_extra_min(monkeypatch):
    runner, _ = _load_runner()
    seen = []
    monkeypatch.setattr(runner, "_run_gmd", lambda cw, rs, defs, minutes=5, chunk_size=500: seen.append(minutes) or 0)
    res = [{"id": 1, "resource_type": "ec2", "resource_id": "i-1", "region": "x", "tags": "{}"}]
    runner._collect_core(object(), res, "ec2", "standard")
    runner._collect_core(object(), res, "ec2", "standard", 10)
    base = {m.lookback_min for m in runner.CORE_METRICS if m.resource_type == "ec2" and m.tier == "standard"}
    assert set(seen[:len(base)]) == base
    assert set(seen[len(base):]) == {b + 10 for b in base}


def test_resource_query_reads_env_column_only_when_flag_on(monkeypatch):
    runner, _ = _load_runner()
    captured = []

    class Cur(_FakeCur):
        def execute(self, sql, params=None):
            captured.append(sql)

    class Conn(_FakeConn):
        def cursor(self, *a, **k):
            return Cur()

    monkeypatch.setattr(runner, "get_connection", lambda: Conn())
    runner._get_resources_for_account(1, "standard")
    assert "env_resolved" not in captured[-1]
    monkeypatch.setenv("ENV_POLLING_ENABLED", "true")
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    runner._get_resources_for_account(1, "standard")
    assert "env_resolved" in captured[-1]


# ── extended collector ──────────────────────────────────────────────────
def _load_extended():
    runner, real_ep = _load_runner()
    install_stub("app.collector.metrics.runner", _execute_gmd=lambda *a, **k: 0, STALE_RESOURCE_HOURS=48)
    return load_module("app/collector/metrics/extended.py")


def test_extended_groups_are_unchanged_for_prod_and_split_for_uat(on):
    ext = _load_extended()
    st = ext.env_polling.state()
    st.clear()
    # id 3: 3 % 3 == 0 so it has no load-spreading head start (see env_polling.stagger_seconds)
    rs = [{"id": 1, "resource_type": "sqs", "env_resolved": "prod"},
          {"id": 3, "resource_type": "sqs", "env_resolved": "uat"}]
    groups = ext._extended_env_groups(rs, "standard", now=1000.0)
    flat = {r["id"]: extra for extra, rr in groups for r in rr}
    assert flat == {1: 0, 3: 10}
    groups = ext._extended_env_groups(rs, "standard", now=1300.0)
    assert {r["id"] for _e, rr in groups for r in rr} == {1}          # uat skipped on the next tick


def test_extended_hourly_tier_dev_is_not_slower_than_hourly(on):
    ext = _load_extended()
    ext.env_polling.state().clear()
    rs = [{"id": 3, "resource_type": "kms", "env_resolved": "dev"}]
    for k in range(3):
        groups = ext._extended_env_groups(rs, "extended", now=1000.0 + 3600 * k)
        assert [r["id"] for _e, rr in groups for r in rr] == [3]


def test_extended_uat_hourly_tier_every_three_hours(on):
    ext = _load_extended()
    ext.env_polling.state().clear()
    rs = [{"id": 4, "resource_type": "kms", "env_resolved": "uat"}]
    due = [bool(ext._extended_env_groups(rs, "extended", now=1000.0 + 3600 * k)) for k in range(12)]
    assert sum(due) in (4, 5)


def test_extended_wafv2_never_slowed(on):
    ext = _load_extended()
    ext.env_polling.state().clear()
    rs = [{"id": 5, "resource_type": "wafv2", "env_resolved": "dev"}]
    for k in range(5):
        assert ext._extended_env_groups(rs, "standard", now=1000.0 + 300 * k)


# ── alert windows / staleness SQL ───────────────────────────────────────
def _norm(sql):
    return re.sub(r"\s+", " ", sql)


def test_alert_sql_is_byte_identical_with_flag_off(monkeypatch):
    for fn in (alert_rules.eval_window_sql, alert_rules.stale_minutes_sql):
        assert "env_resolved" not in fn("r", "acc", "a.metric_name")
        assert "env_resolved" not in fn("r", "acc")
    assert "env_resolved" not in alert_rules.is_stale_sql()
    assert "env_resolved" not in alert_rules.state_sql()
    assert "env_resolved" not in alert_rules.firing_where()


def test_alert_sql_gains_the_environment_term_only_with_flag_on(on):
    for fn in (alert_rules.eval_window_sql, alert_rules.stale_minutes_sql):
        assert "env_resolved" in fn("r", "acc", "m.metric_name")
        assert "env_resolved" not in fn("r", "acc")                    # no metric -> unchanged helper
    assert "env_resolved" in alert_rules.is_stale_sql()
    # P1 metrics / describe metrics / non-AWS / waf: constant zero branches exist
    sql = alert_rules.eval_window_sql("r", "acc", "m.metric_name")
    assert "acc.provider <> 'aws' THEN 0" in sql
    assert "'wafv2'" in sql
    assert "'statuscheckfailed'" in sql


def test_flag_on_sql_keeps_the_original_expression_as_a_prefix(on, monkeypatch):
    on_sql = alert_rules.eval_window_sql("r", "acc", "m.metric_name")
    monkeypatch.delenv("ENV_POLLING_ENABLED")
    off_sql = alert_rules.eval_window_sql("r", "acc", "m.metric_name")
    assert _norm(on_sql).startswith("(" + _norm(off_sql) + " + ")


def test_sql_builder_matches_python_extra_minutes_for_every_metric_key(on, monkeypatch):
    """The SQL constants and env_polling.extra_window_minutes() must agree for every scheduled
    metric: build the expression for one key by evaluating the generated SQL in Python is not
    possible, so check the constants it embeds against the Python function."""
    sql = alert_rules.eval_window_sql("r", "acc", "m.metric_name")
    expected = set()
    p1 = pm.p1_metric_keys()
    for (prov, rt, name), secs in pm.metric_interval_overrides().items():
        if prov != "aws" or secs >= 86400:
            continue
        crit = (rt, name) in p1
        u = ep.extra_window_minutes("uat", secs, crit, rt)
        d = ep.extra_window_minutes("dev", secs, crit, rt)
        expected.add((u, d))
    for u, d in expected:
        if u or d:
            assert f"WHEN 'uat' THEN {u} WHEN 'dev' THEN {d}" in sql, (u, d)


def test_stale_window_covers_the_slowed_gap(on):
    """For every UAT / DEV slowed metric the staleness limit is at least one effective interval
    plus the provider publication delay, i.e. a resource polled on schedule is never 'stale'."""
    for (prov, rt, name), secs in pm.metric_interval_overrides().items():
        if prov != "aws" or secs >= 86400 or rt == "wafv2":
            continue
        crit = (rt, name) in pm.p1_metric_keys()
        for env in ("uat", "dev"):
            eff = ep.effective_interval(env, secs, crit, rt)
            if eff is None:
                continue
            extra = ep.extra_window_minutes(env, secs, crit, rt)
            stale_min = pm.STALE_MIN_BY_INTERVAL[min(pm.STALE_MIN_BY_INTERVAL, key=lambda k: abs(k - secs))] + extra
            assert stale_min * 60 >= eff, (env, rt, name, eff, stale_min)


# ── evaluator hooks ─────────────────────────────────────────────────────
def _load_evaluator():
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.ws.publisher", publish_alert=lambda **k: None, publish_alert_resolved=lambda **k: None)
    install_stub("app.api.live_data", invalidate_accounts_cache=lambda: None)
    install_stub("app.api.alerts", _invalidate_cache=lambda: None)
    sys.modules["app.collector.env_polling"] = ep
    return load_module("app/collector/alert_evaluator.py")


def _row(env, rtype, metric, provider="aws"):
    return {"env_resolved": env, "resource_type": rtype, "metric_name": metric, "provider": provider}


def test_evaluator_hooks_are_inert_with_flag_off():
    ev = _load_evaluator()
    for env in ("uat", "dev", "prod", None):
        assert ev._env_poll_row_info(_row(env, "ec2", "cpuutilization")) == (True, 1)


def test_evaluator_uat_standard_metric_needs_three_times_the_cycles(on):
    ev = _load_evaluator()
    assert ev._env_poll_row_info(_row("uat", "ec2", "cpuutilization")) == (True, 3)
    assert ev._env_poll_row_info(_row("uat", "ebs", "volumereadops")) == (True, 3)


def test_evaluator_uat_p1_metric_is_unchanged(on):
    ev = _load_evaluator()
    assert ev._env_poll_row_info(_row("uat", "rds", "cpuutilization")) == (True, 1)
    assert ev._env_poll_row_info(_row("uat", "elb", "errors5xx")) == (True, 1)


def test_evaluator_prod_like_never_scaled_or_skipped(on):
    ev = _load_evaluator()
    for env in ("prod", "unknown", "ignore", None):
        for rtype, metric in sorted(pm.p1_metric_keys()) + [("ec2", "cpuutilization"), ("ec2", "mem_used_percent")]:
            assert ev._env_poll_row_info(_row(env, rtype, metric)) == (True, 1)


def test_evaluator_dev_skips_non_critical_and_keeps_critical(on):
    ev = _load_evaluator()
    assert ev._env_poll_row_info(_row("dev", "ec2", "cpuutilization"))[0] is False
    assert ev._env_poll_row_info(_row("dev", "rds", "cpuutilization")) == (True, 2)


def test_evaluator_ignores_non_aws_and_describe_metrics(on):
    ev = _load_evaluator()
    assert ev._env_poll_row_info(_row("dev", "vm", "cpu", provider="azure")) == (True, 1)
    assert ev._env_poll_row_info(_row("uat", "ec2", "statuscheckfailed")) == (True, 1)
    assert ev._env_poll_row_info(_row("dev", "ec2", "statuscheckfailed")) == (True, 1)


# ── metric_meta (the UI label) ──────────────────────────────────────────
def _load_meta():
    install_stub("app.db", get_db_cursor=lambda **k: None)
    return load_module("app/metric_meta.py")


def test_meta_poll_view_is_empty_for_prod_and_with_flag_off(on):
    mm = _load_meta()
    for env in ("prod", "unknown", None, "ignore"):
        assert mm._env_poll_view(env, "ec2", "cpuutilization", 300) == {}
    assert mm._env_poll_view("uat", "rds", "cpuutilization", 120) == {}     # P1 on uat: unchanged
    assert mm._env_poll_view("uat", "ec2", "statuscheckfailed", 60) == {}   # free Describe metric


def test_meta_poll_view_for_uat_and_dev(on):
    mm = _load_meta()
    v = mm._env_poll_view("uat", "ec2", "cpuutilization", 300)
    assert v["poll_seconds"] == 900 and v["poll_label"] == "15 min" and v["poll_base_label"] == "5 min"
    assert v["poll_slowed"] and v["poll_env"] == "uat" and v["last_collected_ts"] is None
    assert v["stale_after_seconds"] == (20 + 10) * 60
    d = mm._env_poll_view("dev", "ec2", "cpuutilization", 300)
    assert d["poll_label"] == "1 hr" and d["poll_env"] == "dev"


def test_meta_poll_view_not_collected(on, monkeypatch):
    monkeypatch.setenv("POLL_DEV_NONCRITICAL_MODE", "off")
    mm = _load_meta()
    v = mm._env_poll_view("dev", "ec2", "cpuutilization", 300)
    assert v["poll_not_collected"] is True and v["poll_label"] is None and v["poll_seconds"] is None


def test_meta_poll_view_flag_off_is_empty():
    mm = _load_meta()
    assert mm._env_poll_view("uat", "ec2", "cpuutilization", 300) == {}


# ── frontend label ──────────────────────────────────────────────────────
def test_chart_card_shows_the_effective_interval_and_the_prod_one():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "frontend/src/components/MetricChartCard.jsx"), encoding="utf-8").read()
    assert "poll_base_label" in src and "poll_not_collected" in src and "poll_slowed" in src
    assert "polled every {meta.poll_label}{slowedNote}" in src
