# app/environment_classifier.py
"""
Environment resolution for a monitored resource (phase 4a).

Pure functions, no imports beyond the stdlib, so the alert evaluator, the
collector sync, the API and the unit tests can all share one definition.

Resolution order, first match wins:

    1. manual   a user-saved override (resources.env_override)
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
conflict recorded in the reason. "DevOps" (also DevSecOps / SecDevOps) is a discipline, not the dev tier, and is
ignored like "non-prod".
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
# "DevOps" / "DevSecOps" / "SecDevOps" name a discipline, not the dev tier. Without this a camelCase name such as
# "DevOps-Jenkins" split into Dev + Ops and a PRODUCTION build server was labelled dev (warnings downgraded to INFO).
_DEVOPS = re.compile(r"(?<![a-z0-9])(?:sec" + _SEP + r")?dev" + _SEP + r"(?:sec" + _SEP + r")?ops(?![a-z])", re.I)
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
    s = _DEVOPS.sub(" ", s)          # after the camelCase split, so "DevOpsRunner" -> "Dev Ops Runner" is seen
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

# The tags CloudOps itself can use. Environment decides alert strength and polling today. Criticality is
# reserved for the phase-4b priority bump. Application and Owner say what a resource belongs to and who answers
# for it (alert context, escalation, ownership). The rest of the standard (CostCenter, DataClassification,
# ManagedBy) is cost / governance data that CloudOps never reads, so the Environments page does not nag about it.
ESSENTIAL_TAGS = ("Environment", "Criticality", "Owner", "Application")

# Resource types whose discovery stores the resource's REAL AWS tags. For every other type the
# stored tags are internal bookkeeping (or nothing), so "untagged" cannot be said honestly and the
# state is "not_checked". Attached EBS volumes are a special case, see tags_known().
TAGS_COLLECTED_TYPES = frozenset({"ec2", "rds", "efs", "autoscaling", "transitgateway", "vpn"})


def _fold_key(k):
    return re.sub(r"[-.\s]+", "_", str(k).strip().lower())


# Types whose real AWS tags the tag sync (app/collector/tag_sync.py, flag TAG_SYNC_ENABLED) can learn from the
# Resource Groups Tagging API. Kept equal to app/aws/arn_match.SYNC_TYPES (a test enforces it; this module stays
# import-free). Being listed here never says "untagged" by itself: a row only becomes known when the sync stored
# a fresh result for it, see tags_known(synced=...).
TAG_SYNC_TYPES = frozenset({
    "s3", "logs", "kms", "lambda", "elb", "sqs", "sns", "natgateway", "certificatemanager",
    "cloudfront", "kinesis", "wafv2", "backup", "events", "dynamodb", "eks", "firehose",
})


# Azure and GCP discovery store each resource's OWN tags / labels straight from the list API (no index, no sweep), but
# only for these types, verified one by one in app/providers/{azure,gcp}/discovery.py. Deliberately NOT listed:
#   GCP gke_node, firestore_database, nat_gateway   discovery stores a {} placeholder, not real labels
#   GCP bigquery_project                            labels are read with a hasattr() fallback to {}
#   Azure / GCP types found by the generic Resource Graph / asset-inventory path ("other")
# An unlisted type stays "not_checked": an empty stored value there would otherwise read as "untagged".
AZURE_TAGGED_TYPES = frozenset({
    "vm", "storage_account", "sql_database", "app_service", "function_app", "vmss", "aks_cluster",
    "cosmosdb_account", "redis_cache", "service_bus_namespace", "eventhub_namespace", "load_balancer",
    "application_gateway", "vpn_gateway", "key_vault", "container_instance", "cdn_profile", "data_factory",
    "managed_disk",
})
GCP_LABELED_TYPES = frozenset({
    "compute_instance", "gcs_bucket", "cloudsql_instance", "cloud_run_service", "gke_cluster",
    "cloudfunctions_function", "pubsub_topic", "pubsub_subscription", "cloud_lb", "redis_instance",
    "spanner_instance", "gce_persistent_disk",
})
PROVIDER_TAG_TYPES = {"azure": AZURE_TAGGED_TYPES, "gcp": GCP_LABELED_TYPES}


def tag_sync_enabled():
    """TAG_SYNC_ENABLED gates the Tagging-API sweep and every use of its result. Default: off."""
    return os.getenv("TAG_SYNC_ENABLED", "false").strip().lower() in ("true", "1", "yes")


def tag_sync_feeds_environment():
    """TAG_SYNC_FEED_ENVIRONMENT (needs TAG_SYNC_ENABLED): let an Environment tag learned by the sync decide the
    resource's environment. Separate and default off because it can change alert strength. Default: off."""
    return tag_sync_enabled() and os.getenv("TAG_SYNC_FEED_ENVIRONMENT", "false").strip().lower() in ("true", "1", "yes")


def parse_synced(raw):
    """resources.synced_tags (JSON text / dict / None) -> dict, or None when there is no usable value.
    {} is a real answer ("swept and not in the result"); None means "not known"."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            parsed = json.loads(raw)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _synced_usable(resource_type, synced):
    return synced is not None and str(resource_type or "").lower() in TAG_SYNC_TYPES


def effective_tags(resource_type, tags, synced=None):
    """The tags that describe the resource's OWN AWS tags when known: the sync result for sync types, else the
    stored tags. Used for tag state and for the Environments page's missing-tag list."""
    return synced if _synced_usable(resource_type, synced) else _tags_dict(tags)


def tags_known(resource_type, tags, synced=None, provider=None):
    """True when the stored tags are the resource's own tags / labels.

    An attached EBS volume is stored WITH ITS INSTANCE'S TAGS (discovery copies them on purpose
    so alerts inherit the instance's environment, plus a parent_ec2 key). Its own tags are not
    known, so it is not checked; an unattached volume carries its own tags and is.

    `synced` is the FRESH tag-sync result for the row (dict, {} = swept and absent) or None. It makes the
    row known only for TAG_SYNC_TYPES; a stale or missing result is passed as None by the caller.

    `provider` is "azure" or "gcp" for rows of those clouds (None for AWS, and None for everything while the
    tag-status feature is off): their types are known only when listed in PROVIDER_TAG_TYPES.
    """
    t = str(resource_type or "").lower()
    p = str(provider or "").lower()
    if p in PROVIDER_TAG_TYPES:
        return t in PROVIDER_TAG_TYPES[p]
    if t in TAGS_COLLECTED_TYPES:
        return True
    if _synced_usable(t, synced):
        return True
    if t == "ebs":
        return "parent_ec2" not in _tags_dict(tags)
    return False


def tag_state(resource_type, tags, synced=None, provider=None):
    """ok | nonstandard | invalid | missing | not_checked.

    ok          Environment tag, exactly the standard key and one of prod / uat / dev
                (GCP labels cannot hold capitals, so there the key is `environment`)
    nonstandard understood but not to the standard (Env=Production, environment=prod, ...)
    invalid     an environment-style tag is present but its value means nothing / is ambiguous
    missing     no environment-style tag at all
    not_checked the stored tags are not this resource's own AWS tags (cannot say)
    """
    if not tags_known(resource_type, tags, synced, provider):
        return "not_checked"
    d = effective_tags(resource_type, tags, synced)
    key = "environment" if str(provider or "").lower() == "gcp" else "Environment"
    if str(d.get(key, "")) in TIERS:
        return "ok"
    if not any(_fold_key(k) in TAG_KEYS for k in d):
        return "missing"
    return "nonstandard" if _from_tags(d) else "invalid"


def tag_environment(tags):
    """The environment the resource's own tag says (prod / uat / dev), or None."""
    hit = _from_tags(tags)
    return hit.env if hit else None


def missing_tags(tags, keys=None):
    """Standard tags absent from the resource (key case ignored). `keys` defaults to the full standard;
    pass ESSENTIAL_TAGS for the ones CloudOps actually uses."""
    have = {_fold_key(k) for k in _tags_dict(tags)}
    return [k for k in (keys or MANDATORY_TAGS) if _fold_key(k) not in have]


# ---------------------------------------------------------------------------------------------
# Workstream C: mismatch warnings and suggestions (pure; neither changes a resolved environment)
# ---------------------------------------------------------------------------------------------
# A WARNING says "two pieces of evidence disagree, a person should look". It is stored beside the resolution
# (resources.env_warning) and never feeds back into it: resolve_environment() above is untouched.
WARNING_KINDS = ("manual_vs_tag", "tag_vs_name", "manual_vs_name", "volume_vs_instance")


def name_environment(name, resource_id=None):
    """The tier the NAME (then the ID) says, or None when it says nothing or contradicts itself."""
    hit = _from_name(name, resource_id)
    return hit.env if hit and hit.env in TIERS else None


def _tier_word(env):
    return {PROD: "prod", UAT: "uat", DEV: "dev"}.get(env, str(env))


def detect_warning(*, env, source, override=None, tags=None, name=None, resource_id=None,
                   resource_type=None, attached=None):
    """-> (kind, message) or None. First matching kind wins; `kind` is one of WARNING_KINDS.

    env, source  the resolution already computed for this resource
    attached     iterable of (Resolution, label) for the resources THIS one is attached to (an EBS volume's
                 instance). Only parents whose own environment rests on evidence (RELATED_PARENT_SOURCES)
                 count, the same rule the classifier uses to pass an environment down.

    The message is a short lowercase fragment prefixed with the kind ("tag_vs_name: tag says prod but the
    name says dev"), at most REASON_MAX characters. An "ignore" choice, an unknown resource and a resource
    with no second piece of evidence never produce a warning.
    """
    o = str(override).strip().lower() if override else None
    manual = o if o in TIERS else None
    tag_env = tag_environment(tags)
    name_env = name_environment(name, resource_id)
    # An attached volume is stored WITH ITS INSTANCE'S tags (see tags_known): say whose tag it is.
    own_tag = "tag"
    if str(resource_type or "").lower() == "ebs" and "parent_ec2" in _tags_dict(tags):
        own_tag = "attached instance's tag"

    if manual and tag_env and manual != tag_env:
        return "manual_vs_tag", _clip(f"manual_vs_tag: placed as {_tier_word(manual)} here but the {own_tag} says {_tier_word(tag_env)}")
    if tag_env and name_env and tag_env != name_env:
        return "tag_vs_name", _clip(f"tag_vs_name: the {own_tag} says {_tier_word(tag_env)} but the name says {_tier_word(name_env)}")
    if manual and name_env and manual != name_env:
        return "manual_vs_name", _clip(f"manual_vs_name: placed as {_tier_word(manual)} here but the name says {_tier_word(name_env)}")
    if env in TIERS and source != "related":
        for res, label in (attached or ()):
            if res.env in TIERS and res.source in RELATED_PARENT_SOURCES and res.env != env:
                return "volume_vs_instance", _clip(
                    f"volume_vs_instance: {_tier_word(env)} but attached to {label}, which is {_tier_word(res.env)}")
    return None


def warning_kind(message):
    """'tag_vs_name: ...' -> 'tag_vs_name' (None when empty or not recognised)."""
    if not message:
        return None
    kind = str(message).split(":", 1)[0].strip()
    return kind if kind in WARNING_KINDS else None


# ---- suggestions for UNKNOWN resources -----------------------------------------------------
# Weaker evidence than resolve_environment() accepts, so it is only ever PROPOSED, never applied. The evidence is
# resources in the SAME account that were decided by their own evidence (manual / tag / name), grouped by:
#   application  the same Application tag value
#   stack        the same CloudFormation stack / Auto Scaling group / Beanstalk environment tag
#   prefix       the same first two or three name tokens (the environment word itself is ignored)
# Every basis must be UNANIMOUS among the evidence it finds. Siblings that disagree, or two bases that point at
# different tiers, or a suggestion that contradicts a tier word in the resource's own name, all give no suggestion.
SUGGESTION_SOURCES = ("manual", "tag", "name")        # sibling evidence that rests on the sibling itself
CONFIDENCE = ("low", "medium", "high")
APPLICATION_TAG_KEYS = ("application", "app", "application_name")
STACK_TAG_KEYS = ("aws:cloudformation:stack_name", "aws:autoscaling:groupname",
                  "elasticbeanstalk:environment_name")
MIN_PREFIX_CHARS = 5

Suggestion = namedtuple("Suggestion", "env confidence basis reason evidence")


def _tag_value(tags, keys):
    folded = {}
    for k, v in _tags_dict(tags).items():
        folded.setdefault(re.sub(r"[-.\s]+", "_", str(k).strip().lower()), v)
    for key in keys:
        v = folded.get(key)
        if v is not None and str(v).strip():
            return str(v).strip().lower()
    return None


def _name_tokens(text):
    """Leading name tokens with tier words and bare numbers removed ('HCS-PROD-MD-01' -> hcs, md)."""
    if not text:
        return []
    s = _PRE_PROD.sub(" ", str(text))
    s = _NON_PROD.sub(" ", s)
    s = _CAMEL_2.sub(" ", _CAMEL_1.sub(" ", s))
    s = _DEVOPS.sub(" devops ", s)
    out = []
    for tok in _NON_ALNUM.split(s):
        if not tok:
            continue
        low = tok.lower()
        base = _TRAILING_DIGITS.sub("", low)
        if _WORD_TIER.get(base) or not base:        # an environment word, or digits only
            continue
        out.append(low)
    return out


def _evidence_unit(sib):
    """An attached volume carries its instance's tags and decision, so it is the same piece of evidence as the
    instance, not a second one. Everything else counts once."""
    parent = _tags_dict(sib.get("tags")).get("parent_ec2")
    return str(parent) if parent else str(sib.get("resource_id") or sib.get("id"))      # the instance's own id = its volumes' key


def _vote(candidates):
    """-> (env or None, evidence units). env is None when the candidates are empty or point at different tiers."""
    by_unit = {}
    for s in candidates:
        by_unit.setdefault(_evidence_unit(s), set()).add(s.get("env"))
    envs = set()
    for tiers in by_unit.values():
        envs |= tiers
    if len(envs) != 1:
        return None, len(by_unit)
    return next(iter(envs)), len(by_unit)


def _bump(level):
    return CONFIDENCE[min(CONFIDENCE.index(level) + 1, len(CONFIDENCE) - 1)]


def _shown(value, limit=40):
    v = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))
    return v if len(v) <= limit else v[:limit - 3] + "..."


class SuggestionIndex:
    """Sibling evidence indexed once, so suggesting for every resource of a page costs a few dict lookups each
    (a naive scan per resource is quadratic in the size of the account)."""

    def __init__(self, siblings):
        self.by_app, self.by_stack, self.by_prefix = {}, {}, {}
        self.size = 0
        for s in siblings or ():
            if s.get("env") not in TIERS or s.get("source") not in SUGGESTION_SOURCES:
                continue
            self.size += 1
            v = _tag_value(s.get("tags"), APPLICATION_TAG_KEYS)
            if v:
                self.by_app.setdefault(v, []).append(s)
            v = _tag_value(s.get("tags"), STACK_TAG_KEYS)
            if v:
                self.by_stack.setdefault(v, []).append(s)
            toks = _name_tokens(s.get("name") or s.get("resource_id"))
            for length in (2, 3):
                if len(toks) >= length:
                    self.by_prefix.setdefault((length, tuple(toks[:length])), []).append(s)

    @staticmethod
    def _others(candidates, target):
        rid = target.get("resource_id")
        return [c for c in candidates if c.get("resource_id") != rid]

    def suggest(self, target):
        if not self.size:
            return None
        found = []                               # (basis, env, units, confidence, reason)
        app_v = _tag_value(target.get("tags"), APPLICATION_TAG_KEYS)
        if app_v:
            env, n = _vote(self._others(self.by_app.get(app_v, ()), target))
            if env:
                conf = "high" if n >= 3 else "medium" if n == 2 else "low"
                found.append(("application", env, n, conf,
                              f"{n} other resource(s) with Application '{_shown(app_v)}' are all {_tier_word(env)}"))
        stack_v = _tag_value(target.get("tags"), STACK_TAG_KEYS)
        if stack_v:
            env, n = _vote(self._others(self.by_stack.get(stack_v, ()), target))
            if env:
                conf = "high" if n >= 2 else "medium"
                found.append(("stack", env, n, conf,
                              f"{n} other resource(s) in stack or group '{_shown(stack_v)}' are all {_tier_word(env)}"))
        toks = _name_tokens(target.get("name") or target.get("resource_id"))
        if len(toks) >= 2:
            for length in range(min(len(toks), 3), 1, -1):
                head = tuple(toks[:length])
                if sum(len(t) for t in head) < MIN_PREFIX_CHARS:
                    continue
                cands = self._others(self.by_prefix.get((length, head), ()), target)
                if not cands:
                    continue                     # nobody shares this longer prefix, try a shorter one
                env, n = _vote(cands)
                if env and n >= 2:
                    conf = "medium" if n >= 3 else "low"
                    found.append(("prefix", env, n, conf,
                                  f"{n} other resource(s) named '{'-'.join(head)}...' are all {_tier_word(env)}"))
                break                            # the longest shared prefix decides; a split stays a split
        if not found or len({f[1] for f in found}) > 1:
            return None                          # nothing found, or two kinds of evidence disagree
        found.sort(key=lambda f: (CONFIDENCE.index(f[3]), f[2]), reverse=True)
        basis, env, n, conf, reason = found[0]
        if len(found) > 1:
            conf = _bump(conf)
            reason += f"; also supported by {', '.join(f[0] for f in found[1:])}"
        own = set(_scan(target.get("name"))[0]) | set(_scan(target.get("resource_id"))[0])
        if own and env not in own:
            return None                          # its own name names another tier: leave that to a person
        return Suggestion(env, conf, basis, _clip(reason), n)


def suggest_environment(target, siblings):
    """Propose an environment for one UNKNOWN resource, or None ("no suggestion" is a normal answer).

    target    dict: resource_id, name, resource_type, tags
    siblings  iterable of dicts: resource_id, name, tags, env, source (same account, other resources)
    -> Suggestion(env, confidence, basis, reason, evidence) | None
    Build a SuggestionIndex once when suggesting for many resources of the same account.
    """
    return SuggestionIndex(siblings).suggest(target)
