#!/usr/bin/env python3
"""
apply_console_link_revert_to_manual_signin.py — reverts AWS Console
links back to plain, unauthenticated deep-links. Each person must
sign in with their OWN, personally-provisioned native AWS IAM user
(created directly in AWS, outside this app) — not an app-assumed
role session.

WHY THIS IS THE OPPOSITE OF apply_restore_console_federation.py
That script made Console links auto-select the correct account by
minting a temporary, scoped session under an app-controlled assumed
role — seamless, but the AWS-side identity was a role session
(labeled with the requesting user's name, e.g. "mh-admin"), not that
person's own distinct AWS IAM identity. This script trades the
auto-login back out: AWS's sign-in page cannot be made to auto-fill a
person's own individual password (no legitimate mechanism does that,
by design), so "account auto-selected" and "signed in as the
person's own separate AWS identity" are mutually exclusive — this
app is now configured to prioritize the latter.

CONCRETE IMPLICATION: every person who needs Console access must have
their own IAM user created directly in AWS by whoever manages your
AWS accounts — this app cannot create, list, or manage AWS IAM users,
only AWS resources within the accounts it monitors. What they see
after signing in is governed entirely by their own IAM permissions,
which this app has no visibility into or control over.

No frontend changes needed — every Console button already calls the
same backend endpoints; fixing this one shared function affects all
of them.

Usage:
    python apply_console_link_revert_to_manual_signin.py --dry-run
    python apply_console_link_revert_to_manual_signin.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-console-link-revert-to-manual-signin"

OLD = r'''def build_federated_console_url(role_arn: str | None, external_id: str | None,
                                 destination: str,
                                 target_account_id: str | None = None,
                                 requested_by: str | None = None,
                                 service: str | None = None,
                                 resource_id: str | None = None,
                                 region: str | None = None,
                                 resource_name: str | None = None,
                                 ecs_service_name: str | None = None) -> str:
    """
    Mints a short-lived, least-privilege AWS Console sign-in URL for
    `destination` and returns it — clicking it lands the browser
    already authenticated into the CORRECT account, with no manual
    account ID / username / password entry, regardless of whatever
    AWS identity (if any) is already signed into that browser.

    The session is scoped as tightly as IAM allows:
      - build_scoped_session_policy() narrows it to read-only access
        for the ONE service (and, where AWS IAM supports it, the ONE
        resource) being opened -- never broader than that, and never
        broader than the underlying role's own permissions either
        (AWS enforces the intersection).
      - The STS RoleSessionName is the requesting monitoring-hub
        user's own username (_sanitize_session_name), so the
        resulting CloudTrail record on the AWS side is attributable
        to a specific person, not a shared generic session name.

    Cross-account (role_arn set) uses AssumeRole; same-account (no
    role_arn, target account matches the server's own) uses
    GetFederationToken via get_self_federation_session -- see that
    function's docstring for why no role_arn is required there.
    Raises NoConsoleCredentialsError if neither path applies, so
    callers can surface a clean 400 instead of a raw AWS error.
    """
    _write_console_open_audit(requested_by, target_account_id, service, resource_id)

    session_name = _sanitize_session_name(requested_by)
    policy = build_scoped_session_policy(
        service, resource_id, region, target_account_id, resource_name, ecs_service_name,
    )

    if role_arn:
        session = assume_role(role_arn, external_id, session_name=session_name, policy=policy)
    elif target_account_id and target_account_id == get_own_account_id():
        session = get_self_federation_session(session_name=session_name, policy=policy)
    else:
        raise NoConsoleCredentialsError(
            f"No role_arn configured for account {target_account_id!r}, and it isn't "
            "this server's own account -- cannot mint console credentials for it."
        )

    creds = session.get_credentials().get_frozen_credentials()

    session_json = json.dumps({
        "sessionId":    creds.access_key,
        "sessionKey":   creds.secret_key,
        "sessionToken": creds.token,
    })

    resp = requests.get(
        FEDERATION_ENDPOINT,
        params={
            "Action":          "getSigninToken",
            "SessionDuration": SESSION_DURATION_SECONDS,
            "Session":         session_json,
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
'''
NEW = r'''def build_federated_console_url(role_arn: str | None, external_id: str | None,
                                 destination: str,
                                 target_account_id: str | None = None,
                                 requested_by: str | None = None,
                                 service: str | None = None,
                                 resource_id: str | None = None,
                                 region: str | None = None,
                                 resource_name: str | None = None,
                                 ecs_service_name: str | None = None) -> str:
    """
    Returns the plain AWS Console deep-link URL for `destination`
    directly -- NO session is minted, NO identity is assumed on the
    person's behalf. AWS itself prompts for sign-in when the link is
    opened, and each person is expected to have their OWN, separately
    provisioned native AWS IAM user (created directly in AWS -- this
    app has no ability to create or manage AWS IAM users) to sign in
    with. Whatever that person's OWN IAM permissions allow is what
    they'll be able to see/do in the console; this app has no bearing
    on it either way.

    Deliberate trade-off, chosen over the alternative (a previous
    version of this function minted a temporary, scoped, app-assumed-
    role session so the correct account/region/resource would already
    be selected with no manual entry -- see apply_restore_console_
    federation.py for that version): AWS's sign-in page cannot be
    made to auto-fill a person's own individual password, so
    "auto-selects the account" and "signs in as the person's own,
    distinct AWS identity" are mutually exclusive. This app is
    configured to prioritize the latter -- console access reflects
    each person's real, independently-managed AWS entitlements, not
    an app-controlled impersonated session, at the cost of the
    account/region/resource needing to be selected manually after
    signing in (though `destination` below still encodes the intended
    region and resource, for once they're signed in).

    `role_arn`/`external_id` are accepted for backward compatibility
    with callers but are not used to mint credentials here.
    `requested_by`/`service`/`resource_id`/`target_account_id` are
    used only to record the click in the app's own audit log, since
    the app is no longer in a position to attribute anything on the
    AWS side.
    """
    _write_console_open_audit(requested_by, target_account_id, service, resource_id)
    return destination
'''

TARGET = "app/aws/federation.py"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    full_path = REPO_ROOT / TARGET
    if not full_path.exists():
        print(f"MISSING FILE: {TARGET}", file=sys.stderr)
        sys.exit(1)

    text = full_path.read_text(encoding="utf-8")
    if NEW in text:
        print("Already applied — nothing to do.")
        return
    if OLD not in text:
        print(f"ABORTED: anchor not found in {TARGET} — the file has "
              "changed since this script was written, refusing to guess.", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print(f"[DRY RUN] would patch: {TARGET}")
        return

    backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
    if not backup_path.exists():
        shutil.copy2(full_path, backup_path)
    full_path.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    py_compile.compile(str(full_path), doraise=True)
    print(f"PATCHED: {TARGET}  (backup: {backup_path.name})")
    print("  syntax OK")
    print("\nsudo systemctl restart monitoring-hub")
    print("\nVerify: click any Console button. AWS should now show its normal")
    print("sign-in page (account ID/alias + IAM username + password), NOT an")
    print("already-authenticated session under any role.")


if __name__ == "__main__":
    main()
