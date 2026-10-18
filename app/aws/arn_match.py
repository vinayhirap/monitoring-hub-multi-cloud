# app/aws/arn_match.py
"""
ARN <-> resources-row matching for the tag sync (workstream B). PURE: stdlib only, no AWS, no DB.

WHAT A TAGGING-API SWEEP CAN AND CANNOT SAY
    A resourcegroupstaggingapi:GetResources sweep PROVES a resource is tagged and shows its tags.
    It can NOT prove a resource is untagged: untagged resources are simply absent from the result.
    "Absent from the sweep" may be said as "untagged" ONLY when ALL of these hold, and this module
    is where they are enforced (plan_account):

      a) the resource type is in ABSENCE_VERIFIED_TYPES (its ARN <-> resource_id mapping is
         deterministic, covered by tests, and the type is indexed per region);
      b) the sweep for the row's region completed (no error, no throttle, every page read);
      c) the sweep returned at least one resource at all (an entirely empty answer cannot be told
         apart from a tagging index that has not caught up, so nothing is claimed from it);
      d) the type passes the canary: if the sweep holds ARNs of that type but NOT ONE of them matched
         a row, the mapping is suspect for this account/region and nothing is claimed for the type;
      e) the row itself has an unambiguous key (a row whose id cannot be turned into a key is never
         called untagged).

    Everything else stays unknown ("not_checked" in the UI). A tagged resource that fails to match is
    therefore never reported missing: failing to match is "unknown", never "absent".

MATCHING
    Both sides are reduced to the same key (resource_type, account_number, region, name_or_id):
      * region is "" for global services (S3 buckets, CloudFront distributions);
      * a row whose resource_id is already an ARN (lambda, elb, certificatemanager) is parsed;
      * a row whose resource_id is a bare name/id uses resources.region.
    The matcher REFUSES when ambiguous: two ARNs or two rows with the same key, the same ARN
    delivered twice with different tags, an ARN for another account, a WAF ACL whose id disagrees
    with the id discovery stored. Refusals are returned (plan.types[...]['ambiguous']) so the report
    script can show them.
"""
from collections import namedtuple

Arn = namedtuple("Arn", "partition service region account resource")
Key = namedtuple("Key", "rtype account region name")

# Types the sweep may TAG-PROVE (a matched ARN gives the row its real tags).
# Existing tag-aware types (ec2, rds, efs, autoscaling, transitgateway, vpn, unattached ebs) are not
# listed: discovery already stores their own tags.
SYNC_TYPES = frozenset({
    "s3", "logs", "kms", "lambda", "elb", "sqs", "sns", "natgateway", "certificatemanager",
    "cloudfront", "kinesis", "wafv2", "backup", "events", "dynamodb", "eks", "firehose",
})

# Types that may additionally be called UNTAGGED when absent from a completed sweep.
# Deliberately NOT included until a prod report shows zero unmatched rows and unmatched ARNs:
#   s3          bucket tags may be indexed under the bucket's region or us-east-1; unverified
#   cloudfront  global service, indexed in us-east-1 only; one extra sweep and a global-scope claim
#   wafv2       row id is the ACL NAME; REGIONAL vs CLOUDFRONT scope and the ACL id need a live check
# Promote a type by adding it here in a reviewed change, never by a runtime setting.
ABSENCE_VERIFIED_TYPES = frozenset({
    "logs", "kms", "lambda", "elb", "sqs", "sns", "natgateway", "certificatemanager",
    "kinesis", "backup", "events", "dynamodb", "eks", "firehose",
})

# Types whose ARNs carry no region (global namespace).
GLOBAL_TYPES = frozenset({"s3", "cloudfront"})

# What each kind of sweep may speak for. "full" is an unfiltered GetResources of one region: it proves tags for every
# type AND (rules below) can prove absence. "global" and "s3" are FILTERED sweeps (ResourceTypeFilters) made only to
# find CloudFront distributions / S3 buckets cheaply: they prove tags for those types and nothing else, and they never
# count as a completed sweep of their region, so they cannot support an "untagged" claim for any type.
SCOPE_TYPES = {"full": None, "global": frozenset({"s3", "cloudfront"}), "s3": frozenset({"s3"})}

assert ABSENCE_VERIFIED_TYPES <= SYNC_TYPES

# Types whose tags can be read AUTHORITATIVELY, one resource at a time, when a sweep cannot decide a row
# (TAG_SYNC_DIRECT_LOOKUP, default off): S3 GetBucketTagging answers "NoSuchTagSet" for an untagged bucket and
# WAFv2 ListTagsForResource returns an empty list. Used only for rows the sweep left undecided.
DIRECT_TYPES = frozenset({"s3", "wafv2"})


def parse_arn(arn):
    """'arn:aws:sqs:ap-south-1:123456789012:q' -> Arn(...) or None. Never raises."""
    if not isinstance(arn, str):
        return None
    parts = arn.strip().split(":", 5)
    if len(parts) != 6 or parts[0] != "arn" or not parts[2] or not parts[5]:
        return None
    return Arn(parts[1], parts[2], parts[3], parts[4], parts[5])


def _one_segment(resource, prefix):
    """resource 'prefix<rest>' -> rest, only when rest is a single non-empty segment (no '/' ':')."""
    if not resource.startswith(prefix):
        return None
    rest = resource[len(prefix):]
    if not rest or "/" in rest or ":" in rest:
        return None
    return rest


def _arn_name(a):
    """The row-side name an ARN corresponds to, or None when this ARN is not a resource we store.
    Returns (rtype, name) ."""
    s, r = a.service, a.resource
    if s == "s3":
        # arn:aws:s3:::bucket  (object ARNs contain '/')
        if a.region == "" and a.account == "" and "/" not in r and ":" not in r:
            return "s3", r
        return None
    if s == "logs":
        if r.startswith("log-group:"):
            name = r[len("log-group:"):]
            if name.endswith(":*"):
                name = name[:-2]
            if name and ":" not in name:
                return "logs", name
        return None
    if s == "kms":
        k = _one_segment(r, "key/")
        return ("kms", k) if k else None            # aliases (alias/...) are not rows
    if s == "lambda":
        f = _one_segment(r, "function:")
        return ("lambda", f) if f else None         # aliases / versions carry a further ':' -> refused
    if s == "elasticloadbalancing":
        if r.startswith(("loadbalancer/app/", "loadbalancer/net/", "loadbalancer/gwy/")) and r.count("/") == 3:
            return "elb", r
        return None                                  # target groups, listeners, classic ELBs
    if s == "sqs":
        q = _one_segment(r, "")
        return ("sqs", q) if q else None
    if s == "sns":
        t = _one_segment(r, "")
        return ("sns", t) if t else None             # subscription ARNs have a further ':'
    if s == "ec2":
        g = _one_segment(r, "natgateway/")
        return ("natgateway", g) if g else None
    if s == "acm":
        c = _one_segment(r, "certificate/")
        return ("certificatemanager", c) if c else None
    if s == "cloudfront":
        d = _one_segment(r, "distribution/")
        return ("cloudfront", d) if d else None
    if s == "kinesis":
        k = _one_segment(r, "stream/")
        return ("kinesis", k) if k else None        # stream/NAME/consumer/... has '/' -> refused
    if s == "wafv2":
        # regional/webacl/NAME/ID . global/ (CLOUDFRONT scope) is not discovered -> not a row
        seg = r.split("/")
        if len(seg) == 4 and seg[0] == "regional" and seg[1] == "webacl" and seg[2] and seg[3]:
            return "wafv2", seg[2]
        return None
    if s == "backup":
        v = _one_segment(r, "backup-vault:")
        return ("backup", v) if v else None
    if s == "events":
        # default event bus only: rule/NAME. rule/BUS/NAME is a custom bus and discovery lists the default bus
        e = _one_segment(r, "rule/")
        return ("events", e) if e else None
    if s == "dynamodb":
        t = _one_segment(r, "table/")
        return ("dynamodb", t) if t else None       # table/T/index/I, table/T/stream/S refused
    if s == "eks":
        c = _one_segment(r, "cluster/")
        return ("eks", c) if c else None
    if s == "firehose":
        d = _one_segment(r, "deliverystream/")
        return ("firehose", d) if d else None
    return None


def arn_key(arn):
    """ARN -> (Key, waf_acl_id_or_None), or None when it is not a resource type we sync."""
    a = parse_arn(arn)
    if a is None:
        return None
    hit = _arn_name(a)
    if hit is None:
        return None
    rtype, name = hit
    if rtype not in SYNC_TYPES:
        return None
    region = "" if rtype in GLOBAL_TYPES else a.region
    if rtype not in GLOBAL_TYPES and not region:
        return None
    acct = a.account                # '' for s3
    acl_id = a.resource.split("/")[3] if rtype == "wafv2" else None
    return Key(rtype, acct, region, name), acl_id


def row_key(row, account_number):
    """A resources row -> Key, or None when the row cannot be matched safely.
    row: dict with resource_type, resource_id, region (and tags for wafv2 ids)."""
    rtype = str(row.get("resource_type") or "").lower()
    rid = row.get("resource_id")
    if rtype not in SYNC_TYPES or not rid or not account_number:
        return None
    rid = str(rid)
    if rtype in ("lambda", "elb", "certificatemanager"):
        # discovery stores the full ARN as the id
        hit = arn_key(rid)
        if hit is None or hit[0].rtype != rtype or hit[0].account != account_number:
            return None
        return hit[0]
    if rid.startswith("arn:"):
        return None                                  # a bare-name type holding an ARN: do not guess
    if rtype == "s3":
        return Key("s3", "", "", rid)
    if rtype == "cloudfront":
        return Key("cloudfront", account_number, "", rid)
    region = str(row.get("region") or "")
    if not region or region == "global":
        return None
    return Key(rtype, account_number, region, rid)


def _clean_tags(tags):
    """AWS tags -> {key: str(value)} without the 'aws:' system tags (cloudformation, createdBy...)."""
    out = {}
    for k, v in (tags or {}).items():
        if not isinstance(k, str) or k.lower().startswith("aws:"):
            continue
        out[k] = "" if v is None else str(v)
    return out


Decision = namedtuple("Decision", "status tags reason")        # status: tagged | absent


def plan_account(rows, sweeps, account_number, absence_types=ABSENCE_VERIFIED_TYPES):
    """Decide, for one account, what each row's tag knowledge is.

    rows    [{id, resource_type, resource_id, region, tags}]  live rows of this account
    sweeps  [{region, ok, items: [{arn, tags}], scope?}]   one per sweep; ok=False means failed/partial;
            scope is "full" (default), "global" or "s3" -- see SCOPE_TYPES
    returns {"decisions": {row id: Decision}, "types": {rtype: stats}, "foreign_arns": [...],
             "unsupported_arns": int}
      stats: rows, matched, absent, unknown, unmatched_rows [(id, resource_id)],
             unmatched_arns [arn], ambiguous [text], absence_blocked [reason]
    Rows the plan says nothing about (no entry in decisions) keep whatever was known before.
    """
    absence_types = frozenset(absence_types) & ABSENCE_VERIFIED_TYPES
    stats = {}

    def st(t):
        return stats.setdefault(t, {"rows": 0, "matched": 0, "absent": 0, "unknown": 0,
                                    "direct_tagged": 0, "direct_absent": 0,
                                    "unmatched_rows": [], "unmatched_arns": [],
                                    "ambiguous": [], "absence_blocked": []})

    ok_regions = {}                                   # region -> number of resources the sweep returned
    arn_index = {}                                    # Key -> [(arn, tags, acl_id, sweep_region)]
    foreign, unsupported = [], 0
    for sw in sweeps:
        if not sw.get("ok"):
            continue
        items = sw.get("items") or []
        scope = sw.get("scope") or "full"
        allowed = SCOPE_TYPES.get(scope, frozenset())      # unknown scope: speaks for nothing
        if scope == "full":
            ok_regions[sw["region"]] = ok_regions.get(sw["region"], 0) + len(items)
        for it in items:
            arn = it.get("arn")
            hit = arn_key(arn)
            if hit is None:
                unsupported += 1
                continue
            key, acl_id = hit
            if allowed is not None and key.rtype not in allowed:
                continue                                   # outside what this filtered sweep may speak for
            # S3 ARNs carry no account (rows are keyed with account ""); every other type must be ours
            if key.rtype != "s3" and key.account != account_number:
                foreign.append(arn)
                continue
            arn_index.setdefault(key, []).append((arn, _clean_tags(it.get("tags")), acl_id, sw["region"]))

    # rows by key; duplicates are refused on both sides
    row_keys = {}
    for r in rows:
        k = row_key(r, account_number)
        if str(r.get("resource_type") or "").lower() in SYNC_TYPES:
            st(str(r["resource_type"]).lower())["rows"] += 1
        row_keys[r["id"]] = k
    by_key = {}
    for r in rows:
        k = row_keys[r["id"]]
        if k is not None:
            by_key.setdefault(k, []).append(r)

    decisions = {}
    matched_arns = set()
    matched_per_type_region = {}                      # (rtype, region) -> matches
    arns_per_type_region = {}                         # (rtype, region) -> arns of the type in a completed sweep
    for key, entries in arn_index.items():
        arns_per_type_region[(key.rtype, key.region)] = arns_per_type_region.get((key.rtype, key.region), 0) + 1

    pending_absent = []
    for r in rows:
        rtype = str(r.get("resource_type") or "").lower()
        if rtype not in SYNC_TYPES:
            continue
        s = st(rtype)
        k = row_keys[r["id"]]
        if k is None:
            s["unknown"] += 1
            s["unmatched_rows"].append((r["id"], r.get("resource_id")))
            continue
        if len(by_key[k]) > 1:
            s["unknown"] += 1
            s["ambiguous"].append(f"{len(by_key[k])} rows share key {tuple(k)}")
            continue
        entries = arn_index.get(k)
        if entries:
            distinct = {(tuple(sorted(e[1].items())), e[2]) for e in entries}
            if len(distinct) > 1:
                s["unknown"] += 1
                s["ambiguous"].append(f"{k.rtype} {k.name}: ARN seen with differing tags/ids")
                continue
            arn, tags, acl_id, _ = entries[0]
            if rtype == "wafv2":
                stored = _tags_acl_id(r.get("tags"))
                if stored and acl_id and stored != acl_id:
                    s["unknown"] += 1
                    s["ambiguous"].append(f"wafv2 {k.name}: ARN id {acl_id} != stored acl_id {stored}")
                    continue
            decisions[r["id"]] = Decision("tagged", tags, f"matched {arn}")
            matched_arns.update(e[0] for e in entries)
            s["matched"] += 1
            tr = (rtype, k.region)
            matched_per_type_region[tr] = matched_per_type_region.get(tr, 0) + 1
            continue
        pending_absent.append((r, k))

    for r, k in pending_absent:
        rtype = k.rtype
        s = st(rtype)
        reason = _absence_blocker(rtype, k, absence_types, ok_regions, arns_per_type_region,
                                  matched_per_type_region)
        if reason is None:
            decisions[r["id"]] = Decision("absent", {}, "not in a completed sweep of " + k.region)
            s["absent"] += 1
        else:
            s["unknown"] += 1
            s["unmatched_rows"].append((r["id"], r.get("resource_id")))
            if reason not in s["absence_blocked"]:
                s["absence_blocked"].append(reason)

    for key, entries in arn_index.items():
        for e in entries:
            if e[0] not in matched_arns:
                st(key.rtype)["unmatched_arns"].append(e[0])
    return {"decisions": decisions, "types": stats, "foreign_arns": foreign, "unsupported_arns": unsupported}


clean_tags = _clean_tags


def waf_acl_arn(row, account_number):
    """ARN of a REGIONAL WAFv2 web ACL row (resource_id = ACL name, tags.acl_id = its id), or None when anything
    needed is missing or malformed. Nothing is guessed: no stored acl_id, no lookup."""
    name, acl_id, region = row.get("resource_id"), _tags_acl_id(row.get("tags")), str(row.get("region") or "")
    if not (name and acl_id and account_number) or not region or region == "global":
        return None
    for part in (str(name), str(acl_id), region, str(account_number)):
        if not part or "/" in part or ":" in part or " " in part:
            return None
    return f"arn:aws:wafv2:{region}:{account_number}:regional/webacl/{name}/{acl_id}"


def direct_candidates(rows, plan, limit=None):
    """Rows of DIRECT_TYPES the sweep left without a decision, in id order."""
    out = [r for r in sorted(rows, key=lambda r: r["id"])
           if str(r.get("resource_type") or "").lower() in DIRECT_TYPES and r["id"] not in plan["decisions"]]
    return out if limit is None else out[:limit]


def merge_direct(plan, rows, results):
    """Fold direct-lookup results into a plan_account() result (in place, returned).
    results {row id: dict of tags | None}: a dict is an AUTHORITATIVE answer ({} = the resource has no tags),
    None means the lookup could not tell (error, access denied, wrong region) and the row stays undecided.
    Only rows the sweep left undecided are considered, and only DIRECT_TYPES."""
    by_id = {r["id"]: r for r in rows}
    for rid, tags in (results or {}).items():
        r = by_id.get(rid)
        if r is None or tags is None or rid in plan["decisions"]:
            continue
        t = str(r.get("resource_type") or "").lower()
        if t not in DIRECT_TYPES:
            continue
        s = plan["types"].setdefault(t, {"rows": 0, "matched": 0, "absent": 0, "unknown": 0, "direct_tagged": 0,
                                         "direct_absent": 0, "unmatched_rows": [], "unmatched_arns": [],
                                         "ambiguous": [], "absence_blocked": []})
        clean = _clean_tags(tags)
        if clean:
            plan["decisions"][rid] = Decision("tagged", clean, "direct lookup")
            s["direct_tagged"] += 1
        else:
            plan["decisions"][rid] = Decision("absent", {}, "direct lookup: the resource has no tags")
            s["direct_absent"] += 1
        s["unknown"] = max(0, s["unknown"] - 1)
        s["unmatched_rows"] = [x for x in s["unmatched_rows"] if x[0] != rid]
    return plan


def _tags_acl_id(tags):
    import json
    if isinstance(tags, (str, bytes)):
        try:
            tags = json.loads(tags or "{}")
        except Exception:
            return None
    return tags.get("acl_id") if isinstance(tags, dict) else None


def _absence_blocker(rtype, key, absence_types, ok_regions, arns_per_type_region, matched_per_type_region):
    """None when 'untagged' may be said for this unmatched row, else the reason it may not."""
    if rtype not in absence_types:
        return f"{rtype}: type not in the absence-verified list"
    if rtype in GLOBAL_TYPES:
        return f"{rtype}: global type"
    if key.region not in ok_regions:
        return f"{rtype}: no completed sweep for {key.region}"
    if ok_regions[key.region] == 0:
        return f"{key.region}: sweep returned no resources at all (index may be empty)"
    tr = (rtype, key.region)
    if arns_per_type_region.get(tr, 0) > 0 and matched_per_type_region.get(tr, 0) == 0:
        return f"{rtype}/{key.region}: sweep has {arns_per_type_region[tr]} ARN(s) of this type but none matched a row (mapping suspect)"
    return None
