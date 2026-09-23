import re
import time

import boto3
from botocore.exceptions import ClientError
from app.aws.boto_config import STANDARD_RETRY

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
        identity = boto3.client("sts", config=STANDARD_RETRY).get_caller_identity()
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

    NOT CURRENTLY CALLED ANYWHERE (confirmed by repo-wide grep,
    2026-09-18 console-link audit) -- this was the credential-minting
    step of the OLD AWS console-link approach, removed 2026-09-12 in
    favor of an account-locked sign-in link that mints no credentials
    at all (see app/aws/federation.py's module docstring for the full
    explanation and why that change was made). Do not wire this back
    into console-link generation to "fix" a link problem -- that would
    silently reintroduce auto-signing the visiting person in AS this
    app's own server identity, exactly the behavior that audit removed.
    If a genuinely new feature needs a real scoped session (not just a
    link), that decision and its consent/audit story should be made
    explicitly, not by resurrecting this function's old call site.
    """
    sts = boto3.client("sts", config=STANDARD_RETRY)
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

    SECURITY (fix: 2026-09 B04 audit -- CRITICAL, confused-deputy /
    privilege escalation): this function used to short-circuit straight
    to the server's own ambient/instance-profile credentials (bypassing
    AssumeRole entirely) whenever role_arn's account-id segment matched
    get_own_account_id() -- a check performed by regex-parsing the
    caller-supplied role_arn string, with NO verification that the role
    actually exists, is assumable, or was ever configured by an admin.
    Any editor holding only the accounts.onboard permission (a lower
    trust tier than accounts.delete/full admin -- see delete_account's
    own comment on that distinction) could onboard or test-role an
    account with account_id set to the server's own AWS account number
    and role_arn set to ANY arn:aws:iam::<that number>:role/<anything,
    even nonexistent>, and would be handed back the server's own
    instance-profile session -- not a scoped ReadOnlyAccess assumption,
    the server's actual ambient credentials -- for every subsequent
    boto3 call made against that "account" (test-role, discover,
    console-url, and ongoing monitoring/collection cycles). That is a
    privilege escalation from accounts.onboard up to whatever the
    server's own instance profile can do, gated on nothing but guessing
    or learning a non-secret 12-digit account number.
    The legitimate case this was trying to serve (server's own AWS
    account being one of the monitored accounts, where self-assumption
    normally fails AccessDenied without an explicit self-trust policy)
    is already served safely elsewhere: get_boto3_session() already
    falls back to plain boto3.Session() whenever role_arn is left EMPTY
    for an account. That is an explicit admin decision made once at
    onboarding time, not something inferred per-call from unvalidated
    input. So: no more account-number short-circuit here. A role_arn
    that really does point at the server's own account is now attempted
    for real via sts:AssumeRole like any other role_arn, and fails
    loudly (AccessDenied) unless a genuine self-assumption trust policy
    exists -- surfacing a clear, honest error instead of silently
    substituting a different, more powerful credential set. Operators
    onboarding the server's own account should leave role_arn blank.
    get_own_account_id() is kept (unused by this function now) only in
    case a future, explicitly admin-gated feature needs it -- see its
    own docstring.
    """
    sts = boto3.client("sts", config=STANDARD_RETRY)

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