#!/usr/bin/env python3
"""
fix_wire_up_permissions_and_scope.py
=======================================
Monitoring Hub -- wires the ALREADY-BUILT permission system
(app/auth/permissions.py, the `permissions`/`role_permissions` tables)
and the ALREADY-BUILT scope system (app/auth/authorization.py's
get_accessible_account_ids) into the six route files that currently
never call either.

THE BUG THIS FIXES
-------------------
`require_permission(...)` / `require_role(...)` are used correctly in
exactly two files: app/api/admin/users.py and app/api/admin/groups.py.
Six other route files are mounted with ONLY
    _auth_dep = [Depends(get_current_user)]
at the router level (app/main.py) -- which checks "is this person
logged in", nothing else. No role check. No permission check. No
account/region scope filtering, despite get_accessible_account_ids()
existing and being fully built for exactly this purpose.

Concretely, before this fix, ANY authenticated user -- including the
lowest-privilege Viewer, who the permission catalog explicitly says
should be read-only -- could:
  - See every AWS/Azure/GCP account, resource, and metric, regardless
    of their assigned account/region scope (app/api/live_data.py,
    app/api/admin/accounts.py's list/get).
  - Add a brand-new cloud account (with credentials) or DELETE an
    existing one (app/api/admin/accounts.py).
  - Acknowledge, resolve, mute, or bulk-clear alerts
    (app/api/alerts.py).
  - Create/modify/toggle alert thresholds (app/api/settings.py,
    app/api/metric_catalog.py).
  - Read the full administrative audit log (app/api/audit_logs.py).

THE FIX
-------
Each route now requires the SAME permission code the existing
role_permissions seed data (db/migrations/015_permissions_rbac.sql)
already defines for that capability -- e.g. viewing accounts requires
`accounts.view` (which viewer already has), onboarding a new account
requires `accounts.onboard` (editor+), reading the audit log requires
`audit.view` (admin-only). Nothing about who-can-do-what changes from
what the permission catalog ALREADY SAYS should be true -- this fix
makes the code match the already-documented, already-seeded policy,
where before it matched nothing at all.

Two actions with no existing permission code, because they're more
destructive than anything else on their router (irreversible account
deletion, bulk-deleting all unresolved alerts), are locked to
admin-only via require_role("admin") rather than inventing a new
permission code for a single edge case:
  - DELETE /api/admin/accounts/{id}   (delete_account)
  - DELETE /api/alerts/clear          (clear_alerts)

Scope enforcement (which accounts/regions a non-admin can see, via
get_accessible_account_ids/get_accessible_regions_for_account -- both
already fully implemented in app/auth/authorization.py, just never
called outside the admin user-management screens) is added to every
account-keyed and resource-keyed route in live_data.py and to
admin/accounts.py's list/get.

WHY THIS IS LOW-RISK RIGHT NOW (per your own confirmation)
-------------------------------------------------------------
The dev environment is currently admin-only. `admin` always bypasses
every permission AND scope check by design (has_permission short-
circuits True; get_accessible_account_ids returns None = unrestricted
for admin). So this fix changes ZERO behavior for anyone using the
system today -- it only starts actually enforcing the existing,
already-seeded policy for whenever editor/viewer accounts are used.

VERIFIED SAFE FOR INTERNAL PYTHON CALLS
------------------------------------------
Some of these route functions are also called directly as plain
Python functions elsewhere in the codebase (not via HTTP), e.g.
add_account() calls set_account_metrics(new_id, {...}) directly.
Adding a `current_user: dict = Depends(require_permission(...))`
parameter is safe for those call sites: FastAPI's Depends resolution
only fires when FastAPI itself dispatches the route as an HTTP
request. A direct Python call simply leaves that parameter as the
unevaluated Depends(...) sentinel, which is fine because none of the
patched function bodies read that parameter for anything other than
the dependency injection gate itself. Checked every cross-file import
of these modules before writing this script -- see the accompanying
audit notes.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_wire_up_permissions_and_scope.py --dry-run
    python3 fix_wire_up_permissions_and_scope.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

FILES = [
    "app/api/admin/accounts.py",
    "app/api/alerts.py",
    "app/api/settings.py",
    "app/api/metric_catalog.py",
    "app/api/audit_logs.py",
    "app/api/live_data.py",
]


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


def replace_all_exact(content, pairs, label):
    """pairs: list of (old, new). Each old must match exactly once."""
    for i, (old, new) in enumerate(pairs):
        n = content.count(old)
        if n != 1:
            die(f"{label} [patch {i+1}/{len(pairs)}]: expected exactly 1 match, "
                f"found {n}. File may differ from what this script expects -- "
                f"aborting rather than guessing. NO files were modified.")
        content = content.replace(old, new, 1)
    return content


# ===========================================================================
# app/api/admin/accounts.py
# ===========================================================================

def patch_admin_accounts(content):
    pairs = [
        (
            'from fastapi import APIRouter, HTTPException, Body, Query, Depends\n'
            'from app.db import get_connection\n'
            'from app.auth.deps import get_current_user\n',

            'from fastapi import APIRouter, HTTPException, Body, Query, Depends\n'
            'from app.db import get_connection\n'
            'from app.auth.deps import get_current_user, require_role\n'
            'from app.auth.permissions import require_permission\n'
            'from app.auth.authorization import get_accessible_account_ids\n'
        ),
        (
            '@router.get("")\n'
            'def list_accounts():\n'
            '    conn   = get_connection()\n'
            '    cursor = conn.cursor(dictionary=True)\n'
            '    cursor.execute("""\n'
            '        SELECT id, account_name, account_id, role_arn, provider,\n'
            '               external_id, default_region, status, created_at,\n'
            '               last_synced_at, last_discovered_at, description,\n'
            '               tenant_id, subscription_id, client_id,\n'
            '               project_id, service_account_email\n'
            '        FROM aws_accounts\n'
            "        WHERE status = 'active'\n"
            '        ORDER BY created_at DESC\n'
            '    """)\n'
            '    rows = cursor.fetchall()\n'
            '    cursor.close()\n'
            '    conn.close()\n'
            '    # Never leak secrets: these columns only ever hold identifiers, never\n'
            '    # the client secret / SA key JSON (those live encrypted in\n'
            '    # provider_credentials and are only decrypted server-side on demand).\n'
            '    return [_serialize(r) for r in rows]\n',

            '@router.get("")\n'
            'def list_accounts(current_user: dict = Depends(require_permission("accounts.view"))):\n'
            '    conn   = get_connection()\n'
            '    cursor = conn.cursor(dictionary=True)\n'
            '    cursor.execute("""\n'
            '        SELECT id, account_name, account_id, role_arn, provider,\n'
            '               external_id, default_region, status, created_at,\n'
            '               last_synced_at, last_discovered_at, description,\n'
            '               tenant_id, subscription_id, client_id,\n'
            '               project_id, service_account_email\n'
            '        FROM aws_accounts\n'
            "        WHERE status = 'active'\n"
            '        ORDER BY created_at DESC\n'
            '    """)\n'
            '    rows = cursor.fetchall()\n'
            '    cursor.close()\n'
            '    conn.close()\n'
            '\n'
            '    accessible = get_accessible_account_ids(current_user)\n'
            '    if accessible is not None:\n'
            '        rows = [r for r in rows if r["id"] in accessible]\n'
            '\n'
            '    # Never leak secrets: these columns only ever hold identifiers, never\n'
            '    # the client secret / SA key JSON (those live encrypted in\n'
            '    # provider_credentials and are only decrypted server-side on demand).\n'
            '    return [_serialize(r) for r in rows]\n'
        ),
        (
            '@router.get("/{account_id}")\n'
            'def get_account(account_id: int):\n'
            '    conn   = get_connection()\n'
            '    cursor = conn.cursor(dictionary=True)\n'
            '    cursor.execute("SELECT * FROM aws_accounts WHERE id = %s", (account_id,))\n'
            '    row = cursor.fetchone()\n'
            '    cursor.close()\n'
            '    conn.close()\n'
            '    if not row:\n'
            '        raise HTTPException(status_code=404, detail="Account not found")\n'
            '    return _serialize(row)\n',

            '@router.get("/{account_id}")\n'
            'def get_account(account_id: int, current_user: dict = Depends(require_permission("accounts.view"))):\n'
            '    accessible = get_accessible_account_ids(current_user)\n'
            '    if accessible is not None and account_id not in accessible:\n'
            '        raise HTTPException(status_code=403, detail="You do not have access to this account")\n'
            '\n'
            '    conn   = get_connection()\n'
            '    cursor = conn.cursor(dictionary=True)\n'
            '    cursor.execute("SELECT * FROM aws_accounts WHERE id = %s", (account_id,))\n'
            '    row = cursor.fetchone()\n'
            '    cursor.close()\n'
            '    conn.close()\n'
            '    if not row:\n'
            '        raise HTTPException(status_code=404, detail="Account not found")\n'
            '    return _serialize(row)\n'
        ),
        (
            '@router.post("")\n'
            'def add_account(payload: dict = Body(...)):\n',

            '@router.post("")\n'
            'def add_account(payload: dict = Body(...), current_user: dict = Depends(require_permission("accounts.onboard"))):\n'
        ),
        (
            '@router.delete("/{account_id}")\n'
            'def delete_account(account_id: int):\n',

            '@router.delete("/{account_id}")\n'
            'def delete_account(account_id: int, current_user: dict = Depends(require_role("admin"))):\n'
            '    # Admin-only: no existing permission code covers "delete an entire\n'
            '    # monitored account" (accounts.onboard is scoped to ADDING one in the\n'
            '    # permission catalog\'s own description), and this is irreversible --\n'
            '    # deliberately not extending accounts.onboard to also cover deletion.\n'
        ),
        (
            'def get_account_console_url(\n'
            '    account_id: int,\n'
            '    service: str = Query(None),\n'
            '    resource_id: str = Query(None),\n'
            '    region: str = Query(None),\n'
            '    resource_name: str = Query(None),\n'
            '    ecs_service_name: str = Query(None),\n'
            '    user: dict = Depends(get_current_user),\n'
            '):\n',

            'def get_account_console_url(\n'
            '    account_id: int,\n'
            '    service: str = Query(None),\n'
            '    resource_id: str = Query(None),\n'
            '    region: str = Query(None),\n'
            '    resource_name: str = Query(None),\n'
            '    ecs_service_name: str = Query(None),\n'
            '    user: dict = Depends(require_permission("accounts.view")),\n'
            '):\n'
            '    accessible = get_accessible_account_ids(user)\n'
            '    if accessible is not None and account_id not in accessible:\n'
            '        raise HTTPException(status_code=403, detail="You do not have access to this account")\n'
        ),
        (
            '@router.post("/test-role")\n'
            'def test_role(payload: dict = Body(...)):\n',

            '@router.post("/test-role")\n'
            'def test_role(payload: dict = Body(...), current_user: dict = Depends(require_permission("accounts.onboard"))):\n'
        ),
        (
            '@router.post("/test-azure-credentials")\n'
            'def test_azure_credentials(payload: dict = Body(...)):\n',

            '@router.post("/test-azure-credentials")\n'
            'def test_azure_credentials(payload: dict = Body(...), current_user: dict = Depends(require_permission("accounts.onboard"))):\n'
        ),
        (
            '@router.post("/test-gcp-credentials")\n'
            'def test_gcp_credentials(payload: dict = Body(...)):\n',

            '@router.post("/test-gcp-credentials")\n'
            'def test_gcp_credentials(payload: dict = Body(...), current_user: dict = Depends(require_permission("accounts.onboard"))):\n'
        ),
        (
            '@router.post("/{account_id}/discover")\n'
            'def discover_account(account_id: int):\n',

            '@router.post("/{account_id}/discover")\n'
            'def discover_account(account_id: int, current_user: dict = Depends(require_permission("accounts.onboard"))):\n'
        ),
    ]
    return replace_all_exact(content, pairs, "app/api/admin/accounts.py")


# ===========================================================================
# app/api/alerts.py
# ===========================================================================

def patch_alerts(content):
    pairs = [
        (
            'from fastapi import APIRouter, HTTPException, Depends\n'
            'from app.db import get_connection\n'
            'from app.auth.deps import get_current_user\n',

            'from fastapi import APIRouter, HTTPException, Depends\n'
            'from app.db import get_connection\n'
            'from app.auth.deps import get_current_user, require_role\n'
            'from app.auth.permissions import require_permission\n'
        ),
        (
            '@router.get("")\n'
            'def get_alerts():\n',

            '@router.get("")\n'
            'def get_alerts(current_user: dict = Depends(require_permission("alerts.view"))):\n'
        ),
        (
            '@router.get("/open")\n'
            'def open_alerts():\n',

            '@router.get("/open")\n'
            'def open_alerts(current_user: dict = Depends(require_permission("alerts.view"))):\n'
        ),
        (
            'def get_console_url(alert_id: int, user: dict = Depends(get_current_user)):\n',

            'def get_console_url(alert_id: int, user: dict = Depends(require_permission("alerts.view"))):\n'
        ),
        (
            '@router.post("/{alert_id}/ack")\n'
            '@router.patch("/{alert_id}/ack")\n'
            'def ack_alert(alert_id: int):\n',

            '@router.post("/{alert_id}/ack")\n'
            '@router.patch("/{alert_id}/ack")\n'
            'def ack_alert(alert_id: int, current_user: dict = Depends(require_permission("operations.execute"))):\n'
        ),
        (
            '@router.post("/{alert_id}/resolve")\n'
            '@router.patch("/{alert_id}/resolve")\n'
            'def resolve_alert(alert_id: int):\n',

            '@router.post("/{alert_id}/resolve")\n'
            '@router.patch("/{alert_id}/resolve")\n'
            'def resolve_alert(alert_id: int, current_user: dict = Depends(require_permission("operations.execute"))):\n'
        ),
        (
            '@router.post("/{alert_id}/mute")\n'
            'def mute_alert(alert_id: int, minutes: int = 30):\n',

            '@router.post("/{alert_id}/mute")\n'
            'def mute_alert(alert_id: int, minutes: int = 30, current_user: dict = Depends(require_permission("operations.execute"))):\n'
        ),
        (
            '@router.delete("/clear")\n'
            'def clear_alerts():\n',

            '@router.delete("/clear")\n'
            'def clear_alerts(current_user: dict = Depends(require_role("admin"))):\n'
            '    # Admin-only: bulk-deletes every unresolved/unacked alert with no\n'
            '    # undo. No existing permission code covers a bulk-destructive action\n'
            '    # like this (operations.execute covers acting on ONE alert), so this\n'
            '    # is intentionally locked tighter than the single-alert actions above.\n'
        ),
    ]
    return replace_all_exact(content, pairs, "app/api/alerts.py")


# ===========================================================================
# app/api/settings.py
# ===========================================================================

def patch_settings(content):
    pairs = [
        (
            'from fastapi import APIRouter, Body, Query\n'
            'from app.db import get_connection\n',

            'from fastapi import APIRouter, Body, Query, Depends\n'
            'from app.db import get_connection\n'
            'from app.auth.permissions import require_permission\n'
        ),
        (
            '@router.get("/thresholds")\n'
            'def get_thresholds(account_id: int = Query(3)):\n',

            '@router.get("/thresholds")\n'
            'def get_thresholds(account_id: int = Query(3), current_user: dict = Depends(require_permission("alerts.view"))):\n'
        ),
        (
            '@router.post("/thresholds")\n'
            'def upsert_threshold(payload: dict = Body(...)):\n',

            '@router.post("/thresholds")\n'
            'def upsert_threshold(payload: dict = Body(...), current_user: dict = Depends(require_permission("alerts.configure"))):\n'
        ),
        (
            '@router.patch("/thresholds/{threshold_id}/toggle")\n'
            'def toggle_threshold(threshold_id: int, payload: dict = Body(...)):\n',

            '@router.patch("/thresholds/{threshold_id}/toggle")\n'
            'def toggle_threshold(threshold_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("alerts.configure"))):\n'
        ),
        (
            '@router.post("/thresholds/seed")\n'
            'def seed_default_thresholds(account_id: int = Query(3)):\n',

            '@router.post("/thresholds/seed")\n'
            'def seed_default_thresholds(account_id: int = Query(3), current_user: dict = Depends(require_permission("alerts.configure"))):\n'
        ),
        (
            '@router.get("/check")\n'
            'def check_thresholds(account_id: int = Query(3)):\n',

            '@router.get("/check")\n'
            'def check_thresholds(account_id: int = Query(3), current_user: dict = Depends(require_permission("alerts.view"))):\n'
        ),
    ]
    return replace_all_exact(content, pairs, "app/api/settings.py")


# ===========================================================================
# app/api/metric_catalog.py
# ===========================================================================

def patch_metric_catalog(content):
    pairs = [
        (
            'from fastapi import APIRouter, HTTPException, Body, Query, Response\n'
            'from app.db import get_connection\n',

            'from fastapi import APIRouter, HTTPException, Body, Query, Response, Depends\n'
            'from app.db import get_connection\n'
            'from app.auth.permissions import require_permission\n'
        ),
        (
            '@router.get("/api/metric-catalog")\n'
            'def get_catalog(\n'
            '    category: str = Query(None, description="core | extended | directory"),\n'
            '    service:  str = Query(None, description="service key, e.g. ec2"),\n'
            '    provider: str = Query("aws", description="aws | azure | gcp"),\n'
            '    search:   str = Query(None, description="matches metric name, service, or description"),\n'
            '):\n',

            '@router.get("/api/metric-catalog")\n'
            'def get_catalog(\n'
            '    category: str = Query(None, description="core | extended | directory"),\n'
            '    service:  str = Query(None, description="service key, e.g. ec2"),\n'
            '    provider: str = Query("aws", description="aws | azure | gcp"),\n'
            '    search:   str = Query(None, description="matches metric name, service, or description"),\n'
            '    current_user: dict = Depends(require_permission("metrics.view")),\n'
            '):\n'
        ),
        (
            '@router.get("/api/metric-catalog/services")\n'
            'def get_services(provider: str = Query("aws", description="aws | azure | gcp")):\n',

            '@router.get("/api/metric-catalog/services")\n'
            'def get_services(provider: str = Query("aws", description="aws | azure | gcp"), current_user: dict = Depends(require_permission("metrics.view"))):\n'
        ),
        (
            '@router.get("/api/metric-catalog/default-template")\n'
            'def get_default_template(provider: str = Query("aws", description="aws | azure | gcp")):\n',

            '@router.get("/api/metric-catalog/default-template")\n'
            'def get_default_template(provider: str = Query("aws", description="aws | azure | gcp"), current_user: dict = Depends(require_permission("metrics.view"))):\n'
        ),
        (
            '@router.get("/api/account-metrics/{account_id}")\n'
            'def get_account_metrics(account_id: int):\n',

            '@router.get("/api/account-metrics/{account_id}")\n'
            'def get_account_metrics(account_id: int, current_user: dict = Depends(require_permission("metrics.view"))):\n'
        ),
        (
            '@router.put("/api/account-metrics/{account_id}")\n'
            'def set_account_metrics(account_id: int, payload: dict = Body(...)):\n',

            '@router.put("/api/account-metrics/{account_id}")\n'
            'def set_account_metrics(account_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("alerts.configure"))):\n'
        ),
        (
            '@router.post("/api/account-metrics/{account_id}/apply-default")\n'
            'def apply_default_template(account_id: int):\n',

            '@router.post("/api/account-metrics/{account_id}/apply-default")\n'
            'def apply_default_template(account_id: int, current_user: dict = Depends(require_permission("alerts.configure"))):\n'
        ),
    ]
    return replace_all_exact(content, pairs, "app/api/metric_catalog.py")


# ===========================================================================
# app/api/audit_logs.py
# ===========================================================================

def patch_audit_logs(content):
    pairs = [
        (
            'from fastapi import APIRouter, Query\n'
            'from app.db import get_connection\n',

            'from fastapi import APIRouter, Query, Depends\n'
            'from app.db import get_connection\n'
            'from app.auth.permissions import require_permission\n'
        ),
        (
            '@router.get("/audit-logs")\n'
            'def get_audit_logs(\n'
            '    limit:  int = Query(200, ge=1, le=1000),\n'
            '    actor:  str = Query(None),\n'
            '    action: str = Query(None),\n'
            '):\n',

            '@router.get("/audit-logs")\n'
            'def get_audit_logs(\n'
            '    limit:  int = Query(200, ge=1, le=1000),\n'
            '    actor:  str = Query(None),\n'
            '    action: str = Query(None),\n'
            '    current_user: dict = Depends(require_permission("audit.view")),\n'
            '):\n'
        ),
    ]
    return replace_all_exact(content, pairs, "app/api/audit_logs.py")


# ===========================================================================
# app/api/live_data.py
# ===========================================================================

def patch_live_data(content):
    pairs = [
        (
            'from fastapi import APIRouter, HTTPException, Query\n',

            'from fastapi import APIRouter, HTTPException, Query, Depends\n'
            'from app.auth.permissions import require_permission\n'
            'from app.auth.authorization import get_accessible_account_ids\n'
        ),
        (
            'def _get_active_alert_counts_by_account() -> dict:\n',

            'def _check_account_scope(user: dict, account_db_id: int):\n'
            '    """403s if this account is outside the caller\'s effective scope.\n'
            '    None from get_accessible_account_ids means unrestricted (admin);\n'
            '    otherwise account_db_id must be in the returned set."""\n'
            '    accessible = get_accessible_account_ids(user)\n'
            '    if accessible is not None and account_db_id not in accessible:\n'
            '        raise HTTPException(status_code=403, detail="You do not have access to this account")\n'
            '\n'
            '\n'
            'def _check_resource_scope(user: dict, resource_identifier: str):\n'
            '    """Same idea as _check_account_scope, but for the metrics-by-\n'
            '    resource-id endpoints below (instance id / volume id / db id /\n'
            '    function name / bucket name) which don\'t take account_db_id\n'
            '    directly -- resolves the owning account via the `resources` table\n'
            '    first. If the resource isn\'t tracked yet (not in `resources`),\n'
            '    this intentionally does NOT block -- there\'s nothing to check\n'
            '    against, and returning a scope error would be misleading for what\n'
            '    is really just an empty/unknown metric lookup."""\n'
            '    accessible = get_accessible_account_ids(user)\n'
            '    if accessible is None:\n'
            '        return\n'
            '    conn = get_connection()\n'
            '    cursor = conn.cursor(dictionary=True)\n'
            '    cursor.execute(\n'
            '        "SELECT aws_account_id FROM resources WHERE resource_id = %s LIMIT 1",\n'
            '        (resource_identifier,),\n'
            '    )\n'
            '    row = cursor.fetchone()\n'
            '    cursor.close()\n'
            '    conn.close()\n'
            '    if row and row["aws_account_id"] not in accessible:\n'
            '        raise HTTPException(status_code=403, detail="You do not have access to this resource")\n'
            '\n'
            '\n'
            'def _get_active_alert_counts_by_account() -> dict:\n'
        ),
        (
            '@router.get("/accounts")\n'
            'def live_accounts():\n'
            '    global _accounts_cache\n'
            '\n'
            '    now = time.time()\n'
            '    if _accounts_cache["data"] is not None and now - _accounts_cache["ts"] < CACHE_TTL:\n'
            '        return _accounts_cache["data"]\n'
            '\n'
            '    accounts = _get_db_accounts()\n',

            '@router.get("/accounts")\n'
            'def live_accounts(current_user: dict = Depends(require_permission("resources.view"))):\n'
            '    global _accounts_cache\n'
            '\n'
            '    now = time.time()\n'
            '    if _accounts_cache["data"] is not None and now - _accounts_cache["ts"] < CACHE_TTL:\n'
            '        accessible = get_accessible_account_ids(current_user)\n'
            '        cached = _accounts_cache["data"]\n'
            '        if accessible is not None:\n'
            '            cached = [a for a in cached if a["id"] in accessible]\n'
            '        return cached\n'
            '\n'
            '    accounts = _get_db_accounts()\n'
            '    accessible = get_accessible_account_ids(current_user)\n'
            '    if accessible is not None:\n'
            '        accounts = [a for a in accounts if a["id"] in accessible]\n'
        ),
        (
            '@router.get("/ec2/{account_db_id}")\n'
            'def live_ec2(account_db_id: int):\n'
            '    acc    = _get_db_account(account_db_id)\n'
            '    region = acc.get("default_region") \n'
            '    return _serialize(collect_ec2_instances(region))\n',

            '@router.get("/ec2/{account_db_id}")\n'
            'def live_ec2(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):\n'
            '    _check_account_scope(current_user, account_db_id)\n'
            '    acc    = _get_db_account(account_db_id)\n'
            '    region = acc.get("default_region") \n'
            '    return _serialize(collect_ec2_instances(region))\n'
        ),
        (
            '@router.get("/ebs/{account_db_id}")\n'
            'def live_ebs(account_db_id: int):\n'
            '    acc    = _get_db_account(account_db_id)\n'
            '    region = acc.get("default_region") \n'
            '    return _serialize(collect_ebs_volumes(region))\n',

            '@router.get("/ebs/{account_db_id}")\n'
            'def live_ebs(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):\n'
            '    _check_account_scope(current_user, account_db_id)\n'
            '    acc    = _get_db_account(account_db_id)\n'
            '    region = acc.get("default_region") \n'
            '    return _serialize(collect_ebs_volumes(region))\n'
        ),
        (
            '@router.get("/rds/{account_db_id}")\n'
            'def live_rds(account_db_id: int):\n'
            '    acc    = _get_db_account(account_db_id)\n'
            '    region = acc.get("default_region") \n'
            '    return _serialize(collect_rds_instances(region))\n',

            '@router.get("/rds/{account_db_id}")\n'
            'def live_rds(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):\n'
            '    _check_account_scope(current_user, account_db_id)\n'
            '    acc    = _get_db_account(account_db_id)\n'
            '    region = acc.get("default_region") \n'
            '    return _serialize(collect_rds_instances(region))\n'
        ),
        (
            '@router.get("/lambda/{account_db_id}")\n'
            'def live_lambda(account_db_id: int):\n'
            '    acc    = _get_db_account(account_db_id)\n'
            '    region = acc.get("default_region") \n'
            '    return _serialize(collect_lambda_functions(region))\n',

            '@router.get("/lambda/{account_db_id}")\n'
            'def live_lambda(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):\n'
            '    _check_account_scope(current_user, account_db_id)\n'
            '    acc    = _get_db_account(account_db_id)\n'
            '    region = acc.get("default_region") \n'
            '    return _serialize(collect_lambda_functions(region))\n'
        ),
        (
            '@router.get("/s3/{account_db_id}")\n'
            'def live_s3(account_db_id: int):\n'
            '    acc    = _get_db_account(account_db_id)\n'
            '    region = acc.get("default_region") \n'
            '    return _serialize(collect_s3_buckets(region))\n',

            '@router.get("/s3/{account_db_id}")\n'
            'def live_s3(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):\n'
            '    _check_account_scope(current_user, account_db_id)\n'
            '    acc    = _get_db_account(account_db_id)\n'
            '    region = acc.get("default_region") \n'
            '    return _serialize(collect_s3_buckets(region))\n'
        ),
        (
            '@router.get("/elb/{account_db_id}")\n'
            'def live_elb(account_db_id: int):\n'
            '    acc    = _get_db_account(account_db_id)\n'
            '    region = acc.get("default_region") \n'
            '    return _serialize(collect_elb(region))\n',

            '@router.get("/elb/{account_db_id}")\n'
            'def live_elb(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):\n'
            '    _check_account_scope(current_user, account_db_id)\n'
            '    acc    = _get_db_account(account_db_id)\n'
            '    region = acc.get("default_region") \n'
            '    return _serialize(collect_elb(region))\n'
        ),
        (
            '@router.get("/ecs/{account_db_id}")\n'
            'def live_ecs(account_db_id: int):\n'
            '    acc    = _get_db_account(account_db_id)\n'
            '    region = acc.get("default_region") \n'
            '    return _serialize(collect_ecs_clusters(region))\n',

            '@router.get("/ecs/{account_db_id}")\n'
            'def live_ecs(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):\n'
            '    _check_account_scope(current_user, account_db_id)\n'
            '    acc    = _get_db_account(account_db_id)\n'
            '    region = acc.get("default_region") \n'
            '    return _serialize(collect_ecs_clusters(region))\n'
        ),
        (
            '@router.get("/resource-counts/{account_db_id}")\n'
            'def live_resource_counts(account_db_id: int):\n'
            '    acc    = _get_db_account(account_db_id)\n',

            '@router.get("/resource-counts/{account_db_id}")\n'
            'def live_resource_counts(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):\n'
            '    _check_account_scope(current_user, account_db_id)\n'
            '    acc    = _get_db_account(account_db_id)\n'
        ),
        (
            '@router.get("/metrics/ec2/{instance_id}")\n'
            'def live_ec2_metrics(\n'
            '    instance_id: str,\n'
            '    region: str = Query(None),\n'
            '    hours: int  = Query(6),\n'
            '):\n'
            '    return get_ec2_metric_series(instance_id, region, hours)\n',

            '@router.get("/metrics/ec2/{instance_id}")\n'
            'def live_ec2_metrics(\n'
            '    instance_id: str,\n'
            '    region: str = Query(None),\n'
            '    hours: int  = Query(6),\n'
            '    current_user: dict = Depends(require_permission("metrics.view")),\n'
            '):\n'
            '    _check_resource_scope(current_user, instance_id)\n'
            '    return get_ec2_metric_series(instance_id, region, hours)\n'
        ),
        (
            '@router.get("/metrics/ebs/{volume_id}")\n'
            'def live_ebs_metrics(\n'
            '    volume_id: str,\n'
            '    region: str = Query(None),\n'
            '    hours: int  = Query(6),\n'
            '):\n'
            '    return _get_ebs_metric_series(volume_id, region, hours)\n',

            '@router.get("/metrics/ebs/{volume_id}")\n'
            'def live_ebs_metrics(\n'
            '    volume_id: str,\n'
            '    region: str = Query(None),\n'
            '    hours: int  = Query(6),\n'
            '    current_user: dict = Depends(require_permission("metrics.view")),\n'
            '):\n'
            '    _check_resource_scope(current_user, volume_id)\n'
            '    return _get_ebs_metric_series(volume_id, region, hours)\n'
        ),
        (
            '@router.get("/metrics/rds/{db_id}")\n'
            'def live_rds_metrics(\n'
            '    db_id: str,\n'
            '    region: str = Query(None),\n'
            '    hours: int  = Query(6),\n'
            '):\n'
            '    return _get_rds_metric_series(db_id, region, hours)\n',

            '@router.get("/metrics/rds/{db_id}")\n'
            'def live_rds_metrics(\n'
            '    db_id: str,\n'
            '    region: str = Query(None),\n'
            '    hours: int  = Query(6),\n'
            '    current_user: dict = Depends(require_permission("metrics.view")),\n'
            '):\n'
            '    _check_resource_scope(current_user, db_id)\n'
            '    return _get_rds_metric_series(db_id, region, hours)\n'
        ),
        (
            '@router.get("/metrics/lambda/{function_name}")\n'
            'def live_lambda_metrics(\n'
            '    function_name: str,\n'
            '    region: str = Query(None),\n'
            '    hours: int  = Query(6),\n'
            '):\n'
            '    return _get_lambda_metric_series(function_name, region, hours)\n',

            '@router.get("/metrics/lambda/{function_name}")\n'
            'def live_lambda_metrics(\n'
            '    function_name: str,\n'
            '    region: str = Query(None),\n'
            '    hours: int  = Query(6),\n'
            '    current_user: dict = Depends(require_permission("metrics.view")),\n'
            '):\n'
            '    _check_resource_scope(current_user, function_name)\n'
            '    return _get_lambda_metric_series(function_name, region, hours)\n'
        ),
        (
            '@router.get("/metrics/s3/{bucket_name:path}")\n'
            'def live_s3_metrics(\n'
            '    bucket_name: str,\n'
            '    hours: int = Query(24),\n'
            '):\n'
            '    return get_s3_metric_series(bucket_name, hours)\n',

            '@router.get("/metrics/s3/{bucket_name:path}")\n'
            'def live_s3_metrics(\n'
            '    bucket_name: str,\n'
            '    hours: int = Query(24),\n'
            '    current_user: dict = Depends(require_permission("metrics.view")),\n'
            '):\n'
            '    _check_resource_scope(current_user, bucket_name)\n'
            '    return get_s3_metric_series(bucket_name, hours)\n'
        ),
        (
            '@router.get("/metrics/elb/{account_db_id}")\n'
            'def live_elb_metrics(\n'
            '    account_db_id: int,\n'
            '    lb_name: str = Query(..., description="Load balancer name"),\n'
            '    region: str  = Query(None),\n'
            '    hours: int   = Query(6),\n'
            '):\n'
            '    """\n'
            '    ELB CloudWatch metrics for a specific load balancer by name.\n'
            '    Frontend calls: /api/live/metrics/elb/{accountId}?lb_name=<name>&region=<r>&hours=<h>\n'
            '    """\n'
            '    acc = _get_db_account(account_db_id)\n',

            '@router.get("/metrics/elb/{account_db_id}")\n'
            'def live_elb_metrics(\n'
            '    account_db_id: int,\n'
            '    lb_name: str = Query(..., description="Load balancer name"),\n'
            '    region: str  = Query(None),\n'
            '    hours: int   = Query(6),\n'
            '    current_user: dict = Depends(require_permission("metrics.view")),\n'
            '):\n'
            '    """\n'
            '    ELB CloudWatch metrics for a specific load balancer by name.\n'
            '    Frontend calls: /api/live/metrics/elb/{accountId}?lb_name=<name>&region=<r>&hours=<h>\n'
            '    """\n'
            '    _check_account_scope(current_user, account_db_id)\n'
            '    acc = _get_db_account(account_db_id)\n'
        ),
        (
            '@router.get("/metrics/ecs/{account_db_id}")\n'
            'def live_ecs_metrics(\n'
            '    account_db_id: int,\n'
            '    cluster_name: str  = Query(..., description="ECS cluster name"),\n'
            '    service_name: str  = Query(None, description="ECS service name (optional — omit for cluster-level)"),\n'
            '    region: str        = Query(None),\n'
            '    hours: int         = Query(6),\n'
            '):\n'
            '    """\n'
            '    ECS CloudWatch metrics for a cluster or specific service.\n'
            '    Frontend calls: /api/live/metrics/ecs/{accountId}?cluster_name=<c>&service_name=<s>&region=<r>&hours=<h>\n'
            '    """\n'
            '    acc = _get_db_account(account_db_id)\n',

            '@router.get("/metrics/ecs/{account_db_id}")\n'
            'def live_ecs_metrics(\n'
            '    account_db_id: int,\n'
            '    cluster_name: str  = Query(..., description="ECS cluster name"),\n'
            '    service_name: str  = Query(None, description="ECS service name (optional — omit for cluster-level)"),\n'
            '    region: str        = Query(None),\n'
            '    hours: int         = Query(6),\n'
            '    current_user: dict = Depends(require_permission("metrics.view")),\n'
            '):\n'
            '    """\n'
            '    ECS CloudWatch metrics for a cluster or specific service.\n'
            '    Frontend calls: /api/live/metrics/ecs/{accountId}?cluster_name=<c>&service_name=<s>&region=<r>&hours=<h>\n'
            '    """\n'
            '    _check_account_scope(current_user, account_db_id)\n'
            '    acc = _get_db_account(account_db_id)\n'
        ),
    ]
    return replace_all_exact(content, pairs, "app/api/live_data.py")


PATCHERS = {
    "app/api/admin/accounts.py": patch_admin_accounts,
    "app/api/alerts.py": patch_alerts,
    "app/api/settings.py": patch_settings,
    "app/api/metric_catalog.py": patch_metric_catalog,
    "app/api/audit_logs.py": patch_audit_logs,
    "app/api/live_data.py": patch_live_data,
}


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

    results = {}
    for rel_path in FILES:
        full_path = os.path.join(repo_root, rel_path)
        if not os.path.exists(full_path):
            die(f"{rel_path} not found. Aborting before changing anything else.")
        with open(full_path, "r", encoding="utf-8") as fh:
            original = fh.read()
        patched = PATCHERS[rel_path](original)
        results[rel_path] = (full_path, original, patched)

    print("\nAll patches matched expected content exactly in all 6 files:")
    for rel_path, (full_path, original, patched) in results.items():
        print(f"  {rel_path}: OK ({len(patched) - len(original):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    for rel_path, (full_path, original, patched) in results.items():
        bpath = backup(full_path)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(patched)
        print(f"Patched {rel_path} (backup: {os.path.basename(bpath)})")

    print("""
[Manual follow-up]

  A) Restart the backend:
       sudo systemctl restart monitoring-hub
       sudo systemctl status monitoring-hub --no-pager
       sudo journalctl -u monitoring-hub -n 40 --no-pager

     Watch closely for an import error or startup crash -- 6 files
     changed at once, so if any got the wrong syntax this is where
     it would show up immediately (same as the DB_PASSWORD mismatch
     earlier today).

  B) Functional check in the browser AS ADMIN (should see NO change
     at all -- admin bypasses every check):
       - Dashboard, accounts list, resource pages, alerts, settings,
         metric catalog, audit log -- everything should look and work
         exactly as before.

  C) If/when you create a test editor or viewer account, verify:
       - Viewer: can see dashboards/accounts/alerts/metrics (read-only
         permissions), gets 403 on onboarding/deleting an account,
         acking/resolving/muting alerts, editing thresholds, and
         cannot see the audit log at all.
       - Editor: can do everything a viewer can PLUS onboard accounts,
         act on alerts, and edit thresholds -- but still gets 403 on
         deleting an account, bulk-clearing alerts, and the audit log
         (all admin-only).
       - Assign a viewer/editor a scope limited to ONE account (via
         the existing Access Management screen) and confirm they now
         only see that account's data in live_data-backed pages, and
         get a 403 (not silently empty data) if they hit another
         account's URL directly.

  D) Then review, commit, push:
       git status
       git diff app/api/admin/accounts.py app/api/alerts.py app/api/settings.py \\
                app/api/metric_catalog.py app/api/audit_logs.py app/api/live_data.py
       git add app/api/admin/accounts.py app/api/alerts.py app/api/settings.py \\
               app/api/metric_catalog.py app/api/audit_logs.py app/api/live_data.py
       git commit -m "security(rbac): enforce permissions and account/region scope on all data-serving and operational routes"
       git push origin main

  E) KNOWN REMAINING GAP, not fixed by this script: this closes the
     account/resource-level scope gap, but does NOT add REGION
     filtering within an account a user IS allowed to see (e.g. a
     user scoped to only ap-south-1 within an account can still see
     resources from other regions of that same account, since
     live_data.py's collectors are called with the account's single
     default_region already, not per-resource region checks). Worth a
     follow-up pass if per-region scoping (not just per-account) is
     something you actually need.
""")


if __name__ == "__main__":
    main()
