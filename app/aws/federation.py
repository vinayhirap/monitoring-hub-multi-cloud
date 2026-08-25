# app/aws/federation.py
"""
Builds account-specific AWS Console deep links via the federation endpoint.

Why this exists
----------------
Just linking to https://<region>.console.aws.amazon.com/... does NOT select
an AWS account — it opens whatever account is already active in the user's
browser session (via existing sign-in cookies). If the operator is signed
into a different account than the one the alert belongs to, the console
opens the WRONG account.

The fix is to mint a short-lived sign-in token for the alert's specific
account/role via STS + the AWS sign-in federation endpoint, then wrap the
target deep-link in a `Destination=` federation login URL. That login URL
forces the correct account context before landing on the resource page,
regardless of any existing browser session.

Docs: https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_enable-console-custom-url.html
"""
import datetime
import json
import logging
import urllib.parse

import requests

from app.aws.sts import assume_role, get_own_account_id, get_self_federation_session
from app.aws.sts import _sanitize_session_name

logger = logging.getLogger(__name__)

FEDERATION_ENDPOINT = "https://signin.aws.amazon.com/federation"
ISSUER = "monitoring-hub"
SESSION_DURATION_SECONDS = 3600  # must be <= the assumed role's max session duration


class NoConsoleCredentialsError(ValueError):
    """
    Raised when we have no way to obtain console credentials for the
    target account: no role_arn is configured AND the target account is
    not the server's own account. Callers should surface this as a 400
    (config problem), distinct from other exceptions in this module
    which mean the credential path was found but the AWS call itself
    failed (500/502).
    """
    pass


def service_console_list_url(service: str, region: str) -> str:
    """
    List-view console URL for a whole service (e.g. all EC2 instances) —
    used when no specific resource is selected yet.
    """
    region = region or "us-east-1"
    service = (service or "").lower()
    base = f"https://{region}.console.aws.amazon.com"
    return {
        "ec2":    f"{base}/ec2/home?region={region}#Instances:",
        "ebs":    f"{base}/ec2/home?region={region}#Volumes:",
        "rds":    f"{base}/rds/home?region={region}#databases:",
        "lambda": f"{base}/lambda/home?region={region}#/functions",
        "s3":     "https://s3.console.aws.amazon.com/s3/buckets",
        "elb":    f"{base}/ec2/home?region={region}#LoadBalancers:",
        "ecs":    f"{base}/ecs/home?region={region}",
    }.get(service, f"{base}/console/home?region={region}")


def resource_console_destination(service: str, resource_id: str, region: str,
                                  resource_name: str | None = None,
                                  ecs_service_name: str | None = None) -> str:
    """
    Resource-type-specific AWS Console deep link.

    `service` should be one of the resources.resource_type values
    (ec2/ebs/rds/lambda/s3/elb/ecs — case-insensitive). This is the
    single source of truth for console-link construction — the same
    mapping frontend/src/pages/ServiceDetail.jsx used to keep as its own
    separate copy (see multi-cloud-architecture-assessment.md section
    2.3); that copy is being retired in favor of calling through here.

    `resource_name` is used where the console needs a display name
    rather than an ARN/ID (e.g. ELB search-by-name). `ecs_service_name`
    enables the deeper cluster > service link for ECS when known;
    without it, ECS falls back to the cluster-level view.

    If `service` is missing/unrecognized (an older caller that hasn't
    been updated yet), falls back to the original ID-prefix-guessing
    behavior so nothing regresses for callers not yet passing `service`.
    """
    region = region or "us-east-1"
    if not resource_id:
        return service_console_list_url(service, region)

    svc = (service or "").lower()
    base = f"https://{region}.console.aws.amazon.com"

    if svc == "ec2":
        return f"{base}/ec2/home?region={region}#Instances:instanceId={resource_id}"
    if svc == "ebs":
        return f"{base}/ec2/home?region={region}#Volumes:volumeId={resource_id}"
    if svc == "rds":
        return f"{base}/rds/home?region={region}#database:id={resource_id}"
    if svc == "lambda":
        return f"{base}/lambda/home?region={region}#/functions/{resource_id}"
    if svc == "s3":
        return f"https://s3.console.aws.amazon.com/s3/buckets/{resource_id}"
    if svc == "elb":
        search_term = resource_name or resource_id
        return f"{base}/ec2/home?region={region}#LoadBalancers:search={search_term}"
    if svc == "ecs":
        cluster = resource_name or resource_id
        if ecs_service_name:
            return (f"{base}/ecs/home?region={region}"
                    f"#/clusters/{cluster}/services/{ecs_service_name}")
        return f"{base}/ecs/home?region={region}#/clusters/{cluster}"

    return _legacy_prefix_guess_destination(resource_id, region)


def _legacy_prefix_guess_destination(resource: str, region: str) -> str:
    """
    Original ID-shape-guessing dispatch, kept as a fallback for any
    caller that doesn't pass an explicit `service`. Covers only
    EC2/EBS/Lambda/RDS — identical to this file's behavior before this
    patch, no S3/ELB/ECS support on this path.
    """
    region = region or "us-east-1"
    if not resource:
        return f"https://{region}.console.aws.amazon.com/console/home?region={region}"

    if resource.startswith("i-"):
        return (f"https://{region}.console.aws.amazon.com/ec2/home"
                f"?region={region}#Instances:instanceId={resource}")
    if resource.startswith("vol-"):
        return (f"https://{region}.console.aws.amazon.com/ec2/home"
                f"?region={region}#Volumes:volumeId={resource}")
    if "lambda" in resource or resource.startswith("arn:aws:lambda"):
        fn = resource.split(":")[-1]
        return (f"https://{region}.console.aws.amazon.com/lambda/home"
                f"?region={region}#/functions/{fn}")
    if resource.startswith("db-") or "rds" in resource:
        return f"https://{region}.console.aws.amazon.com/rds/home?region={region}#database:"

    return f"https://{region}.console.aws.amazon.com/console/home?region={region}"


def _service_read_actions(service: str) -> list[str]:
    """
    Minimal read-only IAM actions needed to view/monitor ONE AWS
    service in the console, used to build a session policy that
    narrows a federated session down to just this service — instead
    of the previous blanket ReadOnlyAccess, which grants read access
    to every AWS service regardless of which alert/resource the
    person actually clicked into.
    """
    common = [
        "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics", "cloudwatch:DescribeAlarms",
        "tag:GetResources", "tag:GetTagKeys", "tag:GetTagValues",
        "sts:GetCallerIdentity",
    ]
    per_service = {
        "ec2":    ["ec2:Describe*", "ec2:GetConsoleOutput", "ec2:GetConsoleScreenshot"],
        "ebs":    ["ec2:Describe*"],
        "rds":    ["rds:Describe*", "rds:ListTagsForResource"],
        "lambda": ["lambda:Get*", "lambda:List*"],
        "s3":     ["s3:GetBucket*", "s3:ListBucket", "s3:GetObject", "s3:ListAllMyBuckets"],
        "elb":    ["elasticloadbalancing:Describe*"],
        "ecs":    ["ecs:Describe*", "ecs:List*"],
    }
    extra = per_service.get((service or "").lower())
    if not extra:
        return []
    return common + extra


def _service_resource_arns(service: str, resource_id: str | None, region: str | None,
                            account_id: str | None, resource_name: str | None = None,
                            ecs_service_name: str | None = None) -> list[str] | None:
    """
    Best-effort ARN(s) for the SPECIFIC resource being viewed, so the
    session policy's Resource element can be scoped to just that
    resource wherever AWS IAM actually supports resource-level
    permissions for the relevant read actions. Returns None (caller
    falls back to "*") for services where the Describe/List calls
    involved are account/region-wide by design in AWS IAM — e.g.
    ec2:DescribeInstances has no resource-level permission support —
    which is a hard AWS limitation, not a gap in this function.
    """
    if not resource_id or not account_id:
        return None
    svc = (service or "").lower()
    region = region or "us-east-1"
    if svc == "s3":
        bucket = resource_id
        return [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]
    if svc == "lambda":
        return [f"arn:aws:lambda:{region}:{account_id}:function:{resource_id}"]
    if svc == "rds":
        return [f"arn:aws:rds:{region}:{account_id}:db:{resource_id}"]
    if svc == "ecs":
        cluster = resource_name or resource_id
        arns = [f"arn:aws:ecs:{region}:{account_id}:cluster/{cluster}"]
        if ecs_service_name:
            arns.append(f"arn:aws:ecs:{region}:{account_id}:service/{cluster}/{ecs_service_name}")
        return arns
    return None


def build_scoped_session_policy(service: str | None, resource_id: str | None = None,
                                 region: str | None = None,
                                 target_account_id: str | None = None,
                                 resource_name: str | None = None,
                                 ecs_service_name: str | None = None) -> str | None:
    """
    Builds an IAM session-policy JSON string that narrows a federated
    console session to read-only access for ONE service — and, where
    AWS IAM supports it, ONE specific resource — instead of the
    previous blanket ReadOnlyAccess across every AWS service. Returns
    None if `service` isn't recognized, so callers fall back to
    whichever base policy they already had.

    This is a real IAM session policy: AWS enforces the
    INTERSECTION of this policy and the underlying role/user's own
    permissions, so it can only ever narrow access further — never
    grant anything the base identity didn't already have.
    """
    if not service:
        return None
    actions = _service_read_actions(service)
    if not actions:
        return None
    arns = _service_resource_arns(service, resource_id, region, target_account_id,
                                   resource_name, ecs_service_name)
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect":   "Allow",
            "Action":   sorted(set(actions)),
            "Resource": arns if arns else "*",
        }],
    }
    body = json.dumps(policy)
    # STS session-policy documents are capped at 2048 chars — fall
    # back to no extra scoping (base policy still applies) rather
    # than send something AWS would reject outright.
    if len(body) > 2000:
        logger.warning("Scoped session policy for %s too large (%d chars) — skipping extra scoping", service, len(body))
        return None
    return body


def _write_console_open_audit(requested_by, target_account_id, service, resource_id):
    """
    Records who opened a console link and for what, in the app's OWN
    audit log (visible under Audit Logs in the UI). This is the only
    attribution the app can meaningfully provide now that it no
    longer impersonates anyone for console access — AWS-side
    attribution is whatever identity the person is personally signed
    in as, which the app has no visibility into or control over.
    """
    try:
        from app.db import get_connection
        conn = get_connection(); cur = conn.cursor()
        cur.execute(
            "INSERT INTO audit_logs (actor, action, payload) VALUES (%s,%s,%s)",
            (
                requested_by or "unknown",
                "Opened AWS console link",
                json.dumps({
                    "account_id": target_account_id,
                    "service": service,
                    "resource_id": resource_id,
                    "at": datetime.datetime.utcnow().isoformat(),
                }),
            ),
        )
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.warning("Console-open audit write failed: %s", e)


def build_federated_console_url(role_arn: str | None, external_id: str | None,
                                 destination: str,
                                 target_account_id: str | None = None,
                                 requested_by: str | None = None,
                                 service: str | None = None,
                                 resource_id: str | None = None,
                                 region: str | None = None,
                                 resource_name: str | None = None,
                                 ecs_service_name: str | None = None) -> str:
    """
    Returns the plain AWS Console URL for `destination` directly — no
    AWS session is minted for this — see apply_console_direct_link_fix.py.
    Whichever AWS identity is already signed into the browser (or gets
    prompted to sign in, if none) governs what's actually visible.
    That's a deliberate change from the previous federated-session
    approach: access is now genuinely the viewer's own AWS credentials
    and entitlements, not an app-controlled impersonated identity, and
    there is no token embedded that can silently re-authenticate
    someone after they sign out of the AWS console.

    `role_arn`/`external_id` are accepted for backward compatibility
    with callers but are no longer used to mint credentials here.
    `requested_by`/`service`/`resource_id`/`target_account_id` are used
    only to record the click in the app's own audit log, since the app
    is no longer in a position to attribute anything on the AWS side.
    """
    _write_console_open_audit(requested_by, target_account_id, service, resource_id)
    return destination

    creds = session.get_credentials().get_frozen_credentials()

    session_json = json.dumps({
        "sessionId": creds.access_key,
        "sessionKey": creds.secret_key,
        "sessionToken": creds.token,
    })

    resp = requests.get(
        FEDERATION_ENDPOINT,
        params={
            "Action": "getSigninToken",
            "SessionDuration": SESSION_DURATION_SECONDS,
            "Session": session_json,
        },
        timeout=10,
    )
    resp.raise_for_status()
    signin_token = resp.json()["SigninToken"]

    return (
        f"{FEDERATION_ENDPOINT}?Action=login"
        f"&Issuer={urllib.parse.quote(ISSUER, safe='')}"
        f"&Destination={urllib.parse.quote(destination, safe='')}"
        f"&SigninToken={urllib.parse.quote(signin_token, safe='')}"
    )
