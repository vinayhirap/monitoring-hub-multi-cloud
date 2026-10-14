# app/collector/tag_plan.py
"""
Builds the AWS tagging script that turns CloudOps decisions into real AWS tags.

CloudOps only READS AWS: it does not hold permission to change tags, and that is a feature. A
resource that someone classified in CloudOps (manually, or from its name, or from what it is
attached to) still has no Environment tag in AWS, so nothing else in the organisation can see
the decision. This module produces a reviewable shell script a person runs with their OWN AWS
credentials, per account, to write the tag.

Scope, deliberately narrow so every line can be trusted:
  * EC2 instances and EBS volumes only, through `aws ec2 create-tags`, which adds or overwrites
    only the keys named and leaves every other tag alone, and needs no ARN.
  * Only resources whose environment CloudOps decided from manual / name / related evidence
    (account defaults and unknowns are not exported), and whose AWS tag state is missing /
    invalid / nonstandard.
  * An attached EBS volume is included when its instance is, because its own tags are not
    known (see environment_classifier.tags_known).
  * Every id, region and account number is validated against a strict pattern before it can
    reach the script, so database content cannot inject a command. Names appear only as sanitised comments.
Other resource types are counted but not exported; tag them in the console or with the Tag Editor.
"""
import re
from datetime import datetime, timezone

from app import environment_classifier as ec

DEFAULT_SOURCES = ("manual", "name", "related")
EXPORT_TYPES = ("ec2", "ebs")
_BATCH = 100

_ID = re.compile(r"^(i|vol)-[0-9a-f]{8,17}$")
_REGION = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d$")
_ACCOUNT = re.compile(r"^\d{12}$")


def _eligible(row, sources):
    return (row.get("env_resolved") in ec.TIERS
            and row.get("env_source") in sources
            and row.get("tag_state") in ec.NEEDS_TAGGING)


def _clean(text, limit=60):
    """A resource name as a harmless comment fragment: printable ASCII only, no line breaks."""
    return re.sub(r"[^A-Za-z0-9 ._:@+=-]", "", str(text or ""))[:limit].strip()


def build_tag_plan(rows, sources=DEFAULT_SOURCES, now=None, env=None, region=None, resource_type=None):
    """rows: resources joined with their account (see api/environments.py).

    env / region / resource_type narrow the export to what the person filtered the page by. They are applied
    AFTER an attached volume has been matched to its instance, so filtering by "ebs" still finds volumes
    that follow an instance.

    -> {"script": str, "resources": int, "commands": int, "accounts": int,
        "not_covered": {resource_type: count}, "skipped_invalid": int,
        "by_env": {env: n}, "groups": [{account_id, account_name, region, env, count}]}
    """
    now = now or datetime.now(timezone.utc)
    sources = tuple(sources)
    by_key = {(r["aws_account_id"], r["resource_id"]): r for r in rows}

    chosen = {}                       # (account pk, resource_id) -> env value
    not_covered = {}
    for r in rows:
        if r.get("resource_type") == "ec2" and _eligible(r, sources):
            chosen[(r["aws_account_id"], r["resource_id"])] = r["env_resolved"]
    for r in rows:
        rtype = r.get("resource_type")
        if rtype == "ebs":
            parent = _tags_parent(r)
            if parent:                                    # attached: follows its instance
                if (r["aws_account_id"], parent) in chosen:
                    chosen[(r["aws_account_id"], r["resource_id"])] = chosen[(r["aws_account_id"], parent)]
            elif _eligible(r, sources):
                chosen[(r["aws_account_id"], r["resource_id"])] = r["env_resolved"]
        elif rtype not in EXPORT_TYPES and r.get("env_resolved") in ec.TIERS \
                and r.get("env_source") in sources:
            not_covered[rtype] = not_covered.get(rtype, 0) + 1

    if env or region or resource_type:
        chosen = {k: v for k, v in chosen.items()
                  if (not env or v == env)
                  and (not region or by_key[k].get("region") == region)
                  and (not resource_type or by_key[k].get("resource_type") == resource_type)}

    groups = {}                       # (account pk, region, env) -> [resource ids]
    skipped = 0
    for (acct, rid), tier in chosen.items():
        row = by_key[(acct, rid)]
        reg = row.get("region") or ""
        if not (_ID.match(rid) and _REGION.match(reg) and _ACCOUNT.match(str(row.get("account_id") or ""))):
            skipped += 1
            continue
        groups.setdefault((acct, reg, tier), []).append(rid)

    ordered = sorted(groups.items(), key=lambda kv: (str(by_key[(kv[0][0], kv[1][0])].get("account_name") or ""),
                                                      kv[0][0], kv[0][1], kv[0][2]))
    by_env = {}
    for (_a, _r, tier), ids in ordered:
        by_env[tier] = by_env.get(tier, 0) + len(ids)
    summary = [{"account_id": str(by_key[(a, ids[0])]["account_id"]),
                "account_name": _clean(by_key[(a, ids[0])].get("account_name")),
                "region": reg, "env": tier, "count": len(ids)}
               for (a, reg, tier), ids in ordered]

    # one function per account so each block can only ever run against that account
    accts = []                        # (account pk, 12-digit id, clean name)
    for (a, _reg, _tier), ids in ordered:
        if not accts or accts[-1][0] != a:
            row = by_key[(a, ids[0])]
            accts.append((a, str(row["account_id"]), _clean(row.get("account_name"), 40)))

    total = sum(by_env.values())
    lines = [
        "#!/usr/bin/env bash",
        f"# CloudOps tagging plan, generated {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "#",
        "# WHAT IT DOES",
        "#   Adds or overwrites ONLY the Environment tag on the resources below. Every other tag is left as it is.",
        "#   It covers EC2 instances and EBS volumes. CloudOps never runs it; you do, with your own AWS credentials.",
        "#",
        "# WHERE TO RUN IT",
        "#   Sign in to the AWS console of the account named in a block below and open AWS CloudShell (any region),",
        "#   or use a terminal with that account's credentials. Paste or upload this file and run it there:",
        "#       bash cloudops-tagging-plan.sh",
        "#   Run it once per account. A block only runs when the credentials belong to its account; any other",
        "#   account's block is never run, and if the credentials match none of them the script stops at once.",
        "#   The identity needs the ec2:CreateTags permission (and ec2:DescribeTags to verify).",
        "#   To preview without changing anything:   DRY_RUN=1 bash cloudops-tagging-plan.sh",
        "#",
        "# SUMMARY  (" + (", ".join(f"{t} {n}" for t, n in sorted(by_env.items())) or "nothing to tag")
        + f"; {total} resource(s) in {len(accts)} account(s))",
    ]
    per = {}
    for g in summary:
        per.setdefault((g["account_name"], g["account_id"]), []).append(g)
    for (aname, aid), gs in per.items():
        lines.append(f"#   {aname} ({aid}): " + ", ".join(f"{g['region']} {g['env']} {g['count']}" for g in gs))
    lines += ["",
              "set -euo pipefail",
              'DRY_RUN="${DRY_RUN:-0}"',
              'run() { if [ "$DRY_RUN" = "1" ]; then echo "[dry run] $*"; else "$@"; fi; }',
              ""]

    commands = 0
    last_acct = None
    for (acct, reg, tier), ids in ordered:
        if acct != last_acct:
            if last_acct is not None:
                lines += ["}", ""]
            aid = next(x[1] for x in accts if x[0] == acct)
            aname = next(x[2] for x in accts if x[0] == acct)
            lines += [f"# ===== Account {aname} ({aid}) =====",
                      f"tag_account_{aid}() {{"]
            last_acct = acct
        ids = sorted(ids)
        lines.append(f"  # {reg}: Environment={tier}, {len(ids)} resource(s)")
        for rid in ids:
            nm = _clean(by_key[(acct, rid)].get("name"))
            lines.append(f"  #   {rid}" + (f"  {nm}" if nm and nm != rid else ""))
        for i in range(0, len(ids), _BATCH):
            chunk = ids[i:i + _BATCH]
            lines.append(f"  run aws ec2 create-tags --region {reg} --resources {' '.join(chunk)} "
                         f"--tags Key=Environment,Value={tier}")
            commands += 1
        lines.append("")
    if last_acct is not None:
        lines += ["}", ""]
        lines += ['CURRENT_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"',
                  'case "$CURRENT_ACCOUNT" in']
        for _a, aid, aname in accts:
            lines.append(f"  {aid}) echo 'Tagging account {aname} ({aid})'; tag_account_{aid} ;;")
        lines += ["  *) echo \"These credentials are for account $CURRENT_ACCOUNT, which has no block in this script. "
                  "Expected one of: " + ", ".join(a[1] for a in accts) + "\"; exit 1 ;;",
                  "esac",
                  'echo "Done. Verify with: aws ec2 describe-tags --filters Name=key,Values=Environment '
                  'Name=resource-id,Values=<id> --region <region>"', ""]
    else:
        lines.append("# Nothing to export: no EC2 instance or EBS volume matches, i.e. has a CloudOps decision "
                     "and a missing or non-standard Environment tag.")
    if not_covered:
        lines += ["# Not covered by this script (tag in the console or with Tag Editor): "
                  + ", ".join(f"{t} x{n}" for t, n in sorted(not_covered.items()))]
    return {"script": "\n".join(lines) + "\n", "resources": total, "commands": commands,
            "accounts": len(accts), "not_covered": not_covered, "skipped_invalid": skipped,
            "by_env": by_env, "groups": summary}


def _tags_parent(row):
    d = ec._tags_dict(row.get("tags"))
    p = d.get("parent_ec2")
    return p if isinstance(p, str) and p else None
