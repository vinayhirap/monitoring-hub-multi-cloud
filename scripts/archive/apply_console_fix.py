#!/usr/bin/env python3
"""
apply_console_fix.py

Applies the "Console button opens wrong AWS account" fix to a local
checkout of monitoring-hub.

What it does:
  1. Creates app/aws/federation.py (new file).
  2. Edits app/api/alerts.py:
       - adds the federation import
       - adds GET /alerts/{alert_id}/console-url endpoint
  3. Edits frontend/src/pages/Alerts.jsx:
       - replaces the raw awsConsoleUrl() builder with hasConsoleTarget()
       - adds `openingConsole` state
       - adds openConsole() handler
       - swaps the static <a href={consoleUrl}> Console link for a button
         that calls the new backend endpoint

Safe to re-run: each step checks whether it was already applied and skips
it if so. If a step's expected surrounding text isn't found (because your
local file has diverged), it prints a clear warning with instructions
instead of guessing / corrupting the file.

Usage:
    python apply_console_fix.py [path-to-repo-root]

If no path is given, it uses the current directory.
"""

import sys
from pathlib import Path

# ── resolve repo root ───────────────────────────────────────────
repo_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(".").resolve()

alerts_py_path   = repo_root / "app" / "api" / "alerts.py"
federation_path  = repo_root / "app" / "aws" / "federation.py"
alerts_jsx_path  = repo_root / "frontend" / "src" / "pages" / "Alerts.jsx"

results = []  # (label, "ok" | "skip" | "FAIL", detail)


def report(label, status, detail=""):
    results.append((label, status, detail))
    tag = {"ok": "✅", "skip": "⏭ ", "FAIL": "❌"}[status]
    print(f"{tag} {label}" + (f" — {detail}" if detail else ""))


def replace_once(path: Path, old: str, new: str, label: str, already_marker: str = None):
    """
    Replace `old` with `new` in `path`, exactly once.
    If `already_marker` is found in the file, treat as already-applied and skip.
    If `old` isn't found (and marker isn't either), report FAIL with guidance.
    """
    if not path.exists():
        report(label, "FAIL", f"file not found: {path}")
        return

    text = path.read_text(encoding="utf-8")

    if already_marker and already_marker in text:
        report(label, "skip", "already applied")
        return

    count = text.count(old)
    if count == 0:
        report(label, "FAIL",
                f"expected text not found in {path.name} — your local file has "
                f"diverged here. Apply this change manually (see script source "
                f"for the exact old/new text under label '{label}').")
        return
    if count > 1:
        report(label, "FAIL",
                f"expected text found {count} times (expected exactly once) in "
                f"{path.name} — skipping to avoid ambiguous edit. Apply manually.")
        return

    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    report(label, "ok")


# ═════════════════════════════════════════════════════════════════
# STEP 1 — new file: app/aws/federation.py
# ═════════════════════════════════════════════════════════════════
FEDERATION_PY = '''# app/aws/federation.py
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

from app.aws.sts import assume_role

logger = logging.getLogger(__name__)

FEDERATION_ENDPOINT = "https://signin.aws.amazon.com/federation"
ISSUER = "monitoring-hub"
SESSION_DURATION_SECONDS = 3600  # must be <= the assumed role's max session duration


def resource_console_destination(resource: str, region: str) -> str:
    """
    Resource-type-specific AWS Console deep link.

    Mirrors the mapping in frontend/src/pages/Alerts.jsx (awsConsoleUrl) —
    keep both in sync if a new resource type is added.
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


def build_federated_console_url(role_arn: str, external_id: str | None,
                                 destination: str) -> str:
    """
    Assumes `role_arn` (the alert's own AWS account), exchanges the temporary
    credentials for a sign-in token, and returns a login URL that drops the
    user directly onto `destination` inside the CORRECT account — no
    dependence on whatever account the browser is currently signed into.
    """
    session = assume_role(role_arn, external_id)
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

federation_path.parent.mkdir(parents=True, exist_ok=True)
if federation_path.exists() and "build_federated_console_url" in federation_path.read_text(encoding="utf-8"):
    report("Create app/aws/federation.py", "skip", "already exists")
else:
    federation_path.write_text(FEDERATION_PY, encoding="utf-8")
    report("Create app/aws/federation.py", "ok")


# ═════════════════════════════════════════════════════════════════
# STEP 2 — app/api/alerts.py: add import
# ═════════════════════════════════════════════════════════════════
replace_once(
    alerts_py_path,
    old="from fastapi import APIRouter, HTTPException\nfrom app.db import get_connection",
    new=("from fastapi import APIRouter, HTTPException\n"
         "from app.db import get_connection\n"
         "from app.aws.federation import build_federated_console_url, resource_console_destination"),
    label="alerts.py: add federation import",
    already_marker="from app.aws.federation import build_federated_console_url",
)

# ═════════════════════════════════════════════════════════════════
# STEP 3 — app/api/alerts.py: add /console-url endpoint before ACK section
# ═════════════════════════════════════════════════════════════════
ACK_MARKER = "# ── ACK ───────────────────────────────────────────────────────"

CONSOLE_ENDPOINT = '''# ── AWS CONSOLE DEEP-LINK (account-correct) ────────────────────
@router.get("/{alert_id}/console-url")
def get_console_url(alert_id: int):
    """
    Returns a federated sign-in URL that opens THIS alert's resource in
    THIS alert's AWS account — regardless of which account the operator's
    browser currently happens to be signed into.
    """
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            a.resource_id                          AS resource,
            COALESCE(a.region, acc.default_region) AS region,
            acc.account_id                         AS aws_account_id,
            acc.role_arn,
            acc.external_id
        FROM alerts a
        JOIN resources r      ON r.resource_id = a.resource_id
        JOIN aws_accounts acc ON acc.id = r.aws_account_id
        WHERE a.id = %s
    """, (alert_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")
    if not row.get("role_arn"):
        raise HTTPException(status_code=400, detail="No AWS role configured for this account")

    destination = resource_console_destination(row["resource"], row["region"])

    try:
        url = build_federated_console_url(row["role_arn"], row["external_id"], destination)
    except Exception:
        logger.exception("Failed to build federated console URL for alert %s", alert_id)
        raise HTTPException(status_code=502, detail="Could not generate AWS console link")

    return {"url": url, "account_id": row["aws_account_id"]}


''' + ACK_MARKER

replace_once(
    alerts_py_path,
    old=ACK_MARKER,
    new=CONSOLE_ENDPOINT,
    label="alerts.py: add /console-url endpoint",
    already_marker='@router.get("/{alert_id}/console-url")',
)


# ═════════════════════════════════════════════════════════════════
# STEP 4 — Alerts.jsx: replace awsConsoleUrl() with hasConsoleTarget()
# ═════════════════════════════════════════════════════════════════
OLD_AWS_CONSOLE_URL_FN = '''// ── AWS console deep-link ──────────────────────────────────────
function awsConsoleUrl(resource, region = "") {
  if (!resource) return null;
  if (resource.startsWith("i-"))
    return `https://${region}.console.aws.amazon.com/ec2/home?region=${region}#Instances:instanceId=${resource}`;
  if (resource.startsWith("vol-"))
    return `https://${region}.console.aws.amazon.com/ec2/home?region=${region}#Volumes:volumeId=${resource}`;
  if (resource.includes("lambda") || resource.startsWith("arn:aws:lambda")) {
    const fn = resource.split(":").pop();
    return `https://${region}.console.aws.amazon.com/lambda/home?region=${region}#/functions/${fn}`;
  }
  if (resource.startsWith("db-") || resource.includes("rds"))
    return `https://${region}.console.aws.amazon.com/rds/home?region=${region}#database:`;
  return null;
}'''

NEW_HAS_CONSOLE_TARGET_FN = '''// ── AWS console deep-link ──────────────────────────────────────
// NOTE: We no longer build a raw console.aws.amazon.com URL on the client.
// A plain URL like that has no account context — clicking it just opens
// whatever AWS account the browser is already signed into, which is why
// the button used to land on the WRONG account. Instead we ask the backend
// for a federated sign-in link scoped to THIS alert's account
// (see openConsole / GET /alerts/{id}/console-url).
function hasConsoleTarget(resource) {
  if (!resource) return false;
  return (
    resource.startsWith("i-") ||
    resource.startsWith("vol-") ||
    resource.includes("lambda") ||
    resource.startsWith("arn:aws:lambda") ||
    resource.startsWith("db-") ||
    resource.includes("rds")
  );
}'''

replace_once(
    alerts_jsx_path,
    old=OLD_AWS_CONSOLE_URL_FN,
    new=NEW_HAS_CONSOLE_TARGET_FN,
    label="Alerts.jsx: replace awsConsoleUrl() with hasConsoleTarget()",
    already_marker="function hasConsoleTarget(resource)",
)

# ═════════════════════════════════════════════════════════════════
# STEP 5 — Alerts.jsx: add openingConsole state
# ═════════════════════════════════════════════════════════════════
replace_once(
    alerts_jsx_path,
    old='  const [acting,  setActing]  = useState(null);\n  const [soundOn, setSoundOn] = useState(true);',
    new=('  const [acting,  setActing]  = useState(null);\n'
         '  const [soundOn, setSoundOn] = useState(true);\n'
         '  const [openingConsole, setOpeningConsole] = useState(null);'),
    label="Alerts.jsx: add openingConsole state",
    already_marker="const [openingConsole, setOpeningConsole] = useState(null);",
)

# ═════════════════════════════════════════════════════════════════
# STEP 6 — Alerts.jsx: add openConsole() handler before handleResolve
# ═════════════════════════════════════════════════════════════════
OPEN_CONSOLE_FN = '''  // Opens THIS alert's resource in THIS alert's AWS account. We can't just
  // link straight to console.aws.amazon.com — that ignores which account
  // is intended and opens whatever account the browser is already signed
  // into. Instead we ask the backend for a federated sign-in URL scoped to
  // the correct account, then open that.
  async function openConsole(id) {
    // Open the tab synchronously (on the click) so browsers don't block it
    // as a popup once the async fetch resolves.
    const tab = window.open("", "_blank");
    setOpeningConsole(id);
    try {
      const { url } = await apiFetch(`/api/alerts/${id}/console-url`);
      if (tab) tab.location.href = url;
      else window.open(url, "_blank", "noopener,noreferrer");
    } catch (e) {
      if (tab) tab.close();
      alert("Couldn't open AWS console: " + e.message);
    } finally {
      setOpeningConsole(null);
    }
  }

  async function handleResolve(id) {'''

replace_once(
    alerts_jsx_path,
    old="  async function handleResolve(id) {",
    new=OPEN_CONSOLE_FN,
    label="Alerts.jsx: add openConsole() handler",
    already_marker="async function openConsole(id) {",
)

# ═════════════════════════════════════════════════════════════════
# STEP 7 — Alerts.jsx: swap route/consoleUrl vars in the render map
# ═════════════════════════════════════════════════════════════════
replace_once(
    alerts_jsx_path,
    old='                  const route      = detailRoute(a.resource, a.account_id);\n'
        '                  const consoleUrl = awsConsoleUrl(a.resource, a.region);',
    new='                  const route        = detailRoute(a.resource, a.account_id);\n'
        '                  const canOpenAws   = hasConsoleTarget(a.resource);\n'
        '                  const isOpeningAws = openingConsole === a.id;',
    label="Alerts.jsx: swap route/consoleUrl → route/canOpenAws/isOpeningAws",
    already_marker="const canOpenAws   = hasConsoleTarget(a.resource);",
)

# ═════════════════════════════════════════════════════════════════
# STEP 8 — Alerts.jsx: replace the <a href={consoleUrl}> with a button
# ═════════════════════════════════════════════════════════════════
OLD_CONSOLE_ANCHOR = '''                          {consoleUrl && (
                            <a
                              href={consoleUrl}
                              target="_blank"
                              rel="noreferrer"
                              className="btn-console-aws"
                              onClick={e => e.stopPropagation()}
                              title="Open in AWS Management Console"
                            >
                              ☁ Console
                            </a>
                          )}'''

NEW_CONSOLE_BUTTON = '''                          {canOpenAws && (
                            <button
                              className="btn-console-aws"
                              disabled={isOpeningAws}
                              onClick={e => { e.stopPropagation(); openConsole(a.id); }}
                              title="Open in AWS Management Console (correct account)"
                            >
                              {isOpeningAws ? "☁ Opening…" : "☁ Console"}
                            </button>
                          )}'''

replace_once(
    alerts_jsx_path,
    old=OLD_CONSOLE_ANCHOR,
    new=NEW_CONSOLE_BUTTON,
    label="Alerts.jsx: replace <a> console link with openConsole() button",
    already_marker="onClick={e => { e.stopPropagation(); openConsole(a.id); }}",
)


# ═════════════════════════════════════════════════════════════════
print()
n_fail = sum(1 for _, s, _ in results if s == "FAIL")
n_ok   = sum(1 for _, s, _ in results if s == "ok")
n_skip = sum(1 for _, s, _ in results if s == "skip")
print(f"Done. {n_ok} applied, {n_skip} already applied, {n_fail} failed.")
if n_fail:
    print("\nFor any FAILED step above, your local file differs from what this "
          "script expects at that spot. Open the file, find the described "
          "location, and apply the change by hand (or paste me the current "
          "surrounding code and I'll adjust).")
    sys.exit(1)
