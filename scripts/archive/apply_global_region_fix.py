#!/usr/bin/env python3
"""
apply_global_region_fix.py

Bug: generate_yace_config() in app/api/metric_catalog.py builds every YACE
job using account["default_region"] uniformly. For globally-scoped AWS
services (CloudFront, Route53), the Resource Groups Tagging API index only
exists in us-east-1 regardless of the account's default region - so YACE
queries the wrong region's tagging index, finds nothing, and logs "No
tagged resources made it through filtering" even when the resources ARE
tagged. Confirmed this session: CloudFront distributions have map-migrated
tags, but the generated job used ap-south-1, so YACE saw zero resources.

Fix: any job whose namespace is in GLOBAL_NAMESPACES gets region us-east-1
hardcoded, regardless of the account's default_region. Every other
namespace is unaffected.

Usage:
    python apply_global_region_fix.py app/api/metric_catalog.py
"""
import sys
import shutil
import subprocess


def fail(msg):
    print(f"ABORTED - no changes written.\n{msg}")
    sys.exit(1)


def main():
    if len(sys.argv) != 2:
        fail("Usage: python apply_global_region_fix.py <path-to-metric_catalog.py>")

    path = sys.argv[1]
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    old_block = '''    jobs_by_key = {}
    for r in rows:
        interval = r["default_interval"] or 300
        key = (r["namespace"], interval)
        job = jobs_by_key.setdefault(key, {
            "type": r["namespace"],
            "regions": [account["default_region"] or "us-east-1"],
            "period": interval,
            "length": interval,
            "metrics": [],
        })'''

    count = src.count(old_block)
    if count != 1:
        fail(f"Expected exactly 1 occurrence of the jobs_by_key block, found {count}.\n"
             f"Source may have changed since this patch was written - paste the current "
             f"generate_yace_config() body and I'll regenerate this patch against it.")

    new_block = '''    # Globally-scoped services: their Resource Groups Tagging API index only
    # exists in us-east-1, regardless of the account's default_region. Using
    # the account's regional default here causes YACE to query the wrong
    # region's tagging index and silently discover zero resources even when
    # they ARE tagged. Confirmed with AWS/CloudFront this session.
    GLOBAL_NAMESPACES = {"AWS/CloudFront", "AWS/Route53"}

    jobs_by_key = {}
    for r in rows:
        interval = r["default_interval"] or 300
        key = (r["namespace"], interval)
        job_region = "us-east-1" if r["namespace"] in GLOBAL_NAMESPACES \\
            else (account["default_region"] or "us-east-1")
        job = jobs_by_key.setdefault(key, {
            "type": r["namespace"],
            "regions": [job_region],
            "period": interval,
            "length": interval,
            "metrics": [],
        })'''

    src = src.replace(old_block, new_block)

    backup_path = path + ".bak"
    shutil.copy2(path, backup_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    print(f"Patched: {path}")
    print(f"Backup saved: {backup_path}")

    result = subprocess.run([sys.executable, "-m", "py_compile", path])
    if result.returncode != 0:
        print("\npy_compile FAILED - restoring backup.")
        shutil.copy2(backup_path, path)
        sys.exit(1)
    else:
        print("py_compile OK.")


if __name__ == "__main__":
    main()
