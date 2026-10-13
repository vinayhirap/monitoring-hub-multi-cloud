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
    reach the script, so database content cannot inject a command.
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


def build_tag_plan(rows, sources=DEFAULT_SOURCES, now=None):
    """rows: resources joined with their account (see api/environments.py).

    -> {"script": str, "resources": int, "commands": int, "accounts": int,
        "not_covered": {resource_type: count}, "skipped_invalid": int}
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

    groups = {}                       # (account pk, region, env) -> [resource ids]
    skipped = 0
    for (acct, rid), env in chosen.items():
        row = by_key[(acct, rid)]
        region = row.get("region") or ""
        if not (_ID.match(rid) and _REGION.match(region) and _ACCOUNT.match(str(row.get("account_id") or ""))):
            skipped += 1
            continue
        groups.setdefault((acct, region, env), []).append(rid)

    lines = [
        "#!/usr/bin/env bash",
        f"# CloudOps tagging plan, generated {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "# Review before running. Each command adds or overwrites ONLY the Environment tag;",
        "# every other tag on the resource is left as it is.",
        "# Run each account block with credentials for THAT account. The check at the top of the",
        "# block stops the script if the credentials belong to a different account.",
        "set -euo pipefail",
        "",
    ]
    commands = 0
    accounts = set()
    last_acct = None
    for (acct, region, env), ids in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
        row = by_key[(acct, ids[0])]
        if acct != last_acct:
            name = re.sub(r"[^A-Za-z0-9 ._-]", "", str(row.get("account_name") or ""))
            lines += [f"# ===== Account {name} ({row['account_id']}) =====",
                      f"test \"$(aws sts get-caller-identity --query Account --output text)\" = \"{row['account_id']}\" "
                      f"|| {{ echo 'Wrong AWS account: expected {row['account_id']}'; exit 1; }}", ""]
            last_acct = acct
            accounts.add(acct)
        ids = sorted(ids)
        lines.append(f"# {region}: Environment={env}, {len(ids)} resource(s)")
        for i in range(0, len(ids), _BATCH):
            chunk = ids[i:i + _BATCH]
            lines.append(f"aws ec2 create-tags --region {region} --resources {' '.join(chunk)} "
                         f"--tags Key=Environment,Value={env}")
            commands += 1
        lines.append("")
    if not commands:
        lines.append("# Nothing to export: no EC2 instance or EBS volume has a CloudOps decision "
                     "and a missing or non-standard Environment tag.")
    if not_covered:
        lines += ["", "# Not covered by this script (tag in the console or with Tag Editor): "
                  + ", ".join(f"{t} x{n}" for t, n in sorted(not_covered.items()))]
    return {"script": "\n".join(lines) + "\n", "resources": len(chosen) - skipped, "commands": commands,
            "accounts": len(accounts), "not_covered": not_covered, "skipped_invalid": skipped}


def _tags_parent(row):
    d = ec._tags_dict(row.get("tags"))
    p = d.get("parent_ec2")
    return p if isinstance(p, str) and p else None
