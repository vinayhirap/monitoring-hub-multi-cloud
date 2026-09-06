#!/usr/bin/env python3
"""
patch_seed_script_dotenv.py
Adds load_dotenv() to scripts/seed_metric_catalog.py, matching the exact
pattern app/main.py already uses. Without this, the seed script never
reads .env and silently falls back to hardcoded DB_CONFIG defaults
(monitor / port 3306 - the production values), causing
"Access denied for user 'monitor'@'localhost'" against local Docker MySQL.

Usage:
    python patch_seed_script_dotenv.py --dry-run
    python patch_seed_script_dotenv.py
"""
import argparse
import subprocess
import sys
from pathlib import Path

TARGET_FILE = "scripts/seed_metric_catalog.py"

# Matches the first import line so we can insert load_dotenv() right after it,
# same position pattern as app/main.py (import, then load_dotenv() on next line).
ANCHOR = "import os"
INSERT_AFTER = "import os\nfrom dotenv import load_dotenv\nload_dotenv()"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args()

    fpath = Path(args.repo_root).resolve() / TARGET_FILE
    if not fpath.exists():
        print(f"[ABORT] {TARGET_FILE} not found under {args.repo_root}")
        sys.exit(1)

    text = fpath.read_text(encoding="utf-8")

    if "load_dotenv()" in text:
        print("[ABORT] load_dotenv() already present in this file. Not touching it.")
        sys.exit(1)

    if text.count(ANCHOR) != 1:
        print(f"[ABORT] Expected exactly 1 occurrence of '{ANCHOR}', found {text.count(ANCHOR)}.")
        print("File may not match the expected structure. Check manually.")
        sys.exit(1)

    new_text = text.replace(ANCHOR, INSERT_AFTER, 1)

    if args.dry_run:
        print(f"[DRY-RUN] Would insert load_dotenv() into {TARGET_FILE} after 'import os'")
        print("--- preview ---")
        idx = new_text.find(INSERT_AFTER)
        print(new_text[max(0, idx - 20):idx + len(INSERT_AFTER) + 20])
        return

    backup = fpath.with_suffix(fpath.suffix + ".bak")
    backup.write_text(text, encoding="utf-8")
    fpath.write_text(new_text, encoding="utf-8")
    print(f"[OK] {TARGET_FILE} updated, backup at {backup.name}")

    result = subprocess.run([sys.executable, "-m", "py_compile", str(fpath)],
                             capture_output=True, text=True)
    if result.returncode != 0:
        print("[REVERT] py_compile failed, restoring backup")
        fpath.write_text(text, encoding="utf-8")
        print(result.stderr)
        sys.exit(1)

    print("Patch applied and verified (py_compile OK).")


if __name__ == "__main__":
    main()
