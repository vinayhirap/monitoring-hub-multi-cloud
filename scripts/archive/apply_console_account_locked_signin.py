#!/usr/bin/env python3
"""
apply_console_account_locked_signin.py — fixes "account number still
not auto-filled" WITHOUT bringing back the app-impersonated "mh-admin"
session from before. Uses AWS's own account-locked sign-in URL format
(the exact same one shown in every AWS account's IAM dashboard under
"IAM users sign-in link") instead of the generic
https://signin.aws.amazon.com page.

WHY THIS IS DIFFERENT FROM BOTH PREVIOUS VERSIONS
  - apply_restore_console_federation.py: minted a real temporary
    session (STS AssumeRole) so login was fully automatic — but the
    identity landing in AWS was an app-controlled role session
    ("mh-admin"), not the person's own AWS identity. Rejected.
  - apply_console_link_revert_to_manual_signin.py: plain deep-link,
    zero AWS-side identity assertion — but AWS's generic sign-in page
    shows a blank "Account ID or alias" field, forcing manual entry.
  - THIS script: no session is minted, no credentials are ever touched
    by this app — it just points the browser at
    https://<account-id>.signin.aws.amazon.com/console instead of the
    generic page. That URL is not a hack; it's the standard,
    documented way AWS itself expects an account to be linked to
    directly. The account is implied by the URL, so that field never
    appears — the person only ever sees IAM username + password,
    which are entirely their own. A `redirect_uri` parameter carries
    the intended region/resource through so they land on the specific
    page they clicked, not just the account's console home.

Usage:
    python apply_console_account_locked_signin.py --dry-run
    python apply_console_account_locked_signin.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-console-account-locked-signin"

OLD = r'''    """
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
NEW = r'''    """
    Returns an account-LOCKED AWS Console sign-in URL for `destination`
    -- NO session is minted, NO identity is assumed on the person's
    behalf, and NO password is ever seen or handled by this app. Every
    AWS account has this exact URL built in (Account Settings > "IAM
    users sign-in link" shows the identical format) -- pointing the
    browser at it is not a workaround, it's the standard way AWS
    itself expects a company to link people straight to sign-in for a
    SPECIFIC account instead of AWS's blank, generic sign-in page.

    What this fixes: the generic https://signin.aws.amazon.com page
    shows an empty "Account ID or alias" field the person has to know
    and type before they can even get to the username/password
    fields. The account-locked URL below skips that field entirely --
    the account is already implied by the URL itself -- so the person
    only ever sees IAM username + password, which are theirs alone.
    AWS's own sign-in flow carries the `redirect_uri` through to
    `destination` after a successful login, landing them on the
    specific resource page, not just the account's console home.

    This is NOT federation: no STS call happens here, no temporary
    credentials are minted, and there is nothing embedded in this URL
    that can authenticate anyone -- it is exactly as safe to share/
    log/click as a plain https://console.aws.amazon.com link. Each
    person still needs their own, separately provisioned native AWS
    IAM user (this app cannot create or manage AWS IAM users), and
    whatever THEIR OWN IAM permissions allow is what they'll be able
    to do in the console -- this app has no bearing on that either
    way.

    `role_arn`/`external_id` are accepted for backward compatibility
    with callers but are not used here (no credentials are minted).
    `requested_by`/`service`/`resource_id` are used only to record the
    click in the app's own audit log, since the app is no longer in a
    position to attribute anything on the AWS side.
    """
    _write_console_open_audit(requested_by, target_account_id, service, resource_id)

    if target_account_id:
        return (
            f"https://{target_account_id}.signin.aws.amazon.com/console"
            f"?region={urllib.parse.quote(region or 'us-east-1', safe='')}"
            f"&redirect_uri={urllib.parse.quote(destination, safe='')}"
        )
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
    print("\nVerify: click any Console button. The URL should now start with")
    print("https://<12-digit-account-id>.signin.aws.amazon.com/console — the")
    print("'Account ID or alias' field should be GONE, only IAM username +")
    print("password. Each person still needs their own IAM user created in")
    print("AWS — this does not create or manage AWS IAM users.")


if __name__ == "__main__":
    main()
