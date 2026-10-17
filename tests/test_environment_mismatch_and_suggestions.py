# tests/test_environment_mismatch_and_suggestions.py
"""
Workstream C, pure logic: the mismatch detector and the suggestion engine in app/environment_classifier.py.

Both are advisory. The first block proves a warning can never change a resolved environment; the rest uses awkward
names and conflicting evidence on purpose.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module

ec = load_module("app/environment_classifier.py")
Res = ec.Resolution


def warn(**kw):
    kw.setdefault("env", "unknown")
    kw.setdefault("source", "unknown")
    return ec.detect_warning(**kw)


def kind(**kw):
    w = warn(**kw)
    return w[0] if w else None


# ---- mismatch detector --------------------------------------------------------------------------------

def test_tag_and_name_that_disagree_are_flagged():
    w = warn(env="dev", source="tag", tags={"Environment": "dev"}, name="web-prod-01")
    assert w[0] == "tag_vs_name"
    assert "dev" in w[1] and "prod" in w[1] and w[1].startswith("tag_vs_name:")


def test_tag_and_name_that_agree_are_not():
    assert warn(env="prod", source="tag", tags={"Environment": "Production"}, name="orders-prd-2") is None


def test_manual_choice_against_the_tag_is_flagged_even_with_a_nonstandard_tag():
    assert kind(env="dev", source="manual", override="dev", tags={"env": "Production"}) == "manual_vs_tag"


def test_manual_choice_against_the_name_is_flagged_when_there_is_no_tag():
    assert kind(env="uat", source="manual", override="uat", name="HCS-PROD-MD-01") == "manual_vs_name"


def test_manual_choice_that_agrees_with_tag_but_not_name_reports_tag_vs_name():
    assert kind(env="prod", source="manual", override="prod", tags={"Environment": "prod"}, name="svc-dev-1") == "tag_vs_name"


def test_ignore_is_never_a_mismatch():
    assert warn(env="ignore", source="manual", override="ignore", tags={"Environment": "prod"}, name="x-dev-1") is not None  # tag vs name still real
    assert warn(env="ignore", source="manual", override="ignore", tags={"Environment": "prod"}, name="plain") is None


def test_unknown_with_no_second_piece_of_evidence_has_no_warning():
    assert warn(name="mystery-box") is None and warn() is None


def test_a_name_that_contradicts_itself_is_not_evidence_for_a_mismatch():
    # "prod-to-dev-sync" resolves to unknown; it must not be read as "name says prod" against a dev tag
    assert warn(env="dev", source="tag", tags={"Environment": "dev"}, name="prod-to-dev-sync") is None


def test_devops_and_nonprod_names_say_nothing():
    assert warn(env="prod", source="tag", tags={"Environment": "prod"}, name="DevOps-Jenkins") is None
    assert warn(env="prod", source="tag", tags={"Environment": "prod"}, name="non-prod-gateway") is None


def test_lookalike_words_do_not_trigger():
    assert warn(env="prod", source="tag", tags={"Environment": "prod"}, name="product-catalog-latest") is None


def test_preprod_name_reads_as_uat_not_prod():
    assert kind(env="prod", source="tag", tags={"Environment": "prod"}, name="pre-prod-api") == "tag_vs_name"
    assert warn(env="uat", source="tag", tags={"Environment": "uat"}, name="pre-prod-api") is None


def test_attached_volume_that_disagrees_with_a_decided_instance():
    inst = Res("prod", "name", "name token 'prod'")
    w = warn(env="dev", source="name", name="vol-dev-scratch", resource_type="ebs", attached=[(inst, "ec2 HCS-PROD-MD-01")])
    assert w and w[0] == "volume_vs_instance" and "HCS-PROD-MD-01" in w[1] and "prod" in w[1]


def test_volume_following_its_instance_is_not_a_mismatch():
    inst = Res("prod", "name", "x")
    assert warn(env="prod", source="related", resource_type="ebs", attached=[(inst, "ec2 a")]) is None


def test_an_instance_decided_only_by_account_default_is_too_weak_to_contradict_a_volume():
    inst = Res("prod", "account", "account default")
    assert warn(env="dev", source="name", name="x-dev", resource_type="ebs", attached=[(inst, "ec2 a")]) is None


def test_attached_ebs_tag_is_described_as_the_instances_tag():
    tags = {"Environment": "prod", "parent_ec2": "i-1"}
    w = warn(env="prod", source="tag", tags=tags, name="data-dev-vol", resource_type="ebs")
    assert w and "attached instance's tag" in w[1]


def test_message_is_short_and_has_no_glyphs():
    w = warn(env="dev", source="tag", tags={"Environment": "dev"}, name="x" * 400 + "-prod")
    assert w is None or len(w[1]) <= ec.REASON_MAX
    big = warn(env="prod", source="manual", override="prod", tags={"Environment": "dev"})
    assert len(big[1]) <= ec.REASON_MAX and all(ord(ch) < 128 for ch in big[1])


def test_warning_kind_parses_the_stored_text():
    assert ec.warning_kind("tag_vs_name: tag says dev but ...") == "tag_vs_name"
    assert ec.warning_kind("something else: x") is None and ec.warning_kind(None) is None and ec.warning_kind("") is None


def test_a_warning_never_changes_the_resolution():
    """The detector takes the resolution as input and returns text. Resolving with and without it gives the same answer."""
    cases = [
        dict(tags={"Environment": "dev"}, name="web-prod-01"),
        dict(override="uat", tags={"Env": "prod"}, name="x-dev"),
        dict(name="prod-to-dev-sync"),
    ]
    for c in cases:
        before = ec.resolve_environment(**c)
        ec.detect_warning(env=before.env, source=before.source, override=c.get("override"), tags=c.get("tags"), name=c.get("name"))
        assert ec.resolve_environment(**c) == before


# ---- suggestion engine --------------------------------------------------------------------------------

def sib(rid, name, env, source="name", **tags):
    return dict(resource_id=rid, name=name, tags=tags or None, env=env, source=source)


def target(rid="t-1", name="mystery", rtype="ec2", **tags):
    return dict(resource_id=rid, name=name, resource_type=rtype, tags=tags or None)


def test_same_application_tag_with_three_agreeing_siblings_is_high():
    sibs = [sib(f"i-{i}", f"web-{i}", "prod", Application="orders") for i in range(3)]
    s = ec.suggest_environment(target(Application="orders"), sibs)
    assert (s.env, s.confidence, s.basis) == ("prod", "high", "application") and "orders" in s.reason


def test_application_tag_matching_ignores_case_and_key_style():
    sibs = [sib(f"i-{i}", f"web-{i}", "uat", application="Orders-API") for i in range(2)]
    s = ec.suggest_environment(target(**{"Application": "orders-api"}), sibs)
    assert s and s.env == "uat" and s.confidence == "medium"


def test_one_sibling_is_only_low_confidence():
    s = ec.suggest_environment(target(Application="solo"), [sib("i-1", "a-1", "dev", Application="solo")])
    assert s.confidence == "low"


def test_siblings_that_disagree_give_no_suggestion():
    sibs = [sib("i-1", "a", "prod", Application="x"), sib("i-2", "b", "prod", Application="x"), sib("i-3", "c", "uat", Application="x")]
    assert ec.suggest_environment(target(Application="x"), sibs) is None


def test_only_evidence_the_sibling_owns_counts():
    # account default, related and unknown siblings are not evidence about the application
    sibs = [sib("i-1", "a", "prod", source="account", Application="x"), sib("i-2", "b", "prod", source="related", Application="x"),
            sib("i-3", "c", "unknown", source="unknown", Application="x")]
    assert ec.suggest_environment(target(Application="x"), sibs) is None


def test_ignore_and_unclassified_siblings_are_never_evidence():
    sibs = [sib("i-1", "a", "ignore", source="manual", Application="x"), sib("i-2", "b", None, source=None, Application="x")]
    assert ec.suggest_environment(target(Application="x"), sibs) is None


def test_volumes_copying_their_instances_tags_are_one_piece_of_evidence_not_many():
    sibs = [sib("i-1", "orders-1", "prod", Application="o")]
    sibs += [dict(resource_id=f"vol-{i}", name=f"vol-{i}", env="prod", source="tag",
                  tags={"Application": "o", "parent_ec2": "i-1"}) for i in range(5)]
    s = ec.suggest_environment(target(Application="o"), sibs)
    assert s.evidence == 1 and s.confidence == "low"          # six rows, one instance


def test_stack_tag_is_medium_with_one_sibling_and_high_with_two():
    one = [sib("i-1", "a", "dev", **{"aws:cloudformation:stack-name": "shop-stack"})]
    two = one + [sib("i-2", "b", "dev", **{"aws:cloudformation:stack-name": "shop-stack"})]
    t = target(**{"aws:cloudformation:stack-name": "shop-stack"})
    assert ec.suggest_environment(t, one).confidence == "medium"
    assert ec.suggest_environment(t, two).confidence == "high"


def test_name_prefix_ignores_the_environment_word_and_numbers():
    sibs = [sib(f"i-{i}", f"HCS-PROD-MD-0{i}", "prod") for i in range(3)]
    s = ec.suggest_environment(target(name="HCS-MD-LOGGER"), sibs)
    assert (s.env, s.basis, s.confidence) == ("prod", "prefix", "medium")


def test_single_token_prefix_is_not_enough():
    sibs = [sib(f"i-{i}", f"hcs-prod-{i}", "prod") for i in range(4)]
    assert ec.suggest_environment(target(name="hcs-batch"), sibs) is None       # shares only "hcs"


def test_two_siblings_sharing_a_prefix_is_low_and_one_is_nothing():
    two = [sib("i-1", "billing-core-prod-1", "prod"), sib("i-2", "billing-core-prod-2", "prod")]
    assert ec.suggest_environment(target(name="billing-core-worker"), two).confidence == "low"
    assert ec.suggest_environment(target(name="billing-core-worker"), two[:1]) is None


def test_prefix_siblings_that_split_give_no_suggestion():
    sibs = [sib("i-1", "billing-core-prod-1", "prod"), sib("i-2", "billing-core-dev-1", "dev"), sib("i-3", "billing-core-prod-2", "prod")]
    assert ec.suggest_environment(target(name="billing-core-worker"), sibs) is None


def test_two_kinds_of_evidence_pointing_at_different_tiers_give_nothing():
    sibs = [sib(f"i-{i}", f"x-{i}", "prod", Application="a") for i in range(3)]
    sibs += [sib(f"j-{i}", f"hcs-md-{i}", "dev") for i in range(3)]
    assert ec.suggest_environment(target(name="hcs-md-batch", Application="a"), sibs) is None


def test_agreeing_evidence_raises_confidence_one_level():
    sibs = [sib("i-1", "hcs-md-prod-1", "prod", Application="a"), sib("i-2", "hcs-md-prod-2", "prod", Application="a")]
    s = ec.suggest_environment(target(name="hcs-md-batch", Application="a"), sibs)
    assert s.basis == "application" and s.confidence == "high" and "also supported by prefix" in s.reason


def test_a_suggestion_that_contradicts_the_resources_own_name_is_dropped():
    sibs = [sib(f"i-{i}", f"x-{i}", "prod", Application="a") for i in range(3)]
    assert ec.suggest_environment(target(name="batch-dev-runner", Application="a"), sibs) is None


def test_a_conflicting_name_may_still_be_settled_by_siblings_that_match_one_of_its_tiers():
    sibs = [sib(f"i-{i}", f"x-{i}", "prod", Application="a") for i in range(3)]
    s = ec.suggest_environment(target(name="prod-to-dev-sync", Application="a"), sibs)
    assert s and s.env == "prod"                                # prod is one of the tiers the name itself mentions


def test_devops_in_the_name_is_not_a_dev_vote_against_a_prod_suggestion():
    sibs = [sib(f"i-{i}", f"x-{i}", "prod", Application="a") for i in range(3)]
    s = ec.suggest_environment(target(name="DevOps-Jenkins", Application="a"), sibs)
    assert s and s.env == "prod"


def test_the_target_does_not_vote_for_itself():
    me = sib("t-1", "hcs-md-prod-1", "prod", Application="a")
    assert ec.suggest_environment(target(rid="t-1", name="hcs-md-prod-1", Application="a"), [me]) is None


def test_no_siblings_and_no_clues_is_a_normal_empty_answer():
    assert ec.suggest_environment(target(), []) is None
    assert ec.suggest_environment(target(name="x"), [sib("i-1", "other-thing", "prod")]) is None


def test_index_gives_the_same_answers_as_the_one_shot_function():
    sibs = [sib(f"i-{i}", f"hcs-md-prod-{i}", "prod", Application="a") for i in range(4)]
    idx = ec.SuggestionIndex(sibs)
    for t in (target(Application="a"), target(name="hcs-md-x"), target(name="zzz-yyy")):
        assert idx.suggest(t) == ec.suggest_environment(t, sibs)


def test_reason_is_clipped_and_free_of_control_characters():
    value = "a" * 300 + "\n\x00bad"
    sibs = [sib(f"i-{i}", f"x{i}", "prod", Application=value) for i in range(3)]
    s = ec.suggest_environment(target(Application=value), sibs)
    assert len(s.reason) <= ec.REASON_MAX and "\n" not in s.reason and "\x00" not in s.reason


def test_a_suggestion_is_a_proposal_resolution_is_unchanged():
    t = target(Application="a")
    before = ec.resolve_environment(name=t["name"], resource_id=t["resource_id"], tags=t["tags"])
    ec.suggest_environment(t, [sib(f"i-{i}", f"x{i}", "prod", Application="a") for i in range(3)])
    assert before.env == "unknown" and ec.resolve_environment(name=t["name"], resource_id=t["resource_id"], tags=t["tags"]) == before
