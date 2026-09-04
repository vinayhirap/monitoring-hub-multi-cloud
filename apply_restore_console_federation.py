#!/usr/bin/env python3
"""
apply_restore_console_federation.py — restores AWS Console deep-link
federation across EVERY "Console" button in the app (ServiceList,
Alerts, AccountDetail, ServiceDetail all already call the SAME two
backend endpoints — /api/admin/accounts/{id}/console-url and
/api/alerts/{id}/console-url — so fixing the one shared function
those endpoints both funnel through fixes every button at once).

WHAT WAS ACTUALLY WRONG
Every "Console" button already correctly builds a deep link to the
right region and resource — that part worked. What was missing is
the account auto-select and auto-login: app.aws.federation.
build_federated_console_url() had been deliberately reduced to
`return destination` (a bare, unauthenticated console URL) by a prior
change (see the now-removed "apply_console_direct_link_fix.py"
reference in its old docstring), on the reasoning that federation
means impersonating an app-controlled identity rather than "the
viewer's own AWS credentials." Clicking a bare, unauthenticated
console URL is exactly the generic IAM sign-in page (account ID +
username + password) you were seeing — the region/resource in the
URL are still correct, AWS just won't apply them until *something*
signs in first.

THE RESTORE
All the actual machinery for a PROPERLY scoped, attributable session
was already fully built and just sitting unused below the early
`return` — build_scoped_session_policy() (read-only, narrowed to the
ONE service/resource being opened, never broader than the underlying
role's own permissions — AWS enforces the intersection) and
_sanitize_session_name() (so the resulting AWS-side CloudTrail record
is attributable to the specific monitoring-hub user who clicked it,
not a shared generic session name). This script re-wires
build_federated_console_url() to actually use them: AssumeRole for
cross-account, GetFederationToken (via get_self_federation_session)
for the server's own account, then the standard AWS sign-in
federation endpoint (getSigninToken -> login URL). Nothing else
changes — every caller (accounts.py, alerts.py, provider.py) already
passes the right parameters through; they were just being ignored downstream.

RESULT: clicking Console now opens AWS already signed into the
correct account, region, and resource page, with no manual entry —
and the session that lands there can only read the one service/
resource that was clicked, nothing else in the account.

Usage:
    python apply_restore_console_federation.py --dry-run
    python apply_restore_console_federation.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-restore-console-federation"

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
    print("\nNo frontend changes needed — every Console button already")
    print("calls the backend endpoints this fixes. Just restart the backend:")
    print("  sudo systemctl restart monitoring-hub")
    print("\nVerify: click any Console button. It should land in AWS")
    print("already signed in to the right account, no sign-in page.")
    print("If it still shows a generic sign-in page, check the backend log")
    print("for NoConsoleCredentialsError — that account is missing a")
    print("role_arn and isn't this server's own AWS account either.")


if __name__ == "__main__":
    main()
