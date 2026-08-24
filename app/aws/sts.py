import re
import time

import boto3
from botocore.exceptions import ClientError

# In-memory cache for the server's own AWS account id (discovered via
# STS GetCallerIdentity against whatever credentials the process already
# has — the EC2 instance role in production). Avoids hammering STS on
# every console-link click.
_own_account_cache = {"account_id": None, "checked_at": 0.0}
_OWN_ACCOUNT_CACHE_TTL_SECONDS = 300


def get_own_account_id() -> str | None:
    """
    Returns the AWS account id the server's OWN credentials belong to,
    or None if it can't be determined (e.g. no credentials available).
    Used to detect the "target account IS the server's own account" case,
    where no cross-account role is needed at all.
    """
    now = time.time()
    cached = _own_account_cache["account_id"]
    if cached and (now - _own_account_cache["checked_at"]) < _OWN_ACCOUNT_CACHE_TTL_SECONDS:
        return cached
    try:
        identity = boto3.client("sts").get_caller_identity()
        account_id = identity["Account"]
        _own_account_cache["account_id"] = account_id
        _own_account_cache["checked_at"] = now
        return account_id
    except ClientError:
        return None


def _sanitize_session_name(raw: str | None) -> str | None:
    """
    Turns the monitoring-hub username of the person requesting a
    console link into a valid STS RoleSessionName, so the AWS
    CloudTrail record for that console session shows WHO in the
    monitoring hub opened it, instead of a shared generic name used
    by every user. The app has no way to hand out that person's own
    AWS IAM credentials (it never stores any), so per-user session
    naming plus a scoped session policy (see
    app.aws.federation.build_scoped_session_policy) is the closest
    real equivalent available: a distinct, audit-attributable,
    least-privilege session per person and per resource.

    STS requires RoleSessionName to match [\\w+=,.@-]{2,64}.
    """
    if not raw:
        return None
    cleaned = re.sub(r"[^\w+=,.@-]", "-", raw.strip())
    cleaned = cleaned.strip("-")[:55]  # leave room for "mh-" prefix, cap at 64 total
    if not cleaned:
        return None
    return f"mh-{cleaned}"


def get_self_federation_session(session_name: str | None = None,
                                policy: str | None = None):
    """
    Mints a session-scoped, read-only credential set via STS
    GetFederationToken using the server's OWN identity — no
    cross-account AssumeRole, no pre-created target role. Only valid
    when the target AWS account IS the server's own account (see
    get_own_account_id()); for any other account this still requires a
    role_arn, which is a hard AWS security boundary, not a gap in this
    function.

    `session_name` (see _sanitize_session_name) attributes the
    resulting CloudTrail activity to the actual monitoring-hub user
    who requested it, instead of the previous shared
    "monitoring-hub-self" name. `policy` is an optional IAM session
    policy JSON string (see federation.build_scoped_session_policy)
    that further narrows the session below ReadOnlyAccess — AWS
    always takes the INTERSECTION of PolicyArns and Policy, so this
    can only restrict, never expand, what the session can do.
    """
    sts = boto3.client("sts")
    kwargs = {
        "Name": session_name or "monitoring-hub-self",
        "PolicyArns": [{"arn": "arn:aws:iam::aws:policy/ReadOnlyAccess"}],
        "DurationSeconds": 3600,
    }
    if policy:
        kwargs["Policy"] = policy
    response = sts.get_federation_token(**kwargs)
    credentials = response["Credentials"]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
    )


def assume_role(role_arn: str, external_id: str | None = None,
                 session_name: str | None = None, policy: str | None = None):
    """
    `session_name` attributes the resulting CloudTrail activity to
    the actual monitoring-hub user who requested it (see
    _sanitize_session_name), instead of the previous shared
    "monitoring-hub-session" name used for every operator and every
    scheduled job alike. `policy` is an optional IAM session policy
    JSON string (see federation.build_scoped_session_policy) — AWS
    takes the INTERSECTION of the role's own permissions and this
    policy, so it can only restrict, never expand, access.
    """
    sts = boto3.client("sts")

    params = {
        "RoleArn": role_arn,
        "RoleSessionName": session_name or "monitoring-hub-session",
    }

    if external_id:
        params["ExternalId"] = external_id
    if policy:
        params["Policy"] = policy

    response = sts.assume_role(**params)

    credentials = response["Credentials"]

    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
    )