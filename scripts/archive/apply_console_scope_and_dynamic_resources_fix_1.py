#!/usr/bin/env python3
"""
apply_console_scope_and_dynamic_resources_fix.py

Fixes three related issues in monitoring-hub-multi-cloud:

  1. AWS Console links (Alerts page + Services page) always used a
     generic, shared federated identity ("monitoring-hub-session" /
     "monitoring-hub-self") with a blanket ReadOnlyAccess policy. Every
     click looked identical in AWS CloudTrail no matter who in the
     monitoring hub triggered it, and every session could read every
     AWS service, not just the one resource being viewed.

     Fix: the federated session is now named after the actual logged-in
     monitoring-hub user (so CloudTrail shows WHO opened it) and scoped
     down, via an IAM session policy, to read-only access for just the
     service (and, where AWS IAM supports resource-level permissions,
     the specific resource) being viewed. The app has no way to hand
     out someone's personal AWS IAM keys (it never stores any), so this
     is the closest real equivalent: a distinct, audit-attributable,
     least-privilege session per person and per click.

  2. On the Services page, some service tiles opened their resource
     page correctly, others silently redirected to /overview. Root
     cause: ServiceList.jsx shows a tile for ANY service with at least
     one metric enabled (including AWS "extended"/"directory" services
     and GCP/Azure services), but ServiceDetail/App.jsx only implements
     routes + live-data endpoints for the 7 AWS core services
     (ec2/ebs/rds/lambda/s3/elb/ecs). Clicking anything else hit the
     catch-all route and bounced to /overview.

  3. Tiles were shown based purely on "is a metric enabled for this
     service", not on whether any resource of that type actually
     exists in the account yet (e.g. an ECS tile showed up with zero
     real ECS resources in the account).

     Fix for both 2 and 3 together: ServiceList.jsx now also fetches
     each account's live resource counts (already computed cheaply by
     the existing /api/live/accounts endpoint via VictoriaMetrics/
     boto3-describe collectors — no new AWS calls) and only shows a
     tile when that service both has a metric enabled AND has at least
     one real resource right now. Since only the 7 core AWS services
     have a working resource count + detail page today, this also
     eliminates the dead-link problem as a side effect: nothing gets
     rendered as a clickable tile unless it can actually be opened.

Usage:
    python apply_console_scope_and_dynamic_resources_fix.py [repo_root]

Idempotent: safe to re-run. Backs up every file it touches to
"<file>.bak.pre-console-scope-dynamic-fix" (only on the FIRST run —
later runs won't overwrite that backup). Reverts all changes
automatically if any patched Python file fails py_compile.
"""
import ast
import py_compile
import shutil
import sys
from pathlib import Path

BAK_SUFFIX = ".bak.pre-console-scope-dynamic-fix"


class PatchError(Exception):
    pass


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def backup(path: Path):
    bak = path.with_name(path.name + BAK_SUFFIX)
    if not bak.exists():
        shutil.copy2(path, bak)


def apply_replacements(path: Path, replacements, already_applied_marker=None):
    """
    replacements: list of (old, new, label) tuples.
    If already_applied_marker is found in the file, skip (idempotent re-run).
    Returns True if the file was changed.
    """
    text = read(path)
    if already_applied_marker and already_applied_marker in text:
        print(f"  SKIP  {path} (already patched)")
        return False

    backup(path)
    changed = 0
    for old, new, label in replacements:
        count = text.count(old)
        if count == 0:
            raise PatchError(f"{path}: pattern not found for '{label}'")
        if count > 1:
            raise PatchError(f"{path}: pattern for '{label}' matches {count} times, expected 1")
        text = text.replace(old, new, 1)
        changed += 1
    path.write_text(text, encoding="utf-8")
    print(f"  OK    {path} ({changed} edit{'s' if changed != 1 else ''})")
    return True


def main():
    repo_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    print(f"Repo root: {repo_root}")

    touched_py_files = []
    any_change = False

    # ------------------------------------------------------------------
    # 1. app/aws/sts.py — session naming + session-policy plumbing
    # ------------------------------------------------------------------
    p = repo_root / "app" / "aws" / "sts.py"
    marker = "_sanitize_session_name"
    replacements = [
        (
            "import time\n\nimport boto3\nfrom botocore.exceptions import ClientError\n",
            "import re\nimport time\n\nimport boto3\nfrom botocore.exceptions import ClientError\n",
            "sts.py: add re import",
        ),
        (
            'def get_self_federation_session():\n'
            '    """\n'
            '    Mints a session-scoped, read-only credential set via STS\n'
            '    GetFederationToken using the server\'s OWN identity — no\n'
            '    cross-account AssumeRole, no pre-created target role. Only valid\n'
            '    when the target AWS account IS the server\'s own account (see\n'
            '    get_own_account_id()); for any other account this still requires a\n'
            '    role_arn, which is a hard AWS security boundary, not a gap in this\n'
            '    function.\n'
            '    """\n'
            '    sts = boto3.client("sts")\n'
            '    response = sts.get_federation_token(\n'
            '        Name="monitoring-hub-self",\n'
            '        PolicyArns=[{"arn": "arn:aws:iam::aws:policy/ReadOnlyAccess"}],\n'
            '        DurationSeconds=3600,\n'
            '    )\n'
            '    credentials = response["Credentials"]\n'
            '    return boto3.Session(\n'
            '        aws_access_key_id=credentials["AccessKeyId"],\n'
            '        aws_secret_access_key=credentials["SecretAccessKey"],\n'
            '        aws_session_token=credentials["SessionToken"],\n'
            '    )\n',

            'def _sanitize_session_name(raw: str | None) -> str | None:\n'
            '    """\n'
            '    Turns the monitoring-hub username of the person requesting a\n'
            '    console link into a valid STS RoleSessionName, so the AWS\n'
            '    CloudTrail record for that console session shows WHO in the\n'
            '    monitoring hub opened it, instead of a shared generic name used\n'
            '    by every user. The app has no way to hand out that person\'s own\n'
            '    AWS IAM credentials (it never stores any), so per-user session\n'
            '    naming plus a scoped session policy (see\n'
            '    app.aws.federation.build_scoped_session_policy) is the closest\n'
            '    real equivalent available: a distinct, audit-attributable,\n'
            '    least-privilege session per person and per resource.\n'
            '\n'
            '    STS requires RoleSessionName to match [\\\\w+=,.@-]{2,64}.\n'
            '    """\n'
            '    if not raw:\n'
            '        return None\n'
            '    cleaned = re.sub(r"[^\\w+=,.@-]", "-", raw.strip())\n'
            '    cleaned = cleaned.strip("-")[:55]  # leave room for "mh-" prefix, cap at 64 total\n'
            '    if not cleaned:\n'
            '        return None\n'
            '    return f"mh-{cleaned}"\n'
            '\n'
            '\n'
            'def get_self_federation_session(session_name: str | None = None,\n'
            '                                policy: str | None = None):\n'
            '    """\n'
            '    Mints a session-scoped, read-only credential set via STS\n'
            '    GetFederationToken using the server\'s OWN identity — no\n'
            '    cross-account AssumeRole, no pre-created target role. Only valid\n'
            '    when the target AWS account IS the server\'s own account (see\n'
            '    get_own_account_id()); for any other account this still requires a\n'
            '    role_arn, which is a hard AWS security boundary, not a gap in this\n'
            '    function.\n'
            '\n'
            '    `session_name` (see _sanitize_session_name) attributes the\n'
            '    resulting CloudTrail activity to the actual monitoring-hub user\n'
            '    who requested it, instead of the previous shared\n'
            '    "monitoring-hub-self" name. `policy` is an optional IAM session\n'
            '    policy JSON string (see federation.build_scoped_session_policy)\n'
            '    that further narrows the session below ReadOnlyAccess — AWS\n'
            '    always takes the INTERSECTION of PolicyArns and Policy, so this\n'
            '    can only restrict, never expand, what the session can do.\n'
            '    """\n'
            '    sts = boto3.client("sts")\n'
            '    kwargs = {\n'
            '        "Name": session_name or "monitoring-hub-self",\n'
            '        "PolicyArns": [{"arn": "arn:aws:iam::aws:policy/ReadOnlyAccess"}],\n'
            '        "DurationSeconds": 3600,\n'
            '    }\n'
            '    if policy:\n'
            '        kwargs["Policy"] = policy\n'
            '    response = sts.get_federation_token(**kwargs)\n'
            '    credentials = response["Credentials"]\n'
            '    return boto3.Session(\n'
            '        aws_access_key_id=credentials["AccessKeyId"],\n'
            '        aws_secret_access_key=credentials["SecretAccessKey"],\n'
            '        aws_session_token=credentials["SessionToken"],\n'
            '    )\n',
            "sts.py: get_self_federation_session + sanitize helper",
        ),
        (
            'def assume_role(role_arn: str, external_id: str | None = None):\n'
            '    sts = boto3.client("sts")\n'
            '\n'
            '    params = {\n'
            '        "RoleArn": role_arn,\n'
            '        "RoleSessionName": "monitoring-hub-session"\n'
            '    }\n'
            '\n'
            '    if external_id:\n'
            '        params["ExternalId"] = external_id\n'
            '\n'
            '    response = sts.assume_role(**params)\n',

            'def assume_role(role_arn: str, external_id: str | None = None,\n'
            '                 session_name: str | None = None, policy: str | None = None):\n'
            '    """\n'
            '    `session_name` attributes the resulting CloudTrail activity to\n'
            '    the actual monitoring-hub user who requested it (see\n'
            '    _sanitize_session_name), instead of the previous shared\n'
            '    "monitoring-hub-session" name used for every operator and every\n'
            '    scheduled job alike. `policy` is an optional IAM session policy\n'
            '    JSON string (see federation.build_scoped_session_policy) — AWS\n'
            '    takes the INTERSECTION of the role\'s own permissions and this\n'
            '    policy, so it can only restrict, never expand, access.\n'
            '    """\n'
            '    sts = boto3.client("sts")\n'
            '\n'
            '    params = {\n'
            '        "RoleArn": role_arn,\n'
            '        "RoleSessionName": session_name or "monitoring-hub-session",\n'
            '    }\n'
            '\n'
            '    if external_id:\n'
            '        params["ExternalId"] = external_id\n'
            '    if policy:\n'
            '        params["Policy"] = policy\n'
            '\n'
            '    response = sts.assume_role(**params)\n',
            "sts.py: assume_role session_name/policy params",
        ),
    ]
    if apply_replacements(p, replacements, already_applied_marker=marker):
        any_change = True
    touched_py_files.append(p)

    # ------------------------------------------------------------------
    # 2. app/aws/federation.py — scoped session policy + threading
    #    requested_by/service/resource_id/region through to STS calls
    # ------------------------------------------------------------------
    p = repo_root / "app" / "aws" / "federation.py"
    marker = "build_scoped_session_policy"
    replacements = [
        (
            "from app.aws.sts import assume_role, get_own_account_id, get_self_federation_session\n",
            "from app.aws.sts import assume_role, get_own_account_id, get_self_federation_session\n"
            "from app.aws.sts import _sanitize_session_name\n",
            "federation.py: import sanitize helper",
        ),
        (
            'def build_federated_console_url(role_arn: str | None, external_id: str | None,\n'
            '                                 destination: str,\n'
            '                                 target_account_id: str | None = None) -> str:\n'
            '    """\n'
            '    Exchanges credentials for a sign-in token and returns a login URL that\n'
            '    drops the user directly onto `destination` inside the CORRECT account —\n'
            '    no dependence on whatever account the browser is currently signed into.\n'
            '\n'
            '    Credential path is chosen automatically:\n'
            '      - role_arn set                                   -> AssumeRole (cross-account)\n'
            '      - role_arn empty, target_account_id == own account -> GetFederationToken\n'
            '                                                             (self-federation,\n'
            '                                                             zero config)\n'
            '      - role_arn empty, target_account_id != own account -> NoConsoleCredentialsError\n'
            '    """\n'
            '    role_arn = (role_arn or "").strip()\n'
            '\n'
            '    if role_arn:\n'
            '        session = assume_role(role_arn, external_id)\n'
            '    else:\n'
            '        own_account_id = get_own_account_id()\n'
            '        if target_account_id and own_account_id and str(target_account_id) == str(own_account_id):\n'
            '            logger.info(\n'
            '                "Console link for account %s uses self-federation (server\'s own account, no role_arn needed)",\n'
            '                target_account_id,\n'
            '            )\n'
            '            session = get_self_federation_session()\n'
            '        else:\n'
            '            raise NoConsoleCredentialsError(\n'
            '                "No AWS role configured for this account, and it is not "\n'
            '                "the server\'s own AWS account, so no automatic credential "\n'
            '                "path is available. Set an IAM Role ARN for this account "\n'
            '                "in Settings to enable console access."\n'
            '            )\n',

            'def _service_read_actions(service: str) -> list[str]:\n'
            '    """\n'
            '    Minimal read-only IAM actions needed to view/monitor ONE AWS\n'
            '    service in the console, used to build a session policy that\n'
            '    narrows a federated session down to just this service — instead\n'
            '    of the previous blanket ReadOnlyAccess, which grants read access\n'
            '    to every AWS service regardless of which alert/resource the\n'
            '    person actually clicked into.\n'
            '    """\n'
            '    common = [\n'
            '        "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics",\n'
            '        "cloudwatch:ListMetrics", "cloudwatch:DescribeAlarms",\n'
            '        "tag:GetResources", "tag:GetTagKeys", "tag:GetTagValues",\n'
            '        "sts:GetCallerIdentity",\n'
            '    ]\n'
            '    per_service = {\n'
            '        "ec2":    ["ec2:Describe*", "ec2:GetConsoleOutput", "ec2:GetConsoleScreenshot"],\n'
            '        "ebs":    ["ec2:Describe*"],\n'
            '        "rds":    ["rds:Describe*", "rds:ListTagsForResource"],\n'
            '        "lambda": ["lambda:Get*", "lambda:List*"],\n'
            '        "s3":     ["s3:GetBucket*", "s3:ListBucket", "s3:GetObject", "s3:ListAllMyBuckets"],\n'
            '        "elb":    ["elasticloadbalancing:Describe*"],\n'
            '        "ecs":    ["ecs:Describe*", "ecs:List*"],\n'
            '    }\n'
            '    extra = per_service.get((service or "").lower())\n'
            '    if not extra:\n'
            '        return []\n'
            '    return common + extra\n'
            '\n'
            '\n'
            'def _service_resource_arns(service: str, resource_id: str | None, region: str | None,\n'
            '                            account_id: str | None, resource_name: str | None = None,\n'
            '                            ecs_service_name: str | None = None) -> list[str] | None:\n'
            '    """\n'
            '    Best-effort ARN(s) for the SPECIFIC resource being viewed, so the\n'
            '    session policy\'s Resource element can be scoped to just that\n'
            '    resource wherever AWS IAM actually supports resource-level\n'
            '    permissions for the relevant read actions. Returns None (caller\n'
            '    falls back to "*") for services where the Describe/List calls\n'
            '    involved are account/region-wide by design in AWS IAM — e.g.\n'
            '    ec2:DescribeInstances has no resource-level permission support —\n'
            '    which is a hard AWS limitation, not a gap in this function.\n'
            '    """\n'
            '    if not resource_id or not account_id:\n'
            '        return None\n'
            '    svc = (service or "").lower()\n'
            '    region = region or "us-east-1"\n'
            '    if svc == "s3":\n'
            '        bucket = resource_id\n'
            '        return [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]\n'
            '    if svc == "lambda":\n'
            '        return [f"arn:aws:lambda:{region}:{account_id}:function:{resource_id}"]\n'
            '    if svc == "rds":\n'
            '        return [f"arn:aws:rds:{region}:{account_id}:db:{resource_id}"]\n'
            '    if svc == "ecs":\n'
            '        cluster = resource_name or resource_id\n'
            '        arns = [f"arn:aws:ecs:{region}:{account_id}:cluster/{cluster}"]\n'
            '        if ecs_service_name:\n'
            '            arns.append(f"arn:aws:ecs:{region}:{account_id}:service/{cluster}/{ecs_service_name}")\n'
            '        return arns\n'
            '    return None\n'
            '\n'
            '\n'
            'def build_scoped_session_policy(service: str | None, resource_id: str | None = None,\n'
            '                                 region: str | None = None,\n'
            '                                 target_account_id: str | None = None,\n'
            '                                 resource_name: str | None = None,\n'
            '                                 ecs_service_name: str | None = None) -> str | None:\n'
            '    """\n'
            '    Builds an IAM session-policy JSON string that narrows a federated\n'
            '    console session to read-only access for ONE service — and, where\n'
            '    AWS IAM supports it, ONE specific resource — instead of the\n'
            '    previous blanket ReadOnlyAccess across every AWS service. Returns\n'
            '    None if `service` isn\'t recognized, so callers fall back to\n'
            '    whichever base policy they already had.\n'
            '\n'
            '    This is a real IAM session policy: AWS enforces the\n'
            '    INTERSECTION of this policy and the underlying role/user\'s own\n'
            '    permissions, so it can only ever narrow access further — never\n'
            '    grant anything the base identity didn\'t already have.\n'
            '    """\n'
            '    if not service:\n'
            '        return None\n'
            '    actions = _service_read_actions(service)\n'
            '    if not actions:\n'
            '        return None\n'
            '    arns = _service_resource_arns(service, resource_id, region, target_account_id,\n'
            '                                   resource_name, ecs_service_name)\n'
            '    policy = {\n'
            '        "Version": "2012-10-17",\n'
            '        "Statement": [{\n'
            '            "Effect":   "Allow",\n'
            '            "Action":   sorted(set(actions)),\n'
            '            "Resource": arns if arns else "*",\n'
            '        }],\n'
            '    }\n'
            '    body = json.dumps(policy)\n'
            '    # STS session-policy documents are capped at 2048 chars — fall\n'
            '    # back to no extra scoping (base policy still applies) rather\n'
            '    # than send something AWS would reject outright.\n'
            '    if len(body) > 2000:\n'
            '        logger.warning("Scoped session policy for %s too large (%d chars) — skipping extra scoping", service, len(body))\n'
            '        return None\n'
            '    return body\n'
            '\n'
            '\n'
            'def build_federated_console_url(role_arn: str | None, external_id: str | None,\n'
            '                                 destination: str,\n'
            '                                 target_account_id: str | None = None,\n'
            '                                 requested_by: str | None = None,\n'
            '                                 service: str | None = None,\n'
            '                                 resource_id: str | None = None,\n'
            '                                 region: str | None = None,\n'
            '                                 resource_name: str | None = None,\n'
            '                                 ecs_service_name: str | None = None) -> str:\n'
            '    """\n'
            '    Exchanges credentials for a sign-in token and returns a login URL that\n'
            '    drops the user directly onto `destination` inside the CORRECT account —\n'
            '    no dependence on whatever account the browser is currently signed into.\n'
            '\n'
            '    Credential path is chosen automatically:\n'
            '      - role_arn set                                   -> AssumeRole (cross-account)\n'
            '      - role_arn empty, target_account_id == own account -> GetFederationToken\n'
            '                                                             (self-federation,\n'
            '                                                             zero config)\n'
            '      - role_arn empty, target_account_id != own account -> NoConsoleCredentialsError\n'
            '\n'
            '    `requested_by` (the monitoring-hub username of whoever clicked\n'
            '    "Console") and `service`/`resource_id`/`region`/etc. (the\n'
            '    resource actually being viewed) are used to attribute the\n'
            '    resulting AWS session to that specific person and narrow it to\n'
            '    that specific resource, instead of every click in the app\n'
            '    sharing one generic, blanket-ReadOnlyAccess identity. See\n'
            '    _sanitize_session_name and build_scoped_session_policy.\n'
            '    """\n'
            '    role_arn = (role_arn or "").strip()\n'
            '\n'
            '    session_name   = _sanitize_session_name(requested_by)\n'
            '    session_policy = build_scoped_session_policy(\n'
            '        service, resource_id, region, target_account_id,\n'
            '        resource_name, ecs_service_name,\n'
            '    )\n'
            '\n'
            '    if role_arn:\n'
            '        session = assume_role(role_arn, external_id,\n'
            '                               session_name=session_name, policy=session_policy)\n'
            '    else:\n'
            '        own_account_id = get_own_account_id()\n'
            '        if target_account_id and own_account_id and str(target_account_id) == str(own_account_id):\n'
            '            logger.info(\n'
            '                "Console link for account %s uses self-federation (server\'s own account, no role_arn needed), "\n'
            '                "requested_by=%s service=%s",\n'
            '                target_account_id, requested_by, service,\n'
            '            )\n'
            '            session = get_self_federation_session(session_name=session_name, policy=session_policy)\n'
            '        else:\n'
            '            raise NoConsoleCredentialsError(\n'
            '                "No AWS role configured for this account, and it is not "\n'
            '                "the server\'s own AWS account, so no automatic credential "\n'
            '                "path is available. Set an IAM Role ARN for this account "\n'
            '                "in Settings to enable console access."\n'
            '            )\n',
            "federation.py: scoped policy builder + build_federated_console_url signature",
        ),
    ]
    if apply_replacements(p, replacements, already_applied_marker=marker):
        any_change = True
    touched_py_files.append(p)

    # ------------------------------------------------------------------
    # 3. app/providers/base.py — interface gains requested_by
    # ------------------------------------------------------------------
    p = repo_root / "app" / "providers" / "base.py"
    replacements = [
        (
            "    def get_console_url(self, account: dict, resource_id: str, region: str,\n"
            "                         service: str | None = None,\n"
            "                         resource_name: str | None = None,\n"
            "                         ecs_service_name: str | None = None) -> str:\n"
            '        """\n'
            "        Return a deep link into this provider's web console for the given\n"
            "        resource, scoped to the correct account/subscription/project.\n"
            "\n"
            '        `service` is the normalized/native resource-type key (e.g. "ec2",\n'
            '        "ecs") and lets the provider dispatch precisely instead of\n'
            "        guessing from `resource_id`'s shape. `resource_name` and\n"
            "        `ecs_service_name` are optional extra identifiers some services\n"
            "        need for a fully-specific deep link (AWS ELB search-by-name and\n"
            "        ECS cluster>service, respectively) — providers that don't need\n"
            "        them can ignore the arguments.\n"
            '        """\n'
            "        raise NotImplementedError\n",

            "    def get_console_url(self, account: dict, resource_id: str, region: str,\n"
            "                         service: str | None = None,\n"
            "                         resource_name: str | None = None,\n"
            "                         ecs_service_name: str | None = None,\n"
            "                         requested_by: str | None = None) -> str:\n"
            '        """\n'
            "        Return a deep link into this provider's web console for the given\n"
            "        resource, scoped to the correct account/subscription/project.\n"
            "\n"
            '        `service` is the normalized/native resource-type key (e.g. "ec2",\n'
            '        "ecs") and lets the provider dispatch precisely instead of\n'
            "        guessing from `resource_id`'s shape. `resource_name` and\n"
            "        `ecs_service_name` are optional extra identifiers some services\n"
            "        need for a fully-specific deep link (AWS ELB search-by-name and\n"
            "        ECS cluster>service, respectively) — providers that don't need\n"
            "        them can ignore the arguments.\n"
            "\n"
            "        `requested_by` is the monitoring-hub username of whoever asked\n"
            "        for this link. Providers that mint their own AWS-style\n"
            "        federated/impersonated session (currently just AWS) use it to\n"
            "        attribute that session to the actual person instead of a\n"
            "        shared generic identity; providers that deep-link straight into\n"
            "        the target cloud's own portal (Azure, GCP) can ignore it, since\n"
            "        those portals already use the operator's own signed-in session.\n"
            '        """\n'
            "        raise NotImplementedError\n",
            "base.py: add requested_by to interface",
        ),
    ]
    if apply_replacements(p, replacements, already_applied_marker="requested_by: str | None = None) -> str:"):
        any_change = True
    touched_py_files.append(p)

    # ------------------------------------------------------------------
    # 4. app/providers/aws/provider.py — thread requested_by + resource
    #    context into build_federated_console_url
    # ------------------------------------------------------------------
    p = repo_root / "app" / "providers" / "aws" / "provider.py"
    replacements = [
        (
            "    def get_console_url(self, account: dict, resource_id: str, region: str,\n"
            "                         service: str | None = None,\n"
            "                         resource_name: str | None = None,\n"
            "                         ecs_service_name: str | None = None) -> str:\n"
            "        from app.aws.federation import (\n"
            "            build_federated_console_url,\n"
            "            resource_console_destination,\n"
            "        )\n"
            "\n"
            "        destination = resource_console_destination(\n"
            "            service, resource_id, region,\n"
            "            resource_name=resource_name, ecs_service_name=ecs_service_name,\n"
            "        )\n"
            "        return build_federated_console_url(\n"
            '            account.get("role_arn"), account.get("external_id"), destination,\n'
            '            target_account_id=account.get("account_id"),\n'
            "        )\n",

            "    def get_console_url(self, account: dict, resource_id: str, region: str,\n"
            "                         service: str | None = None,\n"
            "                         resource_name: str | None = None,\n"
            "                         ecs_service_name: str | None = None,\n"
            "                         requested_by: str | None = None) -> str:\n"
            "        from app.aws.federation import (\n"
            "            build_federated_console_url,\n"
            "            resource_console_destination,\n"
            "        )\n"
            "\n"
            "        destination = resource_console_destination(\n"
            "            service, resource_id, region,\n"
            "            resource_name=resource_name, ecs_service_name=ecs_service_name,\n"
            "        )\n"
            "        return build_federated_console_url(\n"
            '            account.get("role_arn"), account.get("external_id"), destination,\n'
            '            target_account_id=account.get("account_id"),\n'
            "            requested_by=requested_by,\n"
            "            service=service, resource_id=resource_id, region=region,\n"
            "            resource_name=resource_name, ecs_service_name=ecs_service_name,\n"
            "        )\n",
            "aws/provider.py: thread requested_by + resource context",
        ),
    ]
    if apply_replacements(p, replacements, already_applied_marker="requested_by: str | None = None) -> str:"):
        any_change = True
    touched_py_files.append(p)

    # ------------------------------------------------------------------
    # 5. app/providers/azure/provider.py & gcp/provider.py — accept and
    #    ignore requested_by (interface compatibility only; these
    #    providers deep-link into the target cloud's own portal using
    #    the operator's own browser session, so there's no shared-
    #    identity problem to fix there)
    # ------------------------------------------------------------------
    for prov_path, sig_old in [
        (repo_root / "app" / "providers" / "azure" / "provider.py",
         "    def get_console_url(self, account: dict, resource_id: str, region: str,\n"
         "                         service: str | None = None,\n"
         "                         resource_name: str | None = None,\n"
         "                         ecs_service_name: str | None = None) -> str:\n"),
        (repo_root / "app" / "providers" / "gcp" / "provider.py",
         "    def get_console_url(self, account: dict, resource_id: str, region: str,\n"
         "                         service: str | None = None,\n"
         "                         resource_name: str | None = None,\n"
         "                         ecs_service_name: str | None = None) -> str:\n"),
    ]:
        sig_new = (
            "    def get_console_url(self, account: dict, resource_id: str, region: str,\n"
            "                         service: str | None = None,\n"
            "                         resource_name: str | None = None,\n"
            "                         ecs_service_name: str | None = None,\n"
            "                         requested_by: str | None = None) -> str:\n"
            "        # requested_by unused: this provider deep-links straight into\n"
            "        # its own cloud portal, which already uses the operator's own\n"
            "        # signed-in browser session — no shared/impersonated identity\n"
            "        # to attribute here the way AWS federation needs.\n"
        )
        if apply_replacements(prov_path, [(sig_old, sig_new, f"{prov_path.name}: accept requested_by")],
                               already_applied_marker="requested_by: str | None = None) -> str:"):
            any_change = True
        touched_py_files.append(prov_path)

    # ------------------------------------------------------------------
    # 6. app/api/admin/accounts.py — pass the logged-in user through
    # ------------------------------------------------------------------
    p = repo_root / "app" / "api" / "admin" / "accounts.py"
    replacements = [
        (
            "from fastapi import APIRouter, HTTPException, Body, Query\n"
            "from app.db import get_connection\n",
            "from fastapi import APIRouter, HTTPException, Body, Query, Depends\n"
            "from app.db import get_connection\n"
            "from app.auth.deps import get_current_user\n",
            "admin/accounts.py: import Depends + get_current_user",
        ),
        (
            "@router.get(\"/{account_id}/console-url\")\n"
            "def get_account_console_url(\n"
            "    account_id: int,\n"
            "    service: str = Query(None),\n"
            "    resource_id: str = Query(None),\n"
            "    region: str = Query(None),\n"
            "    resource_name: str = Query(None),\n"
            "    ecs_service_name: str = Query(None),\n"
            "):\n",
            "@router.get(\"/{account_id}/console-url\")\n"
            "def get_account_console_url(\n"
            "    account_id: int,\n"
            "    service: str = Query(None),\n"
            "    resource_id: str = Query(None),\n"
            "    region: str = Query(None),\n"
            "    resource_name: str = Query(None),\n"
            "    ecs_service_name: str = Query(None),\n"
            "    user: dict = Depends(get_current_user),\n"
            "):\n",
            "admin/accounts.py: inject current_user",
        ),
        (
            "        url = provider.get_console_url(\n"
            "            account, resource_id, region,\n"
            "            service=service, resource_name=resource_name,\n"
            "            ecs_service_name=ecs_service_name,\n"
            "        )\n",
            "        url = provider.get_console_url(\n"
            "            account, resource_id, region,\n"
            "            service=service, resource_name=resource_name,\n"
            "            ecs_service_name=ecs_service_name,\n"
            '            requested_by=user["username"],\n'
            "        )\n",
            "admin/accounts.py: pass requested_by",
        ),
    ]
    if apply_replacements(p, replacements, already_applied_marker='requested_by=user["username"],\n        )'):
        any_change = True
    touched_py_files.append(p)

    # ------------------------------------------------------------------
    # 7. app/api/alerts.py — pass the logged-in user + resource context
    # ------------------------------------------------------------------
    p = repo_root / "app" / "api" / "alerts.py"
    replacements = [
        (
            "from fastapi import APIRouter, HTTPException\n"
            "from app.db import get_connection\n",
            "from fastapi import APIRouter, HTTPException, Depends\n"
            "from app.db import get_connection\n"
            "from app.auth.deps import get_current_user\n",
            "alerts.py: import Depends + get_current_user",
        ),
        (
            '@router.get("/{alert_id}/console-url")\n'
            "def get_console_url(alert_id: int):\n",
            '@router.get("/{alert_id}/console-url")\n'
            "def get_console_url(alert_id: int, user: dict = Depends(get_current_user)):\n",
            "alerts.py: inject current_user",
        ),
        (
            "    try:\n"
            "        url = build_federated_console_url(\n"
            '            row.get("role_arn"), row.get("external_id"), destination,\n'
            '            target_account_id=row.get("aws_account_id"),\n'
            "        )\n",
            "    try:\n"
            "        url = build_federated_console_url(\n"
            '            row.get("role_arn"), row.get("external_id"), destination,\n'
            '            target_account_id=row.get("aws_account_id"),\n'
            '            requested_by=user["username"],\n'
            '            service=row.get("resource_type"), resource_id=row["resource"],\n'
            '            region=row["region"], resource_name=row.get("resource_name"),\n'
            "        )\n",
            "alerts.py: pass requested_by + resource context",
        ),
    ]
    if apply_replacements(p, replacements, already_applied_marker='requested_by=user["username"],\n            service=row.get("resource_type")'):
        any_change = True
    touched_py_files.append(p)

    # ------------------------------------------------------------------
    # 8. frontend/src/pages/ServiceList.jsx — dynamic, resource-count-
    #    based tile visibility (fixes both the dead-link redirect and
    #    the "show tile with zero resources" problem)
    # ------------------------------------------------------------------
    p = repo_root / "frontend" / "src" / "pages" / "ServiceList.jsx"
    marker = "RESOURCE_COUNT_FIELD"
    replacements = [
        (
            'import { getAlerts, getAccountMetrics } from "../api/api";',
            'import { getAlerts, getAccountMetrics, getLiveAccounts } from "../api/api";',
            "ServiceList.jsx: import getLiveAccounts",
        ),
        (
            'const PALETTE = ["#2bb3ac", "#38bdf8", "#7c6ee0", "#fbbf24", "#34d399", "#f472b6", "#22c55e", "#f59e0b", "#a78bfa", "#e879f9"];\n',

            'const PALETTE = ["#2bb3ac", "#38bdf8", "#7c6ee0", "#fbbf24", "#34d399", "#f472b6", "#22c55e", "#f59e0b", "#a78bfa", "#e879f9"];\n'
            '\n'
            '// Maps a service key to the live resource-count field returned by\n'
            '// GET /api/live/accounts (already computed cheaply — VictoriaMetrics-\n'
            '// first with free Describe-API fallback, no extra AWS calls beyond\n'
            '// what that endpoint already does for the Overview page). Only\n'
            '// services listed here have both a live count AND a working\n'
            '// ServiceDetail page today — anything else (AWS "extended"/directory\n'
            '// services, or GCP/Azure services once those clouds are actually\n'
            '// deployed) is intentionally left out, so its tile stays hidden\n'
            '// instead of linking to a page that doesn\'t exist yet.\n'
            'const RESOURCE_COUNT_FIELD = {\n'
            '  ec2: "ec2_total", ebs: "ebs_total", rds: "rds_total",\n'
            '  lambda: "lambda_total", s3: "s3_total", elb: "elb_total", ecs: "ecs_total",\n'
            '};\n',
            "ServiceList.jsx: RESOURCE_COUNT_FIELD map",
        ),
        (
            '  const [account, setAccount] = useState(null);\n'
            '  const [groups,  setGroups]  = useState([]);\n'
            '  const [alerts,  setAlerts]  = useState([]);\n'
            '  const [loading, setLoading] = useState(true);\n'
            '\n'
            '  useEffect(() => {\n'
            '    let cancelled = false;\n'
            '    setLoading(true);\n'
            '    fetch(`/api/admin/accounts/${id}`)\n'
            '      .then(r => r.ok ? r.json() : null)\n'
            '      .then(d => { if (d && !cancelled) setAccount(d); })\n'
            '      .catch(console.error);\n'
            '    getAlerts().then(a => { if (!cancelled) setAlerts(Array.isArray(a) ? a : []); }).catch(() => {});\n'
            '    getAccountMetrics(id)\n'
            '      .then(g => { if (!cancelled) setGroups(Array.isArray(g) ? g : []); })\n'
            '      .catch(console.error)\n'
            '      .finally(() => { if (!cancelled) setLoading(false); });\n'
            '    return () => { cancelled = true; };\n'
            '  }, [id]);\n',

            '  const [account, setAccount] = useState(null);\n'
            '  const [groups,  setGroups]  = useState([]);\n'
            '  const [alerts,  setAlerts]  = useState([]);\n'
            '  const [loading, setLoading] = useState(true);\n'
            '  // Live per-service resource counts for THIS account, from the same\n'
            '  // endpoint the Overview page already uses. null while unresolved —\n'
            '  // used to hold tiles back until we can actually confirm resources\n'
            '  // exist, instead of showing them the moment a metric is enabled.\n'
            '  const [liveCounts,   setLiveCounts]   = useState(null);\n'
            '  const [countsLoading, setCountsLoading] = useState(true);\n'
            '\n'
            '  useEffect(() => {\n'
            '    let cancelled = false;\n'
            '    setLoading(true);\n'
            '    fetch(`/api/admin/accounts/${id}`)\n'
            '      .then(r => r.ok ? r.json() : null)\n'
            '      .then(d => { if (d && !cancelled) setAccount(d); })\n'
            '      .catch(console.error);\n'
            '    getAlerts().then(a => { if (!cancelled) setAlerts(Array.isArray(a) ? a : []); }).catch(() => {});\n'
            '    getAccountMetrics(id)\n'
            '      .then(g => { if (!cancelled) setGroups(Array.isArray(g) ? g : []); })\n'
            '      .catch(console.error)\n'
            '      .finally(() => { if (!cancelled) setLoading(false); });\n'
            '    return () => { cancelled = true; };\n'
            '  }, [id]);\n'
            '\n'
            '  useEffect(() => {\n'
            '    let cancelled = false;\n'
            '    setCountsLoading(true);\n'
            '    getLiveAccounts()\n'
            '      .then(list => {\n'
            '        if (cancelled) return;\n'
            '        const mine = (Array.isArray(list) ? list : []).find(a => String(a.id) === String(id));\n'
            '        setLiveCounts(mine || {});\n'
            '      })\n'
            '      .catch(() => { if (!cancelled) setLiveCounts({}); })\n'
            '      .finally(() => { if (!cancelled) setCountsLoading(false); });\n'
            '    return () => { cancelled = true; };\n'
            '  }, [id]);\n',
            "ServiceList.jsx: fetch live resource counts",
        ),
        (
            '  const activeServices = useMemo(() => {\n'
            '    return groups\n'
            '      .filter(g => (g.metrics || []).some(m => m.enabled))\n'
            '      .map((g, i) => ({\n'
            '        id: g.service,\n'
            '        label: g.display_service || g.service,\n'
            '        desc: DESC_OVERRIDES[g.service] || (g.category === "core" ? "Core service" : "Extended service"),\n'
            '        color: PALETTE[i % PALETTE.length],\n'
            '        enabledCount: g.metrics.filter(m => m.enabled).length,\n'
            '      }));\n'
            '  }, [groups]);\n',

            '  const activeServices = useMemo(() => {\n'
            '    if (!liveCounts) return [];\n'
            '    return groups\n'
            '      .filter(g => (g.metrics || []).some(m => m.enabled))\n'
            '      .map((g, i) => {\n'
            '        const countField = RESOURCE_COUNT_FIELD[g.service];\n'
            '        const resourceCount = countField ? (liveCounts[countField] ?? 0) : null;\n'
            '        return {\n'
            '          id: g.service,\n'
            '          label: g.display_service || g.service,\n'
            '          desc: DESC_OVERRIDES[g.service] || (g.category === "core" ? "Core service" : "Extended service"),\n'
            '          color: PALETTE[i % PALETTE.length],\n'
            '          enabledCount: g.metrics.filter(m => m.enabled).length,\n'
            '          resourceCount,\n'
            '        };\n'
            '      })\n'
            '      // Only show a tile once we can dynamically confirm at least one\n'
            '      // real resource of that type exists. resourceCount === null means\n'
            '      // we have no live count (and no detail page) for this service yet\n'
            '      // — hide it rather than link somewhere broken; resourceCount === 0\n'
            '      // means the metric is enabled but nothing has been created yet.\n'
            '      .filter(svc => (svc.resourceCount ?? 0) > 0);\n'
            '  }, [groups, liveCounts]);\n',
            "ServiceList.jsx: filter tiles by live resource count",
        ),
        (
            "      {loading ? (\n"
            '        <div style={{ color:"var(--text-muted)", fontSize:13, padding:"40px 0", textAlign:"center" }}>Loading services…</div>\n'
            "      ) : activeServices.length === 0 ? (\n"
            "        <div style={{\n"
            '          border:"1px dashed var(--border)", borderRadius:"var(--radius-lg)", padding:"40px 24px",\n'
            '          textAlign:"center", color:"var(--text-muted)", fontSize:13,\n'
            "        }}>\n"
            '          No services are enabled for this account yet. Go to <b style={{color:"var(--text-secondary)"}}>Settings → Metrics</b> to select\n'
            "          which services and metrics to monitor — this page always mirrors that selection.\n"
            "        </div>\n"
            "      ) : (\n",

            "      {(loading || countsLoading) ? (\n"
            '        <div style={{ color:"var(--text-muted)", fontSize:13, padding:"40px 0", textAlign:"center" }}>Loading services…</div>\n'
            "      ) : activeServices.length === 0 ? (\n"
            "        <div style={{\n"
            '          border:"1px dashed var(--border)", borderRadius:"var(--radius-lg)", padding:"40px 24px",\n'
            '          textAlign:"center", color:"var(--text-muted)", fontSize:13,\n'
            "        }}>\n"
            '          No services with live resources yet. A tile appears here once a service both has monitoring enabled in\n'
            '          <b style={{color:"var(--text-secondary)"}}> Settings → Metrics</b> and has at least one real resource discovered in this account.\n'
            "        </div>\n"
            "      ) : (\n",
            "ServiceList.jsx: loading/empty state reflects dynamic counts",
        ),
        (
            '      <div style={{ fontSize:10, color:"var(--text-muted)", opacity:.7, marginBottom:10, fontFamily:"var(--font-mono)" }}>\n'
            '        {svc.enabledCount} metric{svc.enabledCount === 1 ? "" : "s"} enabled\n'
            '      </div>\n',

            '      <div style={{ fontSize:10, color:"var(--text-muted)", opacity:.7, marginBottom:10, fontFamily:"var(--font-mono)" }}>\n'
            '        {svc.resourceCount != null && (\n'
            '          <>{svc.resourceCount} resource{svc.resourceCount === 1 ? "" : "s"} · </>\n'
            '        )}\n'
            '        {svc.enabledCount} metric{svc.enabledCount === 1 ? "" : "s"} enabled\n'
            '      </div>\n',
            "ServiceList.jsx: show resource count on tile",
        ),
    ]
    if apply_replacements(p, replacements, already_applied_marker=marker):
        any_change = True

    # ------------------------------------------------------------------
    # Verify every patched Python file still compiles; revert everything
    # if not.
    # ------------------------------------------------------------------
    compile_errors = []
    for py_path in touched_py_files:
        try:
            ast.parse(read(py_path), filename=str(py_path))
            py_compile.compile(str(py_path), doraise=True)
        except Exception as e:
            compile_errors.append((py_path, e))

    if compile_errors:
        print("\nCOMPILE ERRORS — reverting all changes:")
        for py_path, e in compile_errors:
            print(f"  {py_path}: {e}")
        for py_path in touched_py_files:
            bak = py_path.with_name(py_path.name + BAK_SUFFIX)
            if bak.exists():
                shutil.copy2(bak, py_path)
                print(f"  reverted {py_path}")
        sys.exit(1)

    print("\nAll patched Python files compiled cleanly.")
    if any_change:
        print("Done. Restart the backend (systemctl restart monitoring-hub, or your local uvicorn) and rebuild the frontend (npm run build / dev server hot-reload) to pick up these changes.")
    else:
        print("Nothing to do — all patches were already applied.")


if __name__ == "__main__":
    main()
