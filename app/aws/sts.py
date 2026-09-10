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

    SAME-ACCOUNT SHORT-CIRCUIT (fix: 2026-08-26 AuroGov Mumbai incident):
    role_arn's account can legitimately be the SAME account the server's
    own credentials belong to (an account row doesn't have to be
    cross-account). Real sts:AssumeRole on your own role always fails
    AccessDenied unless its trust policy explicitly allows self-assumption
    -- which nothing here configures and shouldn't have to. Detect this via
    the role ARN's account id vs get_own_account_id() and skip straight to
    the instance's own default credential chain instead. Any genuinely
    cross-account role_arn is completely unaffected by this check.
    """
    match = re.match(r"arn:aws:iam::(\d+):role/", role_arn or "")
    if match:
        target_account_id = match.group(1)
        own_account_id = get_own_account_id()
        if own_account_id and target_account_id == own_account_id:
            # NOTE (2026-08-26): get_self_federation_session() was tried
            # here first but AWS rejects STS GetFederationToken when called
            # with SESSION credentials -- confirmed live: "Cannot call
            # GetFederationToken with session credentials". An EC2 instance
            # profile (what this server runs as) always provides temporary
            # session credentials via IMDS, so that call can never succeed
            # regardless of target account. No STS call is actually needed
            # for the same-account case -- the instance's own default
            # credential chain already has whatever permissions its
            # instance profile grants, same as the existing no-role_arn
            # fallback in discovery/runner.py and metrics/runner.py.
            return boto3.Session()

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


def get_boto3_session(account: dict):
    """
    Single choke point for resolving a boto3 Session for a monitored AWS
    account row. `account` must include at least "id" and "role_arn"; pass
    "auth_mode" and "external_id" too when the caller's query selects them.

    Precedence, matching migration 018_aws_static_key_auth.sql's design:
      1. auth_mode == "static_keys" -- a per-account IAM user's long-lived
         access key + secret key, stored Fernet-encrypted in
         provider_credentials via app.credentials (same table/path Azure
         and GCP already use; the pair is JSON-encoded into one string
         since that table stores one opaque secret per account).
      2. role_arn set -- cross-account AssumeRole via assume_role() above,
         which itself already short-circuits to boto3.Session() when the
         target account matches the server's own account (see the
         SAME-ACCOUNT SHORT-CIRCUIT block in assume_role(), fix: 22ff060).
      3. neither -- plain boto3.Session() (ambient/instance credentials).

    Callers should prefer this over calling assume_role()/boto3.Session()
    directly so static-key accounts work everywhere AssumeRole accounts
    already do, without each call site re-implementing the branch.
    """
    if account.get("auth_mode") == "static_keys":
        from app.credentials import load_credential
        import json as _json

        raw = load_credential(account["id"])
        if not raw:
            raise RuntimeError(
                f"aws_accounts.id={account.get('id')} has auth_mode='static_keys' "
                f"but no credential is stored in provider_credentials -- onboarding "
                f"may have failed partway through, or the credential was deleted "
                f"without resetting auth_mode."
            )
        creds = _json.loads(raw)
        return boto3.Session(
            aws_access_key_id=creds["access_key_id"],
            aws_secret_access_key=creds["secret_access_key"],
        )

    if account.get("role_arn"):
        return assume_role(account["role_arn"], account.get("external_id"))

    return boto3.Session()