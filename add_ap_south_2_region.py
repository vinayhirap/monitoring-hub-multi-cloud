#!/usr/bin/env python3
"""
add_ap_south_2_region.py
-------------------------
Adds the AWS ap-south-2 (Hyderabad) region to the account-onboarding
region selector in monitoring-hub-multi-cloud.

Why only this file?
  The backend (app/api/admin/accounts.py, app/providers/aws/provider.py,
  app/aws/*.py) stores/consumes `default_region` and `region` as plain
  free-text strings with no allow-list/enum validation, and the console-url
  / CloudWatch / boto3 calls all take the region as a runtime parameter.
  There is no hardcoded region enum anywhere server-side, so no backend
  change is required for a new region to work end-to-end.

  The ONLY place AWS regions are hardcoded is the dropdown list the AWS
  account-onboarding wizard uses:
      frontend/src/pages/AccountOnboarding.jsx  ->  AWS_REGIONS

This script inserts:
      { id: "ap-south-2", label: "ap-south-2 (Hyderabad)" },
  right after the existing ap-south-1 (Mumbai) entry, so it shows up in
  the AWS region dropdown when adding/editing an AWS account.

Usage (run from the project root, i.e. the folder containing "frontend/"):
    python add_ap_south_2_region.py

    or, from anywhere:
    python add_ap_south_2_region.py "D:\\Project\\monitoring-tool\\monitoring-hub-V5-multi-cloud"

The script is idempotent — running it twice is safe; it will detect the
region is already present and do nothing the second time.
"""

import sys
from pathlib import Path

TARGET_RELATIVE_PATH = Path("frontend") / "src" / "pages" / "AccountOnboarding.jsx"

ANCHOR = '  { id: "ap-south-1",     label: "ap-south-1 (Mumbai)" },\n'
NEW_LINE = '  { id: "ap-south-2",     label: "ap-south-2 (Hyderabad)" },\n'


def find_project_root(cli_arg: str | None) -> Path:
    if cli_arg:
        root = Path(cli_arg).resolve()
        if not (root / TARGET_RELATIVE_PATH).exists():
            print(f"[ERROR] Could not find {TARGET_RELATIVE_PATH} under: {root}")
            sys.exit(1)
        return root

    # Try current working directory first, then the directory this script
    # lives in (in case it was dropped straight into the project root).
    for candidate in (Path.cwd(), Path(__file__).resolve().parent):
        if (candidate / TARGET_RELATIVE_PATH).exists():
            return candidate

    print(
        "[ERROR] Could not locate 'frontend/src/pages/AccountOnboarding.jsx' from the "
        "current directory.\nRun this script from the project root, or pass the "
        "project root path as an argument:\n"
        '    python add_ap_south_2_region.py "D:\\Project\\monitoring-tool\\monitoring-hub-V5-multi-cloud"'
    )
    sys.exit(1)


def main() -> None:
    cli_arg = sys.argv[1] if len(sys.argv) > 1 else None
    root = find_project_root(cli_arg)
    target = root / TARGET_RELATIVE_PATH

    # utf-8-sig transparently reads/strips a BOM if present, and re-adds it
    # on write via the same codec — the file in this repo starts with a BOM.
    original = target.read_text(encoding="utf-8-sig")

    if 'id: "ap-south-2"' in original:
        print(f"[SKIP] ap-south-2 is already present in {target}")
        return

    if ANCHOR not in original:
        print(
            f"[ERROR] Could not find the expected ap-south-1 line in {target}.\n"
            "The file may have changed since this script was written — "
            "please add the entry manually:\n"
            f"    {NEW_LINE.strip()}"
        )
        sys.exit(1)

    updated = original.replace(ANCHOR, ANCHOR + NEW_LINE, 1)
    target.write_text(updated, encoding="utf-8-sig")

    print(f"[OK] Added ap-south-2 (Hyderabad) to AWS_REGIONS in {target}")
    print("     New entry inserted right after ap-south-1 (Mumbai).")
    print("\nNext steps:")
    print("  1. git diff   # review the change")
    print("  2. git add frontend/src/pages/AccountOnboarding.jsx")
    print('  3. git commit -m "Add ap-south-2 (Hyderabad) AWS region"')
    print("  4. git push")
    print("  5. On the server: git pull, then rebuild/restart the frontend.")


if __name__ == "__main__":
    main()
