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
import json
import logging
import urllib.parse

import requests

from app.aws.sts import assume_role, get_own_account_id, get_self_federation_session

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


def build_federated_console_url(role_arn: str | None, external_id: str | None,
                                 destination: str,
                                 target_account_id: str | None = None) -> str:
    """
    Exchanges credentials for a sign-in token and returns a login URL that
    drops the user directly onto `destination` inside the CORRECT account —
    no dependence on whatever account the browser is currently signed into.

    Credential path is chosen automatically:
      - role_arn set                                   -> AssumeRole (cross-account)
      - role_arn empty, target_account_id == own account -> GetFederationToken
                                                             (self-federation,
                                                             zero config)
      - role_arn empty, target_account_id != own account -> NoConsoleCredentialsError
    """
    role_arn = (role_arn or "").strip()

    if role_arn:
        session = assume_role(role_arn, external_id)
    else:
        own_account_id = get_own_account_id()
        if target_account_id and own_account_id and str(target_account_id) == str(own_account_id):
            logger.info(
                "Console link for account %s uses self-federation (server's own account, no role_arn needed)",
                target_account_id,
            )
            session = get_self_federation_session()
        else:
            raise NoConsoleCredentialsError(
                "No AWS role configured for this account, and it is not "
                "the server's own AWS account, so no automatic credential "
                "path is available. Set an IAM Role ARN for this account "
                "in Settings to enable console access."
            )

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
