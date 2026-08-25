#!/usr/bin/env python3
"""
apply_console_direct_link_fix.py

Replaces AWS console federation with a plain, direct console deep link.

Why the previous approach (scoped STS federation) still wasn't right:
Any link built via STS GetFederationToken / AssumeRole + the AWS
sign-in federation endpoint embeds a SigninToken that signs the browser
straight into an AWS-side session for that impersonated identity —
that's what federation IS, by design. Scoping the session's name and
permissions (previous patch) fixed CloudTrail attribution and
blast-radius, but could never fix the actual complaint: every link
still auto-authenticates as an app-controlled identity, and signing
out of the AWS console + reopening the same kind of link just
re-authenticates silently again, because a fresh valid token is
generated server-side every time. There's no way to make a federation
URL behave like a personal login — the two are different mechanisms.

The fix: stop minting AWS credentials for this at all. Open the plain
https://<region>.console.aws.amazon.com/... URL for the resource
directly (resource_console_destination() already builds exactly this —
it's a real, complete console URL, not a path fragment). Whichever AWS
identity is already signed into that browser governs what's visible;
if nobody is signed in, AWS shows its own normal sign-in page. Access
is now genuinely the viewer's own AWS credentials and entitlements,
not the monitoring hub's.

Trade-off (real, not hidden): this requires each person who wants to
open Console links to actually have their own AWS Console access
(IAM user or SSO) to the target account already. Anyone who doesn't
will land on AWS's sign-in page instead of the resource — that's the
correct, honest behavior for "their own credentials," not a bug. The
app now keeps its OWN record of who clicked what and when (new
`_write_audit` call in federation.py, visible in Audit Logs), which is
the only attribution the app is still in a position to provide once it
stops impersonating anyone.

The scoped-federation machinery from the previous patch (STS session
naming, build_scoped_session_policy, etc.) is left in place but no
longer called by build_federated_console_url — it's dead code for now,
kept in case a future "break-glass" federated-access mode is wanted
for people without their own AWS access, rather than deleted and lost.

Usage:
    python apply_console_direct_link_fix.py [repo_root]

Idempotent, backs up to "<file>.bak.pre-console-direct-link-fix",
reverts automatically if any patched Python file fails py_compile.
"""
import ast
import py_compile
import shutil
import sys
from pathlib import Path

BAK_SUFFIX = ".bak.pre-console-direct-link-fix"


class PatchError(Exception):
    pass


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def backup(path: Path):
    bak = path.with_name(path.name + BAK_SUFFIX)
    if not bak.exists():
        shutil.copy2(path, bak)


def apply_replacements(path: Path, replacements, already_applied_marker=None):
    text = read(path)
    if already_applied_marker and already_applied_marker in text:
        print(f"  SKIP  {path} (already patched)")
        return False

    backup(path)
    for old, new, label in replacements:
        count = text.count(old)
        if count == 0:
            raise PatchError(f"{path}: pattern not found for '{label}'")
        if count > 1:
            raise PatchError(f"{path}: pattern for '{label}' matches {count} times, expected 1")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    print(f"  OK    {path} ({len(replacements)} edit{'s' if len(replacements) != 1 else ''})")
    return True


def main():
    repo_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    print(f"Repo root: {repo_root}")

    touched_py_files = []
    any_change = False

    # ------------------------------------------------------------------
    # app/aws/federation.py — build_federated_console_url no longer
    # mints any AWS credentials; it just returns the plain console URL
    # and records the click in the app's own audit log.
    # ------------------------------------------------------------------
    p = repo_root / "app" / "aws" / "federation.py"
    marker = "no AWS session is minted for this — see apply_console_direct_link_fix.py"
    replacements = [
        (
            "import json\n"
            "import logging\n"
            "import urllib.parse\n"
            "\n"
            "import requests\n"
            "\n"
            "from app.aws.sts import assume_role, get_own_account_id, get_self_federation_session\n"
            "from app.aws.sts import _sanitize_session_name\n",

            "import datetime\n"
            "import json\n"
            "import logging\n"
            "import urllib.parse\n"
            "\n"
            "import requests\n"
            "\n"
            "from app.aws.sts import assume_role, get_own_account_id, get_self_federation_session\n"
            "from app.aws.sts import _sanitize_session_name\n",
            "federation.py: add datetime import for audit timestamp",
        ),
        (
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
            "                \"Console link for account %s uses self-federation (server's own account, no role_arn needed), \"\n"
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

            'def _write_console_open_audit(requested_by, target_account_id, service, resource_id):\n'
            '    """\n'
            '    Records who opened a console link and for what, in the app\'s OWN\n'
            '    audit log (visible under Audit Logs in the UI). This is the only\n'
            '    attribution the app can meaningfully provide now that it no\n'
            '    longer impersonates anyone for console access — AWS-side\n'
            '    attribution is whatever identity the person is personally signed\n'
            '    in as, which the app has no visibility into or control over.\n'
            '    """\n'
            '    try:\n'
            '        from app.db import get_connection\n'
            '        conn = get_connection(); cur = conn.cursor()\n'
            '        cur.execute(\n'
            '            "INSERT INTO audit_logs (actor, action, payload) VALUES (%s,%s,%s)",\n'
            '            (\n'
            '                requested_by or "unknown",\n'
            '                "Opened AWS console link",\n'
            '                json.dumps({\n'
            '                    "account_id": target_account_id,\n'
            '                    "service": service,\n'
            '                    "resource_id": resource_id,\n'
            '                    "at": datetime.datetime.utcnow().isoformat(),\n'
            '                }),\n'
            '            ),\n'
            '        )\n'
            '        conn.commit(); cur.close(); conn.close()\n'
            '    except Exception as e:\n'
            '        logger.warning("Console-open audit write failed: %s", e)\n'
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
            '    Returns the plain AWS Console URL for `destination` directly — no\n'
            '    AWS session is minted for this — see apply_console_direct_link_fix.py.\n'
            '    Whichever AWS identity is already signed into the browser (or gets\n'
            '    prompted to sign in, if none) governs what\'s actually visible.\n'
            '    That\'s a deliberate change from the previous federated-session\n'
            '    approach: access is now genuinely the viewer\'s own AWS credentials\n'
            '    and entitlements, not an app-controlled impersonated identity, and\n'
            '    there is no token embedded that can silently re-authenticate\n'
            '    someone after they sign out of the AWS console.\n'
            '\n'
            '    `role_arn`/`external_id` are accepted for backward compatibility\n'
            '    with callers but are no longer used to mint credentials here.\n'
            '    `requested_by`/`service`/`resource_id`/`target_account_id` are used\n'
            "    only to record the click in the app's own audit log, since the app\n"
            '    is no longer in a position to attribute anything on the AWS side.\n'
            '    """\n'
            '    _write_console_open_audit(requested_by, target_account_id, service, resource_id)\n'
            '    return destination\n',
            "federation.py: build_federated_console_url returns plain destination URL",
        ),
    ]
    if apply_replacements(p, replacements, already_applied_marker=marker):
        any_change = True
    touched_py_files.append(p)

    # ------------------------------------------------------------------
    # Verify compile; revert on failure
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
        print("Done. Restart the backend to pick this up (no frontend changes needed —")
        print("Alerts/AccountDetail/ServiceDetail all already call the same two")
        print("console-url endpoints, which now just return a plain AWS URL).")
        print("")
        print("Reminder: this only shows the resource correctly if the person clicking")
        print("Console is already signed into AWS Console (IAM user or SSO) with access")
        print("to account 924922671984 (aurogov) — otherwise they'll land on AWS's own")
        print("sign-in page instead of the resource, which is the correct behavior now.")
    else:
        print("Nothing to do — already patched.")


if __name__ == "__main__":
    main()
