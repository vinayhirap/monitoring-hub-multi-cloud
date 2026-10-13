# app/environment_classifier.py
"""
Environment resolution for a monitored resource (phase 4a).

Pure functions, no imports beyond the stdlib, so the alert evaluator, the
collector sync, the API and the unit tests can all share one definition.

Resolution order, first match wins:

    1. manual   a user-saved override (resource_env_overrides)
    2. tag      Environment / Env / Stage ... tag with a recognised value
    3. name     a recognised token in the resource NAME, then its ID
    4. related  the environment of the resource it is ATTACHED TO (an EBS volume
                follows its EC2 instance), only when that parent's own
                environment rests on a manual choice, a tag or a name
    5. account  the account's admin-set default (aws_accounts.default_env)
    6. unknown  nothing matched

Canonical results: "prod", "uat" (uat / pre-prod / stage / staging / qa),
"dev" (dev / development / test / sandbox), "unknown", and "ignore" (manual
only: a user dismissed the resource from the Needs-classification queue).

UNKNOWN IS NOT DEV AND NOT SILENT. The old evaluator defaulted a resource with
no Environment tag to "prod". That label was wrong but it failed SAFE (nothing
was suppressed). The replacement keeps the safe half: unknown and ignore are
"prod-like" (is_prod_like), so an unclassified resource is still alerted at
prod strength, but it is no longer LABELLED prod and it shows up in the
Needs-classification queue for a human to settle.

NAME MATCHING. A name is split into tokens on every non-alphanumeric character
and on camelCase boundaries; a token matches only if it equals a keyword after
trailing digits are dropped ("prod01" and "dev2" match, "product",
"reproduce", "contest" and "latest" do not). "pre-prod" is recognised as a
phrase BEFORE tokenising, otherwise its "prod" half would read as production;
"non-prod" / "nonprod" is deliberately ignored (it does not say which
non-production tier it is). Tokens that point at two different tiers
("prod-to-dev-sync") are not guessed at: the result is "unknown" with the
conflict recorded in the reason.
"""
import json
import os
import re
from collections import namedtuple

PROD, UAT, DEV, UNKNOWN, IGNORE = "prod", "uat", "dev", "unknown", "ignore"

TIERS = (PROD, UAT, DEV)
MANUAL_CHOICES = (PROD, UAT, DEV, IGNORE)          # what a user may save
SOURCES = ("manual", "tag", "name", "related", "account", "unknown")
# A parent only lends its environment when that rests on evidence about the parent itself.
# Account defaults, unknowns and other inherited values are never passed down the chain.
RELATED_PARENT_SOURCES = ("manual", "tag", "name")

Resolution = namedtuple("Resolution", "env source reason")

# Tag keys, in priority order. Keys are compared lower-cased with "-", "." and
# spaces folded to "_". "tier" is deliberately absent: it usually means web/db.
TAG_KEYS = (
    "environment", "env", "stage", "deployment_environment", "app_env",
    "appenv", "env_type", "environment_type", "envtype",
)

_PROD_WORDS = {"prod", "production", "prd"}
_UAT_WORDS = {"uat", "preprod", "preproduction", "stage", "staging", "stg", "qa", "qas"}
_DEV_WORDS = {"dev", "development", "test", "testing", "sandbox"}

_WORD_TIER = {}
for _w in _PROD_WORDS:
    _WORD_TIER[_w] = PROD
for _w in _UAT_WORDS:
    _WORD_TIER[_w] = UAT
for _w in _DEV_WORDS:
    _WORD_TIER[_w] = DEV

_SEP = r"[\s_\-./:]*"
_PRE_PROD = re.compile(r"(?<![a-z0-9])pre" + _SEP + r"prod(?:uction)?(?![a-z])", re.I)
_NON_PROD = re.compile(r"(?<![a-z0-9])non" + _SEP + r"prod(?:uction)?(?![a-z])", re.I)
_CAMEL_1 = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_2 = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
_TRAILING_DIGITS = re.compile(r"\d+$")

REASON_MAX = 200


def flag_enabled():
    """ENV_AWARE_CLASSIFICATION gates every behaviour change that reaches
    alerting (evaluator environment label and severity). Read at call time so
    flipping it in .env + restart is the whole rollout. Default: off."""
    return os.getenv("ENV_AWARE_CLASSIFICATION", "false").strip().lower() in ("true", "1", "yes")


def is_prod_like(env):
    """True for prod AND for everything we could not classify. Unknown must
    never be alerted more weakly than prod."""
    return env not in (UAT, DEV)


def normalize_tier(value):
    """'Production' / 'STAGING' / 'qa-2' -> canonical tier, or None."""
    tiers, _ = _scan(value)
    return tiers[0] if len(set(tiers)) == 1 else None


def _scan(text):
    """-> (tiers found in order, matched words). Never raises."""
    if text is None:
        return [], []
    s = str(text)
    tiers, words = [], []

    def _take_phrase(m):
        tiers.append(UAT)
        words.append(m.group(0).strip().lower())
        return " "

    s = _PRE_PROD.sub(_take_phrase, s)
    s = _NON_PROD.sub(" ", s)
    s = _CAMEL_2.sub(" ", _CAMEL_1.sub(" ", s))
    for tok in _NON_ALNUM.split(s):
        if not tok:
            continue
        base = _TRAILING_DIGITS.sub("", tok.lower())
        tier = _WORD_TIER.get(base)
        if tier:
            tiers.append(tier)
            words.append(base)
    return tiers, words


def _clip(text):
    return text if len(text) <= REASON_MAX else text[:REASON_MAX - 3] + "..."


def _tags_dict(tags):
    if isinstance(tags, dict):
        return tags
    if isinstance(tags, (str, bytes)):
        try:
            parsed = json.loads(tags or "{}")
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _from_tags(tags):
    folded = {}
    for k, v in _tags_dict(tags).items():
        folded.setdefault(re.sub(r"[-.\s]+", "_", str(k).strip().lower()), (k, v))
    for key in TAG_KEYS:
        if key not in folded:
            continue
        orig_key, value = folded[key]
        tiers, words = _scan(value)
        if tiers and len(set(tiers)) == 1:
            return Resolution(tiers[0], "tag", _clip(f"tag {orig_key}={value}"))
    return None


def _from_name(name, resource_id):
    conflict = None
    for label, text in (("name", name), ("id", resource_id)):
        tiers, words = _scan(text)
        if not tiers:
            continue
        distinct = sorted(set(tiers))
        if len(distinct) == 1:
            return Resolution(distinct[0], "name",
                              _clip(f"{label} token '{words[0]}' in '{text}'"))
        conflict = conflict or _clip(f"conflicting {label} tokens {sorted(set(words))} in '{text}'")
    if conflict:
        return Resolution(UNKNOWN, "unknown", conflict)
    return None


def _from_related(related):
    """related: iterable of (Resolution, label) for the resources this one is attached to."""
    usable = [(r, label) for r, label in (related or [])
              if r.env in TIERS and r.source in RELATED_PARENT_SOURCES]
    if not usable:
        return None
    envs = sorted({r.env for r, _ in usable})
    if len(envs) > 1:
        detail = ", ".join(f"{label}={r.env}" for r, label in usable)
        return Resolution(UNKNOWN, "unknown", _clip(f"attached resources disagree: {detail}"))
    r, label = usable[0]
    return Resolution(r.env, "related", _clip(f"attached to {label} ({r.env}, by {r.source})"))


def resolve_environment(*, override=None, tags=None, name=None, resource_id=None,
                        account_default=None, related=None):
    """Resolve one resource. See the module docstring for the order."""
    if override:
        o = str(override).strip().lower()
        if o in MANUAL_CHOICES:
            return Resolution(o, "manual", "set by a user")

    hit = _from_tags(tags)
    if hit:
        return hit

    # A name that contradicts itself comes back as UNKNOWN. It may still be settled by the
    # resource it is attached to (a person or a tag decided that), but it must never fall
    # through to the account default and pass for a confident answer.
    hit = _from_name(name, resource_id)
    if hit and hit.env != UNKNOWN:
        return hit
    name_conflict = hit

    rel = _from_related(related)
    if rel:
        return rel
    if name_conflict:
        return name_conflict

    if account_default:
        d = str(account_default).strip().lower()
        if d in TIERS:
            return Resolution(d, "account", "account default")

    return Resolution(UNKNOWN, "unknown", "no override, tag, name token or account default")


def severity_for(env, is_critical):
    """The evaluator's severity rule, environment-aware: CRITICAL stays
    CRITICAL; a warning-level breach is WARNING for prod-like resources (prod,
    unknown, ignore) and INFO for uat / dev -- the same prod-vs-rest split the
    evaluator has always had, with unknown now on the prod side explicitly."""
    if is_critical:
        return "CRITICAL"
    return "WARNING" if is_prod_like(env) else "INFO"


# ---------------------------------------------------------------------------------------------
# Tag compliance (docs/TAGGING_STANDARD.md)
# ---------------------------------------------------------------------------------------------
MANDATORY_TAGS = ("Environment", "Application", "Owner", "CostCenter",
                  "Criticality", "DataClassification", "ManagedBy")
TAG_STATES = ("ok", "nonstandard", "invalid", "missing", "not_checked")
NEEDS_TAGGING = ("missing", "invalid", "nonstandard")

# Resource types whose discovery stores the resource's REAL AWS tags. For every other type the
# stored tags are internal bookkeeping (or nothing), so "untagged" cannot be said honestly and the
# state is "not_checked". Attached EBS volumes are a special case, see tags_known().
TAGS_COLLECTED_TYPES = frozenset({"ec2", "rds", "efs", "autoscaling", "transitgateway", "vpn"})


def _fold_key(k):
    return re.sub(r"[-.\s]+", "_", str(k).strip().lower())


def tags_known(resource_type, tags):
    """True when the stored tags are the resource's own AWS tags.

    An attached EBS volume is stored WITH ITS INSTANCE'S TAGS (discovery copies them on purpose
    so alerts inherit the instance's environment, plus a parent_ec2 key). Its own tags are not
    known, so it is not checked; an unattached volume carries its own tags and is.
    """
    t = str(resource_type or "").lower()
    if t in TAGS_COLLECTED_TYPES:
        return True
    if t == "ebs":
        return "parent_ec2" not in _tags_dict(tags)
    return False


def tag_state(resource_type, tags):
    """ok | nonstandard | invalid | missing | not_checked.

    ok          Environment tag, exactly the standard key and one of prod / uat / dev
    nonstandard understood but not to the standard (Env=Production, environment=prod, ...)
    invalid     an environment-style tag is present but its value means nothing / is ambiguous
    missing     no environment-style tag at all
    not_checked the stored tags are not this resource's own AWS tags (cannot say)
    """
    if not tags_known(resource_type, tags):
        return "not_checked"
    d = _tags_dict(tags)
    if str(d.get("Environment", "")) in TIERS:
        return "ok"
    if not any(_fold_key(k) in TAG_KEYS for k in d):
        return "missing"
    return "nonstandard" if _from_tags(d) else "invalid"


def tag_environment(tags):
    """The environment the resource's own tag says (prod / uat / dev), or None."""
    hit = _from_tags(tags)
    return hit.env if hit else None


def missing_tags(tags):
    """Mandatory standard tags absent from the resource (key case ignored)."""
    have = {_fold_key(k) for k in _tags_dict(tags)}
    return [k for k in MANDATORY_TAGS if _fold_key(k) not in have]
