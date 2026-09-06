"""
cleanup_dead_endpoints.py
--------------------------
Removes confirmed dead API endpoints from backend files.
Run from project root: venv\Scripts\python cleanup_dead_endpoints.py

Dead endpoints being removed:
  1. GET /dashboard/overview        → entire file app/api/dashboard/overview.py
                                      (also removes it from main.py)
  2. GET /api/admin/accounts/queue  → function in app/api/admin/accounts.py
  3. GET /api/live/account/{id}     → function in app/api/live_data.py
  4. GET /api/audit-logs/stats      → function in app/api/audit_logs.py
  5. GET /api/settings/metrics      → function in app/api/settings.py
"""

import os
import re
import shutil
import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR   = os.path.join(PROJECT_ROOT, f"_backup_endpoints_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}")

FILES_TO_PATCH = [
    "app/main.py",
    "app/api/admin/accounts.py",
    "app/api/live_data.py",
    "app/api/audit_logs.py",
    "app/api/settings.py",
]

FILE_TO_DELETE = "app/api/dashboard/overview.py"


def backup_file(rel_path):
    src = os.path.join(PROJECT_ROOT, rel_path.replace("/", os.sep))
    if not os.path.exists(src):
        return
    dst = os.path.join(BACKUP_DIR, rel_path.replace("/", os.sep))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    print(f"  backed up: {rel_path}")


def read_file(rel_path):
    path = os.path.join(PROJECT_ROOT, rel_path.replace("/", os.sep))
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_file(rel_path, content):
    path = os.path.join(PROJECT_ROOT, rel_path.replace("/", os.sep))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def remove_function_block(content, decorator_pattern, func_label):
    """
    Removes a complete router function block starting from its decorator.
    Finds the decorator line, then removes everything until the next
    @router decorator or end of file.
    """
    lines = content.split("\n")
    start_idx = None

    for i, line in enumerate(lines):
        if re.search(decorator_pattern, line.strip()):
            start_idx = i
            break

    if start_idx is None:
        print(f"  ⚠  SKIP (not found): {func_label}")
        return content

    # Find end — next @router.* or @app.* at same indent level
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("@router.") or stripped.startswith("@app."):
            end_idx = i
            break

    removed = lines[start_idx:end_idx]
    print(f"  ✅ removed: {func_label} ({end_idx - start_idx} lines)")
    remaining = lines[:start_idx] + lines[end_idx:]

    # Clean up excess blank lines (max 2 consecutive)
    cleaned = []
    blank_count = 0
    for line in remaining:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= 2:
                cleaned.append(line)
        else:
            blank_count = 0
            cleaned.append(line)

    return "\n".join(cleaned)


# ── Step 1: Backup ────────────────────────────────────────────

def step_backup():
    print("\n── Step 1: Backing up files ──────────────────────")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    for f in FILES_TO_PATCH + [FILE_TO_DELETE]:
        backup_file(f)
    print(f"  Backup saved to: {BACKUP_DIR}")


# ── Step 2: Remove dashboard/overview.py from main.py ─────────

def step_patch_main():
    print("\n── Step 2: Removing dashboard_overview_router from main.py ──")
    content = read_file("app/main.py")
    changed = False

    for old, label in [
        ("from app.api.dashboard.overview import router as dashboard_overview_router\n",
         "remove dashboard_overview_router import"),
        ("app.include_router(dashboard_overview_router)\n",
         "remove dashboard_overview_router include"),
    ]:
        if old in content:
            content = content.replace(old, "", 1)
            print(f"  ✅ {label}")
            changed = True
        else:
            print(f"  ⚠  SKIP (not found): {label}")

    if changed:
        write_file("app/main.py", content)
        print("  main.py saved.")


# ── Step 3: Delete dashboard/overview.py ──────────────────────

def step_delete_overview():
    print("\n── Step 3: Deleting app/api/dashboard/overview.py ──")
    full = os.path.join(PROJECT_ROOT, FILE_TO_DELETE.replace("/", os.sep))
    if os.path.exists(full):
        confirm = input("  Delete app/api/dashboard/overview.py? Type YES: ").strip()
        if confirm == "YES":
            os.remove(full)
            print("  deleted: app/api/dashboard/overview.py")
            # Remove empty dashboard dir
            d = os.path.join(PROJECT_ROOT, "app", "api", "dashboard")
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
                print("  removed empty dir: app/api/dashboard")
        else:
            print("  skipped.")
    else:
        print("  already gone.")


# ── Step 4: Remove /queue from admin/accounts.py ──────────────

def step_patch_accounts():
    print("\n── Step 4: Removing /queue endpoint from admin/accounts.py ──")
    content = read_file("app/api/admin/accounts.py")
    content = remove_function_block(
        content,
        r'^@router\.get\("\/queue"\)',
        "GET /queue endpoint"
    )
    write_file("app/api/admin/accounts.py", content)
    print("  admin/accounts.py saved.")


# ── Step 5: Remove /account/{id} from live_data.py ────────────

def step_patch_live_data():
    print("\n── Step 5: Removing /account/{id} from live_data.py ──")
    content = read_file("app/api/live_data.py")
    content = remove_function_block(
        content,
        r'^@router\.get\("\/account\/\{account_db_id\}"\)',
        "GET /account/{account_db_id} endpoint"
    )
    write_file("app/api/live_data.py", content)
    print("  live_data.py saved.")


# ── Step 6: Remove /audit-logs/stats from audit_logs.py ───────

def step_patch_audit_logs():
    print("\n── Step 6: Removing /audit-logs/stats from audit_logs.py ──")
    content = read_file("app/api/audit_logs.py")
    content = remove_function_block(
        content,
        r'^@router\.get\("\/audit-logs\/stats"\)',
        "GET /audit-logs/stats endpoint"
    )
    write_file("app/api/audit_logs.py", content)
    print("  audit_logs.py saved.")


# ── Step 7: Remove /metrics from settings.py ──────────────────

def step_patch_settings():
    print("\n── Step 7: Removing /metrics endpoint from settings.py ──")
    content = read_file("app/api/settings.py")
    content = remove_function_block(
        content,
        r'^@router\.get\("\/metrics"\)',
        "GET /metrics endpoint"
    )
    write_file("app/api/settings.py", content)
    print("  settings.py saved.")


# ── Step 8: Verify final routes ───────────────────────────────

def step_verify():
    print("\n── Step 8: Verifying final route list ────────────")
    try:
        import sys
        sys.path.insert(0, PROJECT_ROOT)
        for mod in list(sys.modules.keys()):
            if mod.startswith("app"):
                del sys.modules[mod]
        from app.main import app
        print("  Registered routes:")
        for route in app.routes:
            if hasattr(route, "methods"):
                print(f"    {list(route.methods)} {route.path}")
    except Exception as e:
        print(f"  Could not verify: {e}")
        print("  Restart uvicorn manually to verify.")


# ── Main ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  CloudOps — Dead Endpoint Cleanup Script")
    print("=" * 60)

    step_backup()
    step_patch_main()
    step_delete_overview()
    step_patch_accounts()
    step_patch_live_data()
    step_patch_audit_logs()
    step_patch_settings()
    step_verify()

    print("\n" + "=" * 60)
    print("  Done. Backup at:", BACKUP_DIR)
    print("  Restart uvicorn:")
    print("  venv\\Scripts\\uvicorn app.main:app --reload --port 8000")
    print("=" * 60)