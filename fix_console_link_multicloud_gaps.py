#!/usr/bin/env python3
"""
fix_console_link_multicloud_gaps.py
========================================
Monitoring Hub -- frontend audit finding: the "click through to cloud
console" feature is comprehensively AWS-only in three places, even though
the underlying provider abstraction (get_console_url) already supports
all three clouds -- Azure fully generically, and GCP with real dispatch
for 10 of 16 curated services as of this audit's Phase 4 fix.

This is now much more consequential than it would have been before this
session: Phase 1 makes far more Azure/GCP resources show up with enabled
metrics, and Phase 5 makes their alerts able to fire for the first time
ever. Without this fix, none of that newly-real data would have a working
"open in console" button anywhere in the UI.

THREE SEPARATE BUGS, SAME ROOT CAUSE
-------------------------------------
1. BACKEND -- app/api/alerts.py's GET /api/alerts/{id}/console-url never
   got migrated to the provider-dispatch pattern. It directly calls
   app.aws.federation's AWS-only functions, bypassing get_provider()
   entirely. For any Azure/GCP alert this either 500s or produces a
   nonsensical URL. (Contrast with app/api/admin/accounts.py's sibling
   endpoint, whose own docstring says "Dispatches through the provider
   layer so this also works for Azure/GCP" -- that one was done right;
   this one was apparently never brought in line with it.)

2. FRONTEND -- ServiceList.jsx's openInConsole() opens with
   `if (provider !== "aws") return;` -- a silent no-op for every
   Azure/GCP service tile. The backend endpoint it calls
   (/api/admin/accounts/{id}/console-url) already works for all three
   providers; this bailout was never removed.

3. FRONTEND -- Alerts.jsx's hasConsoleTarget() gates the per-alert
   console button on AWS resource-ID SHAPE (`i-...`, `vol-...`,
   `arn:aws:lambda...` etc.). An Azure or GCP alert's resource_id never
   matches any of these patterns, so the button never renders at all for
   non-AWS alerts -- not even a disabled/failing one, it's just absent.

FIX
---
1. app/api/alerts.py: migrated to the same get_provider() dispatch the
   accounts endpoint already uses correctly. Selects the full account
   row (every provider's credential columns live in the one aws_accounts
   table) instead of only the AWS-specific columns the old query picked.

2. ServiceList.jsx: removed the AWS-only bailout. The button now attempts
   the same real backend call for every provider and relies on the
   existing try/catch (already present, already shows a clear error) for
   graceful failure -- same pattern already used successfully elsewhere
   in this codebase (ServiceDetail.jsx's openAccountConsole).

3. Alerts.jsx: replaced ID-shape guessing with "show the button whenever
   the alert has a resource at all," same graceful-failure-on-click
   philosophy. Also fixed the hardcoded "AWS Management Console" /
   "Couldn't open AWS console" wording to be provider-neutral, since the
   button is no longer AWS-only.

NOT changed: AccountDetail.jsx's hardcoded "ec2"/"AWS Console" button --
that page is a dedicated EC2-only legacy detail view (confirmed by its
own header, "EC2 -- N instances"), not a generic multi-provider page, so
the AWS-specific wording there is accurate, not a bug.

TESTED: the backend endpoint's new query and dispatch logic exercised
against mocked account rows for all three providers (confirms it builds
the right provider object and passes the full account dict through,
matching the already-correct accounts.py endpoint's own pattern
byte-for-byte). Frontend changes are small, mechanical diffs (removing a
bailout, widening a boolean condition, and text-only wording changes) --
verify by clicking through in the browser for an Azure/GCP account and
alert once Phase 1/5 are also deployed and have real data flowing.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_console_link_multicloud_gaps.py --dry-run
    python3 fix_console_link_multicloud_gaps.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

# ─────────────────────────── backend: app/api/alerts.py ───────────────────────────

BACKEND_IMPORT_OLD = '''from app.aws.federation import (
    build_federated_console_url,
    resource_console_destination,
    NoConsoleCredentialsError,
)
from app.ws.publisher import publish_alert_resolved'''

BACKEND_IMPORT_NEW = '''from app.aws.federation import NoConsoleCredentialsError
from app.ws.publisher import publish_alert_resolved'''

BACKEND_OLD_FUNC = '''@router.get("/{alert_id}/console-url")
def get_console_url(alert_id: int, user: dict = Depends(require_permission("alerts.view"))):
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
            r.resource_type                        AS resource_type,
            r.name                                  AS resource_name,
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

    destination = resource_console_destination(
        row.get("resource_type"), row["resource"], row["region"],
        resource_name=row.get("resource_name"),
    )

    try:
        url = build_federated_console_url(
            row.get("role_arn"), row.get("external_id"), destination,
            target_account_id=row.get("aws_account_id"),
            requested_by=user["username"],
            service=row.get("resource_type"), resource_id=row["resource"],
            region=row["region"], resource_name=row.get("resource_name"),
        )
    except NoConsoleCredentialsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Failed to build federated console URL for alert %s", alert_id)
        raise HTTPException(status_code=502, detail="Could not generate AWS console link")'''

BACKEND_NEW_FUNC = '''@router.get("/{alert_id}/console-url")
def get_console_url(alert_id: int, user: dict = Depends(require_permission("alerts.view"))):
    """
    Returns a console deep link that opens THIS alert's resource in THIS
    alert's account -- regardless of which account/cloud the operator's
    browser currently happens to be signed into.

    Dispatches through the provider layer (get_provider().get_console_url)
    the same way app/api/admin/accounts.py's sibling endpoint already
    does -- this one was the one place that migration was never finished,
    which meant no Azure/GCP alert could ever produce a working console
    link (AWS's federation helpers were being called directly regardless
    of the alert's actual account provider).
    """
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            a.resource_id                          AS resource,
            r.resource_type                        AS resource_type,
            r.name                                  AS resource_name,
            COALESCE(a.region, acc.default_region) AS region,
            acc.*
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

    try:
        from app.providers.registry import get_provider
        provider = get_provider(row.get("provider") or "aws")
        url = provider.get_console_url(
            row, row["resource"], row["region"],
            service=row.get("resource_type"), resource_name=row.get("resource_name"),
            requested_by=user["username"],
        )
    except NoConsoleCredentialsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Failed to build console URL for alert %s", alert_id)
        raise HTTPException(status_code=502, detail="Could not generate console link")'''

BACKEND_RETURN_OLD = '''    return {"url": url, "account_id": row["aws_account_id"]}'''

BACKEND_RETURN_NEW = '''    return {"url": url, "account_id": row["account_id"]}'''

# ─────────────────────────── frontend: ServiceList.jsx ───────────────────────────

SERVICELIST_OLD = '''  function openInConsole(serviceId) {
    if (provider !== "aws") return;
    setConsoleLoading(serviceId);'''

SERVICELIST_NEW = '''  function openInConsole(serviceId) {
    // Previously bailed out here for any non-AWS provider. The backend
    // endpoint this calls (getConsoleUrl -> /api/admin/accounts/{id}/
    // console-url) already dispatches through get_provider() and works
    // for Azure/GCP too -- Azure generically, GCP with real per-service
    // deep links for 10 of 16 curated types. Removed the bailout; the
    // existing catch below already surfaces a clear error if a
    // particular service genuinely has no console link available yet.
    setConsoleLoading(serviceId);'''

# ─────────────────────────── frontend: Alerts.jsx ───────────────────────────

ALERTS_HASCONSOLE_OLD = '''function hasConsoleTarget(resource) {
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

ALERTS_HASCONSOLE_NEW = '''function hasConsoleTarget(resource) {
  // Previously guessed AWS resource-ID shapes (i-.../vol-.../arn:aws:...)
  // -- an Azure ARM path or GCP asset name never matches any of those,
  // so the console button silently never appeared for non-AWS alerts at
  // all. The backend endpoint this gates (/api/alerts/{id}/console-url)
  // now dispatches through get_provider() for any cloud, so any alert
  // with a resource at all is worth attempting -- the existing
  // try/catch in openConsole() below already surfaces a clear error for
  // the genuine case where a console link truly isn't available.
  return !!resource;
}'''

ALERTS_ERROR_OLD = '''      alert("Couldn't open AWS console: " + e.message);'''
ALERTS_ERROR_NEW = '''      alert("Couldn't open console: " + e.message);'''

ALERTS_TITLE_OLD = '''                              title="Open in AWS Management Console (correct account)"'''
ALERTS_TITLE_NEW = '''                              title="Open in cloud console (correct account)"'''


def die(msg):
    print(f"\n[ABORT] {msg}", file=sys.stderr)
    sys.exit(1)


def find_repo_root():
    cur = os.path.abspath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(cur, "app", "auth", "security.py")) and \
           os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            die("Could not locate the monitoring-hub-multi-cloud repo root.")
        cur = parent


def backup(path):
    bpath = path + f".bak.{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(path, bpath)
    return bpath


def patch_file(path, label, replacements, done_marker):
    """replacements: list of (old, new). Returns (new_content_or_None, note)."""
    if not os.path.exists(path):
        die(f"{label} not found at {path}.")
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    if done_marker in content:
        return None, f"{label} already patched -- skipping."
    new_content = content
    for old, new in replacements:
        n = new_content.count(old)
        if n != 1:
            die(f"{label}: expected exactly 1 match for one of the expected blocks, found {n}. "
                f"File may differ from what this script expects.")
        new_content = new_content.replace(old, new, 1)
    return new_content, f"{label}: OK ({len(new_content) - len(content):+d} bytes)"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply_ = args.apply and not args.dry_run

    repo_root = find_repo_root()
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    backend_path = os.path.join(repo_root, "app", "api", "alerts.py")
    servicelist_path = os.path.join(repo_root, "frontend", "src", "pages", "ServiceList.jsx")
    alerts_jsx_path = os.path.join(repo_root, "frontend", "src", "pages", "Alerts.jsx")

    results = []

    backend_content, backend_note = patch_file(
        backend_path, "app/api/alerts.py",
        [
            (BACKEND_IMPORT_OLD, BACKEND_IMPORT_NEW),
            (BACKEND_OLD_FUNC, BACKEND_NEW_FUNC),
            (BACKEND_RETURN_OLD, BACKEND_RETURN_NEW),
        ],
        "Dispatches through the provider layer",
    )
    results.append((backend_path, "app/api/alerts.py", backend_content, backend_note))

    sl_content, sl_note = patch_file(
        servicelist_path, "frontend/src/pages/ServiceList.jsx",
        [(SERVICELIST_OLD, SERVICELIST_NEW)],
        "Previously bailed out here for any non-AWS provider",
    )
    results.append((servicelist_path, "frontend/src/pages/ServiceList.jsx", sl_content, sl_note))

    al_content, al_note = patch_file(
        alerts_jsx_path, "frontend/src/pages/Alerts.jsx",
        [
            (ALERTS_HASCONSOLE_OLD, ALERTS_HASCONSOLE_NEW),
            (ALERTS_ERROR_OLD, ALERTS_ERROR_NEW),
            (ALERTS_TITLE_OLD, ALERTS_TITLE_NEW),
        ],
        "now dispatches through get_provider() for any cloud",
    )
    results.append((alerts_jsx_path, "frontend/src/pages/Alerts.jsx", al_content, al_note))

    print("\nPatch plan:")
    for _, _, _, note in results:
        print(f"  {note}")

    if all(content is None for _, _, content, _ in results):
        print("\nNothing to do -- everything this script would add is already present.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    for path, label, content, note in results:
        if content is None:
            continue
        backup(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"Patched {label}")

    print("""
[Manual follow-up]

  A) Backend: restart to pick up the changed endpoint:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 20 --no-pager

  B) Frontend: rebuild and redeploy the static bundle the same way you
     already do for other frontend changes.

  C) Verify: click "Console" on an AWS alert -- should behave exactly as
     before (no regression). Once Phase 1/5 are deployed and an
     Azure/GCP alert exists, click its Console button too -- previously
     absent entirely, should now open (or fail with a clear message
     rather than being invisible). Same for an Azure/GCP service tile
     on the Services page.

  D) Review, commit, push:
       git status
       git diff app/api/alerts.py frontend/src/pages/ServiceList.jsx frontend/src/pages/Alerts.jsx
       git add app/api/alerts.py frontend/src/pages/ServiceList.jsx frontend/src/pages/Alerts.jsx \\
               fix_console_link_multicloud_gaps.py
       git commit -m "fix(frontend): console-link click-through was AWS-only in 3 places despite the backend already supporting all providers"
       git push origin main
""")


if __name__ == "__main__":
    main()
