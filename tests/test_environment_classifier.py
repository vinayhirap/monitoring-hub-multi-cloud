# tests/test_environment_classifier.py
"""
Phase 4a: environment resolution (app/environment_classifier.py).

The module is pure, so these tests load it straight off disk with no stubs.
The name cases are the point: a token must equal a keyword, never merely
contain it ("product", "reproduce" are not prod).
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module

ec = load_module("app/environment_classifier.py")
resolve = ec.resolve_environment


def _name(n, **kw):
    return resolve(name=n, **kw)


# -- names that must NOT match -------------------------------------------------

def test_substring_lookalikes_are_not_prod():
    for n in ("product-service", "Products-DB", "reproduce-bug", "produce-api",
              "improduction-tools", "byproduct-etl", "prodigy-web", "reproducible-builds"):
        r = _name(n)
        assert r.env == "prod" and r.source == "unknown", n     # unplaced = PROD (fail-safe), waits for a person


def test_substring_lookalikes_are_not_other_tiers():
    for n in ("latest-build", "contest-app", "attestation-svc", "protest-api",
              "devops-tools", "developer-portal", "device-registry", "qatar-edge",
              "stagecoach", "upstage-api", "sandboxed-runner", "testimony-app"):
        r = _name(n)
        assert (r.env, r.source) == ("prod", "unknown"), n


def test_opaque_ids_never_match():
    for rid in ("i-0abc123def4567890", "vol-0dev1234abcd", "sg-0prod99", "db-ABCDEFGHIJ"):
        r = resolve(resource_id=rid)
        assert (r.env, r.source) == ("prod", "unknown"), rid


# -- names that must match -----------------------------------------------------

def test_prod_tokens():
    for n in ("HCS-PROD-MD-01", "prod-api", "api_prod", "web.prod.internal",
              "production-db", "orders-prd-1", "app/prod-alb/abc123", "PROD"):
        r = _name(n)
        assert (r.env, r.source) == ("prod", "name"), n


def test_staging_and_qa_tokens_are_nonprod():
    for n in ("uat-web", "web-UAT-02", "api-staging", "staging.db", "svc-stage",
              "qa-runner", "QA_DB", "preprod-api", "pre-prod-api", "pre_prod_cache",
              "PreProd-Web", "pre-production-db"):
        r = _name(n)
        assert (r.env, r.source) == ("nonprod", "name"), n


def test_dev_and_test_tokens_are_nonprod():
    for n in ("dev-api", "api-dev", "development-db", "test-runner", "load_test_node",
              "sandbox-vpc", "my-dev-box"):
        r = _name(n)
        assert (r.env, r.source) == ("nonprod", "name"), n


def test_trailing_digits_are_part_of_the_boundary():
    assert _name("prod01").env == "prod"
    assert _name("web-prod2").env == "prod"
    assert _name("dev3-api").env == "nonprod"
    assert _name("qa10").env == "nonprod"


def test_camel_case_boundaries():
    assert _name("OrdersProdDb").env == "prod"
    assert _name("myDevServer").env == "nonprod"
    assert _name("StagingCache").env == "nonprod"
    # glued with no boundary: deliberately not matched (unplaced -> PROD / unknown)
    for n in ("prodapi", "Reproduce", "ProductCatalog"):
        assert (_name(n).env, _name(n).source) == ("prod", "unknown"), n


def test_case_insensitive():
    assert _name("Prod-Api").env == "prod"
    assert _name("DEV-API").env == "nonprod"
    assert _name("STAGING-db").env == "nonprod"
    assert _name("Staging-DB").env == "nonprod"


# -- pre-prod / non-prod phrases -----------------------------------------------

def test_preprod_is_nonprod_not_prod():
    for n in ("pre-prod", "pre_prod-db", "pre prod web", "app.pre.prod", "preprod2"):
        assert (_name(n).env, _name(n).source) == ("nonprod", "name"), n


def test_nonprod_phrase_is_nonprod_not_prod():
    for n in ("non-prod-db", "nonprod-api", "non_production", "NonProd-cache"):
        r = _name(n)
        assert (r.env, r.source) == ("nonprod", "name"), n
    # a dev token next to it agrees (same tier), it is not a conflict
    assert (_name("non-prod-dev-box").env, _name("non-prod-dev-box").source) == ("nonprod", "name")
    # but a real prod token next to it is a conflict
    r = _name("non-prod-to-prod-sync")
    assert (r.env, r.source) == ("prod", "unknown") and "conflicting" in r.reason


# -- conflicts -----------------------------------------------------------------

def test_conflicting_tokens_are_unknown_not_guessed():
    # the env is the fail-safe PROD, the source says nobody actually decided
    for n in ("prod-to-dev-sync", "dev-prod-replica", "uat-and-prod"):
        r = _name(n)
        assert r.env == "prod" and r.source == "unknown", n
        assert "conflicting" in r.reason


def test_conflicting_name_does_not_fall_through_to_account_default():
    r = _name("prod-to-dev-sync", account_default="nonprod")
    assert (r.env, r.source) == ("prod", "unknown")


def test_same_tier_twice_is_not_a_conflict():
    assert _name("prod-prod-01").env == "prod"
    assert _name("dev-test-box").env == "nonprod"      # dev + test are both non-prod
    assert _name("uat-qa-box").env == "nonprod"        # uat + qa too: one tier, no conflict


def test_name_conflict_but_clean_id_uses_id():
    r = resolve(name="prod-to-dev", resource_id="uat-bucket")
    assert (r.env, r.source) == ("nonprod", "name")


# -- tags ----------------------------------------------------------------------

def test_tag_keys_and_values():
    for tags, want in (
        ({"Environment": "Production"}, "prod"),
        ({"environment": "prod"}, "prod"),
        ({"Env": "UAT"}, "nonprod"),
        ({"env": "staging"}, "nonprod"),
        ({"Stage": "dev"}, "nonprod"),
        ({"ENVIRONMENT": "Sandbox"}, "nonprod"),
        ({"Environment": "nonprod"}, "nonprod"),
        ({"Environment": "Non-Prod"}, "nonprod"),
        ({"deployment-environment": "qa"}, "nonprod"),
        ({"App.Env": "prd"}, None),            # "app.env" folds to app_env -> a listed key
    ):
        r = resolve(tags=tags)
        if want is None:
            assert r.env == "prod" and r.source == "tag", tags
        else:
            assert (r.env, r.source) == (want, "tag"), tags


def test_tag_json_string_and_garbage():
    assert resolve(tags='{"Environment": "dev"}').env == "nonprod"
    for bad in (None, "", "null", "[]", "not json", 42, '{"Environment": null}'):
        r = resolve(tags=bad)
        assert (r.env, r.source) == ("prod", "unknown"), bad


def test_tag_value_that_means_nothing_falls_through_to_name():
    r = resolve(tags={"Environment": "blue"}, name="prod-web")
    assert (r.env, r.source) == ("prod", "name")


def test_tag_beats_name_and_account():
    r = resolve(tags={"Environment": "dev"}, name="prod-web", account_default="prod")
    assert (r.env, r.source) == ("nonprod", "tag")


def test_tag_key_priority_environment_before_env():
    r = resolve(tags={"Env": "dev", "Environment": "prod"})
    assert r.env == "prod"


def test_non_env_tag_keys_are_not_used():
    # "Tier" usually means web/db, so it is not an environment key
    assert resolve(tags={"Tier": "prod"}).source == "unknown"
    assert resolve(tags={"Project": "prod-migration"}).source == "unknown"
    assert resolve(tags={"Tier": "dev"}).env == "prod"          # not read -> fail-safe PROD


def test_internal_underscore_tags_do_not_confuse_it():
    r = resolve(tags={"_instance_type": "t3.micro", "Name": "x"})
    assert (r.env, r.source) == ("prod", "unknown")


# -- manual / account / unknown ------------------------------------------------

def test_manual_override_beats_everything():
    r = resolve(override="nonprod", tags={"Environment": "prod"}, name="prod-x",
                account_default="prod")
    assert (r.env, r.source) == ("nonprod", "manual")
    r = resolve(override="prod", tags={"Environment": "dev"}, name="dev-x", account_default="nonprod")
    assert (r.env, r.source) == ("prod", "manual")


def test_legacy_manual_override_values_are_still_honoured():
    # stored before the two-environment model: uat / dev mean non-prod, the old "ignore" is a manual PROD
    for legacy in ("uat", "dev", "UAT", " Dev "):
        r = resolve(override=legacy, tags={"Environment": "prod"})
        assert (r.env, r.source) == ("nonprod", "manual"), legacy
    r = resolve(override="ignore", tags={"Environment": "dev"})
    assert (r.env, r.source) == ("prod", "manual")


def test_bad_override_value_is_not_honoured():
    assert resolve(override="bogus", tags={"Environment": "prod"}).env == "prod"
    r = resolve(override="bogus", tags={"Environment": "dev"})
    assert (r.env, r.source) == ("nonprod", "tag")
    r = resolve(override="unknown", name="dev-x")
    assert (r.env, r.source) == ("nonprod", "name")
    assert resolve(override="", name="dev-x").env == "nonprod"


def test_account_default_only_after_tag_and_name():
    assert resolve(account_default="nonprod").env == "nonprod"
    assert resolve(account_default="nonprod").source == "account"
    assert resolve(account_default="prod").source == "account"
    assert resolve(account_default="NONPROD", name="plain-db").env == "nonprod"
    # a pre-migration default of uat / dev still means non-prod
    assert (resolve(account_default="uat").env, resolve(account_default="uat").source) == ("nonprod", "account")
    assert resolve(account_default="dev").env == "nonprod"
    for junk in ("bogus", "unknown", "ignore"):
        r = resolve(account_default=junk)
        assert (r.env, r.source) == ("prod", "unknown"), junk


def test_unknown_when_nothing_matches():
    r = resolve(name="orders-db", resource_id="db-1", tags={"Owner": "x"})
    assert (r.env, r.source) == ("prod", "unknown")


def test_reason_is_bounded():
    r = resolve(name="prod-" + "x" * 500)
    assert len(r.reason) <= ec.REASON_MAX


# -- alerting semantics --------------------------------------------------------

def test_unplaced_and_legacy_values_are_prod_like():
    assert ec.is_prod_like("prod") and ec.is_prod_like("unknown") and ec.is_prod_like("ignore")
    assert ec.is_prod_like(None) and ec.is_prod_like("") and ec.is_prod_like("bogus")
    assert not ec.is_prod_like("nonprod")
    assert not ec.is_prod_like("uat") and not ec.is_prod_like("dev")      # legacy stored values


def test_severity_for():
    assert ec.severity_for("nonprod", True) == "CRITICAL"
    assert ec.severity_for("prod", False) == "WARNING"
    assert ec.severity_for("unknown", False) == "WARNING"     # never silently weaker than prod
    assert ec.severity_for("ignore", False) == "WARNING"
    assert ec.severity_for(None, False) == "WARNING"
    assert ec.severity_for("nonprod", False) == "INFO"
    assert ec.severity_for("uat", False) == "INFO"            # legacy value
    assert ec.severity_for("dev", False) == "INFO"


def test_normalize_tier():
    assert ec.normalize_tier("Production") == "prod"
    assert ec.normalize_tier("pre-prod") == "nonprod"
    assert ec.normalize_tier("non-prod") == "nonprod"
    assert ec.normalize_tier("uat") == "nonprod"
    assert ec.normalize_tier("prod/dev") is None
    assert ec.normalize_tier(None) is None


def test_flag_default_off_and_on(monkeypatch):
    monkeypatch.delenv("ENV_AWARE_CLASSIFICATION", raising=False)
    assert ec.flag_enabled() is False
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    assert ec.flag_enabled() is True
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "no")
    assert ec.flag_enabled() is False


# -- related (attached-to) inheritance -------------------------------------------

R = ec.Resolution


def _parent(env, source="manual", label="ec2 i-0abc"):
    return (R(env, source, "x"), label)


def test_unnamed_child_follows_its_parent():
    r = resolve(name="data-vol", related=[_parent("prod", "name", "ec2 HCS-PROD-MD-01")])
    assert (r.env, r.source) == ("prod", "related")
    assert "ec2 HCS-PROD-MD-01" in r.reason and "by name" in r.reason


def test_childs_own_tag_and_name_beat_the_parent():
    assert resolve(tags={"Environment": "dev"}, related=[_parent("prod")]).env == "nonprod"
    r = resolve(name="uat-vol", related=[_parent("prod")])
    assert (r.env, r.source) == ("nonprod", "name")


def test_manual_override_beats_the_parent():
    assert resolve(override="nonprod", related=[_parent("prod")]).env == "nonprod"


def test_parent_beats_account_default():
    r = resolve(name="vol", account_default="prod", related=[_parent("nonprod", "tag")])
    assert (r.env, r.source) == ("nonprod", "related")


def test_conflicting_child_name_is_settled_by_a_trusted_parent():
    r = resolve(name="TEST-U4RAD-PROD-ORTHANC", related=[_parent("nonprod", "manual")])
    assert (r.env, r.source) == ("nonprod", "related")


def test_conflicting_child_name_without_a_parent_stays_unknown_even_with_account_default():
    r = resolve(name="TEST-U4RAD-PROD-ORTHANC", account_default="prod")
    assert (r.env, r.source) == ("prod", "unknown")
    r = resolve(name="TEST-U4RAD-PROD-ORTHANC", account_default="prod",
                related=[_parent("prod", "account"), _parent("prod", "unknown")])
    assert (r.env, r.source) == ("prod", "unknown")


def test_only_evidence_based_parents_are_inherited_from():
    for src in ("account", "unknown", "related"):
        r = resolve(name="vol", related=[_parent("prod", src)])
        assert (r.env, r.source) == ("prod", "unknown"), src
    r = resolve(name="vol", related=[_parent("nonprod", "account")])
    assert (r.env, r.source) == ("prod", "unknown")         # a non-prod parent is not inherited either unless evidence-based
    # a legacy / unplaceable parent env is not a tier, so it lends nothing
    assert resolve(name="vol", related=[_parent("ignore", "manual")]).source == "unknown"


def test_parents_that_disagree_give_unknown_not_a_guess():
    r = resolve(name="vol", account_default="prod",
                related=[_parent("prod", "tag", "ec2 a"), _parent("nonprod", "tag", "ec2 b")])
    assert (r.env, r.source) == ("prod", "unknown") and "disagree" in r.reason


def test_two_parents_that_agree_inherit():
    r = resolve(name="vol", related=[_parent("nonprod", "tag"), _parent("nonprod", "name")])
    assert (r.env, r.source) == ("nonprod", "related")


def test_related_is_a_known_source():
    assert "related" in ec.SOURCES


# -- tag compliance ----------------------------------------------------------------

def test_tag_state_ok_needs_the_exact_standard_key_and_value():
    assert ec.tag_state("ec2", {"Environment": "prod"}) == "ok"
    assert ec.tag_state("ec2", {"Environment": "nonprod", "Owner": "x"}) == "ok"
    assert ec.tag_state("ec2", '{"Environment": "nonprod"}') == "ok"


def test_tag_state_nonstandard_is_understood_but_not_to_standard():
    for tags in ({"Env": "prod"}, {"environment": "prod"}, {"Environment": "Production"},
                 {"Environment": "staging"}, {"Stage": "dev"}, {"Environment": "prod "},
                 {"Environment": "uat"}, {"Environment": "dev"}, {"Environment": "non-prod"},
                 {"Environment": "Non-Prod"}):
        assert ec.tag_state("ec2", tags) == "nonstandard", tags


def test_tag_state_invalid_is_present_but_meaningless_or_ambiguous():
    for tags in ({"Environment": "blue"}, {"Environment": "prod/dev"}, {"Environment": ""},
                 {"Environment": "unknown"}, {"Environment": "ignore"}):
        assert ec.tag_state("ec2", tags) == "invalid", tags


def test_tag_state_missing_when_no_environment_style_key():
    assert ec.tag_state("ec2", {}) == "missing"
    assert ec.tag_state("rds", {"Name": "x", "Owner": "y", "_instance_type": "t3"}) == "missing"
    assert ec.tag_state("ec2", None) == "missing"
    assert ec.tag_state("ec2", {"Tier": "prod"}) == "missing"


def test_tag_state_is_not_checked_when_the_stored_tags_are_not_the_resources_own():
    for rtype in ("s3", "logs", "kms", "lambda", "elb", "sqs", "natgateway", "certificatemanager"):
        assert ec.tag_state(rtype, {}) == "not_checked", rtype
    assert ec.tag_state("s3", {"Environment": "prod"}) == "not_checked"   # not even when present


def test_attached_ebs_volume_carries_its_instances_tags_so_it_is_not_checked():
    assert ec.tag_state("ebs", {"parent_ec2": "i-1"}) == "not_checked"
    assert ec.tag_state("ebs", {"Environment": "prod", "parent_ec2": "i-1"}) == "not_checked"
    assert ec.tag_state("ebs", {"_ebs_state": "available"}) == "missing"          # unattached: own tags
    assert ec.tag_state("ebs", {"Environment": "nonprod"}) == "ok"


def test_missing_tags_lists_absent_mandatory_keys_ignoring_case():
    got = ec.missing_tags({"environment": "prod", "OWNER": "t", "Application": "a"})
    assert got == ["CostCenter", "Criticality", "DataClassification", "ManagedBy"]
    assert ec.missing_tags({}) == list(ec.MANDATORY_TAGS)
    assert ec.missing_tags(None) == list(ec.MANDATORY_TAGS)


def test_tag_environment_reads_only_the_tag():
    assert ec.tag_environment({"Env": "Production"}) == "prod"
    assert ec.tag_environment({"Env": "uat"}) == "nonprod"
    assert ec.tag_environment({"Environment": "blue"}) is None
    assert ec.tag_environment({}) is None


# --- "DevOps" is a discipline, not the dev tier (a prod build server must not be labelled dev) ---------------------
import pytest as _pytest


@_pytest.mark.parametrize("name", ["DevOps-Jenkins", "DevOpsRunner", "RunDevOps", "devops", "Dev-Ops",
                                   "DevSecOps-scan", "SecDevOps", "DEVOPS"])
def test_devops_names_are_not_read_as_dev(name):
    r = resolve(name=name)
    assert (r.env, r.source) == ("prod", "unknown")      # unplaced is PROD: still alerted at prod strength


@_pytest.mark.parametrize("name,env", [("prod-DevOps-tools", "prod"), ("DevOps-UAT", "nonprod"),
                                       ("web-dev-01", "nonprod"), ("dev-ops-dev", "nonprod")])
def test_devops_does_not_hide_a_real_tier_token(name, env):
    assert resolve(name=name).env == env


def test_essential_tags_exclude_cost_and_governance_tags():
    assert ec.ESSENTIAL_TAGS == ("Environment", "Criticality", "Owner", "Application")
    assert not {"CostCenter", "DataClassification", "ManagedBy"} & set(ec.ESSENTIAL_TAGS)
    assert ec.missing_tags({"Environment": "prod"}, ec.ESSENTIAL_TAGS) == ["Criticality", "Owner", "Application"]
    assert ec.missing_tags({"environment": "prod", "OWNER": "t", "application": "a", "criticality": "low"},
                           ec.ESSENTIAL_TAGS) == []
