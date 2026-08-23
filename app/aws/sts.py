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


def get_self_federation_session():
    """
    Mints a session-scoped, read-only credential set via STS
    GetFederationToken using the server's OWN identity — no
    cross-account AssumeRole, no pre-created target role. Only valid
    when the target AWS account IS the server's own account (see
    get_own_account_id()); for any other account this still requires a
    role_arn, which is a hard AWS security boundary, not a gap in this
    function.
    """
    sts = boto3.client("sts")
    response = sts.get_federation_token(
        Name="monitoring-hub-self",
        PolicyArns=[{"arn": "arn:aws:iam::aws:policy/ReadOnlyAccess"}],
        DurationSeconds=3600,
    )
    credentials = response["Credentials"]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
    )


def assume_role(role_arn: str, external_id: str | None = None):
    sts = boto3.client("sts")

    params = {
        "RoleArn": role_arn,
        "RoleSessionName": "monitoring-hub-session"
    }

    if external_id:
        params["ExternalId"] = external_id

    response = sts.assume_role(**params)

    credentials = response["Credentials"]

    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
    )