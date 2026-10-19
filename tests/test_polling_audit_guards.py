# tests/test_polling_audit_guards.py
"""
Phase B1 guardrails in scripts/polling_audit.py.

Every scenario runs in a FRESH interpreter (same approach as test_polling_audit.py) because other test files install stub
modules into sys.modules for app.collector / the catalogs, which would make an in-process import of the script read stubs.

Each guardrail gets two kinds of test: the real catalog passes it (or fails exactly the documented KNOWN_OPEN findings),
and a deliberately broken input is CAUGHT, so a guardrail that silently stopped checking would fail here.
"""
import json
import os
import subprocess
import sys
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT = os.path.join(ROOT, "scripts", "polling_audit.py")

PRE = """
import sys, json
sys.path.insert(0, %r)
from scripts import polling_audit as pa
from app.collector import env_polling as real_ep, polling_model as pm

def row(provider="aws", service="svc", metric="Metric", tier="low", interval=900, publish=60, status="repo",
        source="core", static=True, db=None):
    return {"provider": provider, "service": service, "metric": metric, "db_metric": db or metric.lower(), "tier": tier,
            "interval_s": interval, "source": source, "evaluated_every_2min": False, "static_only": static,
            "publish_s": publish, "publish_status": status}

class EP:
    # the real env_polling with selected functions replaced, to prove a broken policy is caught
    def __init__(self, **over):
        self._over = over
    def __getattr__(self, name):
        return self._over.get(name, getattr(real_ep, name))

def msgs(problems):
    return [p.rule + ": " + p.msg for p in problems]
""" % ROOT


def run(code):
    r = subprocess.run([sys.executable, "-c", PRE + textwrap.dedent(code)], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def cli(*args):
    return subprocess.run([sys.executable, AUDIT, *args], capture_output=True, text=True, cwd=ROOT)


# ── the real catalog ────────────────────────────────────────────────────────────────────────────────────────

def test_real_catalog_only_documented_findings_fail():
    out = run("""
        problems, warnings = pa.run_guardrails()
        new, known, gone = pa.split_known(problems)
        print(json.dumps({"new": msgs(new), "known": sorted("/".join(k[1:]) for k in {(q.rule, q.provider, q.service, q.metric) for q in known}),
                          "gone": [list(k) for k in gone], "rules": sorted({q.rule for q in problems})}))
    """)
    assert out["new"] == [], out["new"]
    assert out["gone"] == [], f"KNOWN_OPEN entries that no longer reproduce, remove them: {out['gone']}"
    assert out["known"] == ["azure/storage_account/UsedCapacity", "gcp/gcs_bucket/object_count",
                            "gcp/gcs_bucket/total_byte_seconds", "gcp/gcs_bucket/total_bytes"]
    assert out["rules"] == ["publish_interval"]


def test_cli_check_passes_and_strict_fails_on_known_open():
    ok = cli("--check")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "KNOWN OPEN" in ok.stdout and "0 failing, 4 known open" in ok.stdout
    strict = cli("--check", "--strict")
    assert strict.returncode == 1, strict.stdout
    assert "gcs_bucket/total_bytes" in strict.stdout


def test_every_scheduled_metric_is_classified_and_table_has_no_dead_entry():
    out = run("""
        rows = pa.inventory()
        print(json.dumps({"n": len(rows), "unclassified": [r["metric"] for r in rows if r["publish_s"] is None],
                          "classified": msgs(pa.check_classified(rows)),
                          "bad_status": [r["metric"] for r in rows if r["publish_status"] not in ("verified", "secondary", "repo", "unverified")]}))
    """)
    assert out["n"] > 300
    assert out["unclassified"] == [] and out["classified"] == [] and out["bad_status"] == []


def test_waf_fast_polling_on_changes_tiers_and_still_passes():
    out = run("""
        off = {r["metric"]: r["interval_s"] for r in pa.inventory() if r["service"] == "wafv2"}
        on = {r["metric"]: r["interval_s"] for r in pa.inventory_waf_fast() if r["service"] == "wafv2"}
        after = {r["metric"]: r["interval_s"] for r in pa.inventory() if r["service"] == "wafv2"}
        print(json.dumps({"off": off["BlockedRequests"], "on": on["BlockedRequests"], "on_low": on["RequestsWithValidCaptchaToken"],
                          "restored": after == off}))
    """)
    assert out == {"off": 86400, "on": 300, "on_low": 900, "restored": True}


# ── 1. publish interval ─────────────────────────────────────────────────────────────────────────────────────

def test_publish_interval_rule_catches_polling_faster_than_publish():
    out = run("""
        fail = pa.check_publish_interval([row(interval=120, publish=300, status="repo"), row(interval=900, publish=86400, status="verified")])
        okay = pa.check_publish_interval([row(interval=300, publish=300), row(interval=120, publish=60), row(interval=86400, publish=86400)])
        unv = pa.check_publish_interval([row(interval=120, publish=300, status="unverified")])
        print(json.dumps({"fail": len(fail[0]), "fail_warn": len(fail[1]), "ok": [len(okay[0]), len(okay[1])], "unv": [len(unv[0]), len(unv[1])]}))
    """)
    assert out["fail"] == 2 and out["fail_warn"] == 0
    assert out["ok"] == [0, 0]
    assert out["unv"] == [0, 1], "an unverified entry must warn, never fail"


def test_publish_table_rejects_bad_entries():
    out = run("""
        def raises(table):
            try:
                pa._build_publish(table)
            except ValueError as e:
                return str(e)
            return None
        print(json.dumps({
            "status": raises([("aws", "a", "*", 60, "guess", "x")]),
            "dup": raises([("aws", "a", "*", 60, "repo", "x"), ("aws", "a", "*", 300, "repo", "y")]),
            "nosource": raises([("aws", "a", "*", 60, "repo", " ")]),
            "unv_nosource_ok": raises([("aws", "a", "*", 60, "unverified", "")]),
        }))
    """)
    assert "bad status" in out["status"] and "duplicate" in out["dup"] and "no source" in out["nosource"]
    assert out["unv_nosource_ok"] is None


def test_classified_flags_missing_and_dead_entries():
    out = run("""
        rows = [row(publish=None, status="unclassified"), row(service="live")]
        table = [("aws", "live", "*", 60, "repo", "x"), ("aws", "gone", "*", 60, "repo", "x"), ("aws", "live", "Renamed", 60, "repo", "x")]
        print(json.dumps(msgs(pa.check_classified(rows, table))))
    """)
    assert any("no PUBLISH_TABLE entry" in m for m in out)
    assert sum("dead PUBLISH_TABLE entry" in m for m in out) == 2


# ── 2. daily publishers ─────────────────────────────────────────────────────────────────────────────────────

def test_daily_metric_on_critical_or_standard_tier_is_caught():
    out = run("""
        bad = [row(tier="critical", interval=120, publish=86400), row(tier="standard", interval=300, publish=86400, provider="gcp")]
        good = [row(tier="slow_extended", interval=86400, publish=86400), row(tier="low", interval=900, publish=3600)]
        unv = [row(tier="standard", interval=300, publish=86400, status="unverified")]
        print(json.dumps({"bad": len(pa.check_daily_fast_tier(bad, {})[0]), "good": len(pa.check_daily_fast_tier(good, {})[0]),
                          "unv": [len(x) for x in pa.check_daily_fast_tier(unv, {})]}))
    """)
    assert out == {"bad": 2, "good": 0, "unv": [0, 1]}


def test_period_override_must_agree_with_audit_table():
    out = run("""
        rows = [row(service="s3", metric="BucketSizeBytes", tier="slow_extended", interval=86400, publish=60)]
        p, _ = pa.check_daily_fast_tier(rows, {("s3", "BucketSizeBytes"): 86400, ("s3", "Ghost"): 86400})
        real, _ = pa.check_daily_fast_tier(pa.inventory())
        print(json.dumps({"msgs": msgs(p), "real": msgs(real)}))
    """)
    assert any("one of the two is wrong" in m for m in out["msgs"])
    assert any("not a scheduled metric" in m for m in out["msgs"])
    assert out["real"] == []


# ── 3. P1 set ───────────────────────────────────────────────────────────────────────────────────────────────

def test_p1_set_catches_dangling_slow_and_unevaluated_keys():
    out = run("""
        crit = row(service="rds", metric="CPUUtilization", tier="critical", interval=120, publish=60, db="cpuutilization")
        slow = row(service="slowsvc", metric="X", tier="standard", interval=300, publish=300, db="x")
        pub = row(service="pubslow", metric="Y", tier="critical", interval=120, publish=300, db="y")
        ok, _ = pa.check_p1_set([crit], {("rds", "cpuutilization")})
        dangling, _ = pa.check_p1_set([crit], {("rds", "cpuutilization"), ("rds", "cpuutilisation")})
        polled_slow, _ = pa.check_p1_set([slow], {("slowsvc", "x")})
        publishes_slow, _ = pa.check_p1_set([pub], {("pubslow", "y")})
        unevaluated, _ = pa.check_p1_set([crit], set())
        print(json.dumps({"ok": len(ok), "dangling": msgs(dangling), "polled_slow": msgs(polled_slow),
                          "publishes_slow": msgs(publishes_slow), "unevaluated": msgs(unevaluated)}))
    """)
    assert out["ok"] == 0
    assert any("matches no scheduled metric" in m for m in out["dangling"])
    assert any("only polled every 5m" in m or "only polled every 300s" in m for m in out["polled_slow"])
    assert any("published every 300s" in m for m in out["publishes_slow"])
    assert any("not in the P1 set" in m for m in out["unevaluated"])


# ── 4. env polling invariants (flag ON) ─────────────────────────────────────────────────────────────────────

def test_env_invariants_hold_on_the_real_policy_and_the_real_catalog():
    out = run("""
        rows = pa.inventory()
        print(json.dumps({"policy": msgs(pa.check_env_invariants(rows)), "base": msgs(pa.check_base_interval_agrees(rows))}))
    """)
    assert out["policy"] == [] and out["base"] == []


def test_env_invariants_catch_broken_policies():
    out = run("""
        rows = pa.inventory()
        real = real_ep.effective_interval

        def faster(env, base, crit, rtype=None, force_enabled=None):         # slower than base is the only legal direction
            eff = real(env, base, crit, rtype, force_enabled)
            return None if eff is None else (eff - 1 if real_ep.normalize_env(env) == "uat" and not crit else eff)

        def slows_waf(env, base, crit, rtype=None, force_enabled=None):
            return base * 3 if (rtype == "wafv2" and real_ep.normalize_env(env) == "uat") else real(env, base, crit, rtype, force_enabled)

        def slows_daily(env, base, crit, rtype=None, force_enabled=None):
            return base * 2 if (base >= 86400 and real_ep.normalize_env(env) == "dev") else real(env, base, crit, rtype, force_enabled)

        def slows_uat_critical(env, base, crit, rtype=None, force_enabled=None):
            return base * 3 if (crit and real_ep.normalize_env(env) == "uat" and base < 86400) else real(env, base, crit, rtype, force_enabled)

        def slows_prod(env, base, crit, rtype=None, force_enabled=None):
            return base * 2 if (real_ep.normalize_env(env) == "prod" and base == 300) else real(env, base, crit, rtype, force_enabled)

        def drops_dev(env, base, crit, rtype=None, force_enabled=None):       # None while POLL_DEV_NONCRITICAL_MODE is "slow"
            return None if (real_ep.normalize_env(env) == "dev" and not crit and rtype != "wafv2" and base == 900) else real(env, base, crit, rtype, force_enabled)

        def extra_zero(env, base, crit, rtype=None, force_enabled=None):      # claims no extra window although the interval grew
            return 0

        res = {}
        for name, fn in (("faster", faster), ("waf", slows_waf), ("daily", slows_daily), ("uat_critical", slows_uat_critical),
                         ("prod", slows_prod), ("drops_dev", drops_dev)):
            res[name] = len(pa.check_env_invariants(rows, EP(effective_interval=fn)))
        res["extra_zero"] = len(pa.check_env_invariants(rows, EP(extra_window_minutes=extra_zero)))
        print(json.dumps(res))
    """)
    for name, n in out.items():
        assert n >= 1, f"broken policy {name!r} was not caught"


def test_base_interval_disagreement_is_caught():
    out = run("""
        rows = [row(service="ec2", metric="CPUUtilization", tier="critical", interval=120, db="cpuutilization", source="core")]
        print(json.dumps(msgs(pa.check_base_interval_agrees(rows))))
    """)
    assert out and "base_interval_seconds says 300" in out[0]


# ── 5. staleness ────────────────────────────────────────────────────────────────────────────────────────────

def test_staleness_tables_exceed_every_interval_including_env_slowed():
    out = run("""
        rows = pa.inventory()
        tiny_stale = {120: 1, 300: 1, 900: 1, 3600: 1, 86400: 1}
        tiny_window = {120: 1, 300: 5, 900: 15, 3600: 60, 86400: 1440}
        print(json.dumps({"real": msgs(pa.check_staleness(rows)),
                          "tiny_stale": len(pa.check_staleness(rows, stale=tiny_stale)),
                          "tiny_window": len(pa.check_staleness(rows, window=tiny_window)),
                          "no_extra": len(pa.check_staleness(rows, ep_mod=EP(extra_window_minutes=lambda *a, **k: 0)))}))
    """)
    assert out["real"] == []
    assert out["tiny_stale"] >= 1 and out["tiny_window"] >= 1, "a table equal to the interval must fail"
    assert out["no_extra"] >= 1, "slowed intervals without the extra window must fail"


def test_sql_extra_formula_matches_env_polling():
    out = run("""
        bad = []
        for um in (1, 3, 12):
            for di in (120, 3600, 86400):
                for dci in (120, 600):
                    for base in (120, 300, 900, 3600, 86400):
                        for crit in (True, False):
                            for env in ("uat", "dev", "prod"):
                                with pa.envvars(POLL_UAT_MULTIPLIER=um, POLL_DEV_INTERVAL_SECONDS=di, POLL_DEV_CRITICAL_INTERVAL_SECONDS=dci):
                                    a = real_ep.extra_window_minutes(env, base, crit, None, True)
                                    b = pa._sql_extra_minutes(base, crit, env, real_ep.uat_multiplier(), real_ep.dev_interval_seconds(),
                                                              real_ep.dev_critical_interval_seconds())
                                    if a != b:
                                        bad.append([um, di, dci, base, crit, env, a, b])
        print(json.dumps(bad))
    """)
    assert out == []


# ── GetMetricData Period, expiry ────────────────────────────────────────────────────────────────────────────

def test_period_finer_than_native_publish_is_caught():
    out = run("""
        finer = row(service="efs", metric="StorageBytes", source="extended", publish=900, status="verified")   # extended Period is 300
        coarser = row(service="rds", metric="CPUUtilization", source="core", publish=60, status="repo")        # Period 60 == 60
        unv = row(service="efs", metric="StorageBytes", source="extended", publish=900, status="unverified")
        real = pa.check_period_vs_publish(pa.inventory())
        print(json.dumps({"finer": len(pa.check_period_vs_publish([finer])[0]), "ok": len(pa.check_period_vs_publish([coarser])[0]),
                          "unv": [len(x) for x in pa.check_period_vs_publish([unv])], "real_problems": len(real[0])}))
    """)
    assert out["finer"] == 1 and out["ok"] == 0 and out["unv"] == [0, 1] and out["real_problems"] == 0


def test_expiry_metric_must_be_static_only():
    out = run("""
        bad = pa.check_expiry_static([row(metric="DaysToExpiry", static=False)])
        good = pa.check_expiry_static([row(metric="DaysToExpiry", static=True), row(metric="CPUUtilization", static=False)])
        real = pa.check_expiry_static(pa.inventory())
        print(json.dumps([len(bad), len(good), len(real)]))
    """)
    assert out == [1, 0, 0]


# ── 6. paid or free, cost units ─────────────────────────────────────────────────────────────────────────────

def test_billing_classification_and_units():
    out = run("""
        rows = {(r["provider"], r["service"], r["metric"]): r for r in pa.inventory()}
        g = lambda *k: rows[k]
        print(json.dumps({
            "describe": [g("aws", "ec2", "statuscheckfailed")["billing"], g("aws", "ec2", "statuscheckfailed")["units_per_resource_day"]],
            "derived": [g("aws", "wafv2", "BlockRatio")["billing"], g("aws", "wafv2", "BlockRatio")["units_per_resource_day"]],
            "unsupported": g("aws", "dynamodb", "UserErrors")["billing"],
            "ec2_cpu": [g("aws", "ec2", "CPUUtilization")["billing"], g("aws", "ec2", "CPUUtilization")["units_per_resource_day"]],
            "rds_cpu": g("aws", "rds", "CPUUtilization")["units_per_resource_day"],
            "s3_size": g("aws", "s3", "BucketSizeBytes")["units_per_resource_day"],
            "search": g("aws", "dynamodb", "ThrottledRequests")["cost_note"],
            "azure": [g("azure", "vm", "Percentage CPU")["billing"], g("azure", "vm", "Percentage CPU")["billed_unit"],
                      g("azure", "vm", "Percentage CPU")["units_per_resource_day"]],
            "gcp": [g("gcp", "cloudsql_instance", "cpu/utilization")["billed_unit"], g("gcp", "cloudsql_instance", "cpu/utilization")["units_per_resource_day"]],
            "gate": g("aws", "elb", "RequestCount")["cost_note"],
        }))
    """)
    assert out["describe"] == ["free", 0.0] and out["derived"] == ["free", 0.0] and out["unsupported"] == "free"
    assert out["ec2_cpu"] == ["paid", 288.0]
    assert out["rds_cpu"] == 720.0 and out["s3_size"] == 1.0
    assert "SEARCH" in out["search"]
    assert out["azure"] == ["paid_after_free_tier", "api_calls", 5.76]
    assert out["gcp"] == ["series_returned", 288.0]
    assert "alb" in out["gate"]


def test_report_and_csv_carry_the_cost_columns():
    r = cli("--report", "--counts", '{"ec2": 19}')
    assert r.returncode == 0, r.stderr
    assert "Per metric:" in r.stdout and "units/resource/day" in r.stdout and "19 resources" in r.stdout
    header = open(os.path.join(ROOT, "docs", "polling_inventory.csv")).readline().strip().split(",")
    for col in ("publish_s", "publish_status", "billing", "billed_unit", "units_per_resource_day", "cost_note"):
        assert col in header


def test_tier_map_names_must_exist_in_the_catalog():
    out = run("""
        real = pa.check_tier_maps_in_catalog()
        typo_az = pa.check_tier_maps_in_catalog(az_tiers={("vm", "VM Availabilty Typo"): "critical", ("vm", "Percentage CPU"): "standard"})
        typo_gcp = pa.check_tier_maps_in_catalog(gcp_tiers={("cloudsql_instance", "upp"): "critical"})
        unknown_svc = pa.check_tier_maps_in_catalog(az_tiers={("no_such_service", "x"): "low"})
        stale_aws = pa.check_tier_maps_in_catalog(aws_overrides={("sqs", "ApproximateAgeOfOldestMessage"): "standard", ("sqs", "Renamed"): "low"})
        print(json.dumps({"real": len(real), "az": msgs(typo_az), "gcp": len(typo_gcp), "svc": len(unknown_svc), "aws": msgs(stale_aws)}))
    """)
    assert out["real"] == 0
    assert len(out["az"]) == 1 and "typo or removed" in out["az"][0], out["az"]
    assert out["gcp"] == 1 and out["svc"] == 1
    assert len(out["aws"]) == 1 and "does nothing" in out["aws"][0]
