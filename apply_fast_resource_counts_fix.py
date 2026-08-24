#!/usr/bin/env python3
"""
apply_fast_resource_counts_fix.py

Fixes the slow Services-page load introduced by the previous patch
(apply_console_scope_and_dynamic_resources_fix.py).

Root cause: ServiceList.jsx was calling GET /api/live/accounts to get
this account's resource counts. That endpoint fans out to EVERY active
account in the DB (not just the one you're looking at) — including
accounts like AuroGov Hyd / AuroGov US East that have DB rows but no
real deployed YACE/role setup. Those accounts' boto3 Describe calls can
be slow to fail or time out, and since the frontend has to wait for
ALL accounts to resolve before it gets back the one it actually needs,
every Services-page load paid that cost.

Fix: a new, lightweight GET /api/live/resource-counts/{account_id}
endpoint that only touches the ONE account being viewed, reusing the
same already-cached collector functions (60s in-process cache in
app/aws/collector_direct.py) — no new AWS calls beyond what already
existed, just scoped to one account instead of all of them.

Usage:
    python apply_fast_resource_counts_fix.py [repo_root]

Idempotent, backs up touched files to
"<file>.bak.pre-fast-resource-counts-fix", reverts automatically if any
patched Python file fails py_compile.
"""
import ast
import py_compile
import shutil
import sys
from pathlib import Path

BAK_SUFFIX = ".bak.pre-fast-resource-counts-fix"


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
    # 1. app/api/live_data.py — new lightweight per-account endpoint
    # ------------------------------------------------------------------
    p = repo_root / "app" / "api" / "live_data.py"
    marker = "/resource-counts/"
    replacements = [
        (
            '@router.get("/ec2/{account_db_id}")\n'
            "def live_ec2(account_db_id: int):\n",

            '@router.get("/resource-counts/{account_db_id}")\n'
            "def live_resource_counts(account_db_id: int):\n"
            '    """\n'
            "    Lightweight per-account resource counts for the Services page\n"
            "    (ServiceList.jsx tile visibility) — deliberately scoped to ONE\n"
            "    account instead of GET /api/live/accounts, which fans out to\n"
            "    every active account (including ones with no real deployment,\n"
            "    e.g. accounts without a working role/YACE setup) and makes the\n"
            "    caller wait for the slowest one. Reuses the same 60s in-process\n"
            "    cache in collector_direct.py, so this costs nothing extra beyond\n"
            "    what get_account_summary() already does for the Overview page.\n"
            '    """\n'
            "    acc     = _get_db_account(account_db_id)\n"
            '    region  = acc.get("default_region")\n'
            "    summary = get_account_summary(region)\n"
            "    return {\n"
            '        "ec2_total":    summary.get("ec2_total",    0),\n'
            '        "ebs_total":    summary.get("ebs_total",    0),\n'
            '        "rds_total":    summary.get("rds_total",    0),\n'
            '        "lambda_total": summary.get("lambda_total", 0),\n'
            '        "s3_total":     summary.get("s3_total",     0),\n'
            '        "elb_total":    summary.get("elb_total",    0),\n'
            '        "ecs_total":    summary.get("ecs_total",    0),\n'
            "    }\n"
            "\n"
            "\n"
            '@router.get("/ec2/{account_db_id}")\n'
            "def live_ec2(account_db_id: int):\n",
            "live_data.py: add /resource-counts/{account_id}",
        ),
    ]
    if apply_replacements(p, replacements, already_applied_marker=marker):
        any_change = True
    touched_py_files.append(p)

    # ------------------------------------------------------------------
    # 2. frontend/src/api/api.js — client helper for the new endpoint
    # ------------------------------------------------------------------
    p = repo_root / "frontend" / "src" / "api" / "api.js"
    replacements = [
        (
            'export const getLiveAccounts  = ()   => apiFetch("/api/live/accounts");\n',
            'export const getLiveAccounts  = ()   => apiFetch("/api/live/accounts");\n'
            'export const getResourceCounts = (id) => apiFetch(`/api/live/resource-counts/${id}`);\n',
            "api.js: add getResourceCounts",
        ),
    ]
    if apply_replacements(p, replacements, already_applied_marker="getResourceCounts"):
        any_change = True

    # ------------------------------------------------------------------
    # 3. frontend/src/pages/ServiceList.jsx — use the fast, scoped
    #    endpoint instead of the all-accounts one
    # ------------------------------------------------------------------
    p = repo_root / "frontend" / "src" / "pages" / "ServiceList.jsx"
    replacements = [
        (
            'import { getAlerts, getAccountMetrics, getLiveAccounts } from "../api/api";',
            'import { getAlerts, getAccountMetrics, getResourceCounts } from "../api/api";',
            "ServiceList.jsx: import getResourceCounts instead of getLiveAccounts",
        ),
        (
            '  useEffect(() => {\n'
            '    let cancelled = false;\n'
            '    setCountsLoading(true);\n'
            '    getLiveAccounts()\n'
            '      .then(list => {\n'
            '        if (cancelled) return;\n'
            '        const mine = (Array.isArray(list) ? list : []).find(a => String(a.id) === String(id));\n'
            '        setLiveCounts(mine || {});\n'
            '      })\n'
            '      .catch(() => { if (!cancelled) setLiveCounts({}); })\n'
            '      .finally(() => { if (!cancelled) setCountsLoading(false); });\n'
            '    return () => { cancelled = true; };\n'
            '  }, [id]);\n',

            '  useEffect(() => {\n'
            '    let cancelled = false;\n'
            '    setCountsLoading(true);\n'
            '    // Scoped to THIS account only — avoids waiting on every other\n'
            '    // active account (including undeployed ones) the way\n'
            '    // /api/live/accounts does.\n'
            '    getResourceCounts(id)\n'
            '      .then(counts => { if (!cancelled) setLiveCounts(counts || {}); })\n'
            '      .catch(() => { if (!cancelled) setLiveCounts({}); })\n'
            '      .finally(() => { if (!cancelled) setCountsLoading(false); });\n'
            '    return () => { cancelled = true; };\n'
            '  }, [id]);\n',
            "ServiceList.jsx: fetch scoped resource counts",
        ),
    ]
    if apply_replacements(p, replacements, already_applied_marker="getResourceCounts(id)"):
        any_change = True

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
        print("Done. Restart the backend and rebuild/reload the frontend to pick this up.")
    else:
        print("Nothing to do — all patches were already applied.")


if __name__ == "__main__":
    main()
