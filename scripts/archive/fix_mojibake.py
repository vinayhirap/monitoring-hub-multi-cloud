#!/usr/bin/env python
"""
fix_mojibake.py

Finds and fixes UTF-8-interpreted-as-Latin-1 mojibake in frontend source
files (the "âœ" Apply recommended" / "Â· 7 services shown" bug).

This happens when a UTF-8 file (containing real ✓, ·, ", ', —, etc.) gets
re-encoded somewhere in the pipeline as if it were Latin-1/Windows-1252,
turning each multi-byte UTF-8 character into 2-3 garbled characters.

USAGE:
    Dry run (shows what would change, makes no edits):
        python fix_mojibake.py --dir frontend/src

    Apply fixes (writes changes, makes a .bak backup of every touched file):
        python fix_mojibake.py --dir frontend/src --apply

Safe to run repeatedly. Only touches .js, .jsx, .ts, .tsx files.
"""
import argparse
import os
import sys

# Common mojibake sequences -> correct character.
# These are UTF-8 bytes for common "smart" punctuation, re-decoded as Latin-1.
MOJIBAKE_MAP = {
    "â€™": "\u2019",  # right single quote  '
    "â€˜": "\u2018",  # left single quote   '
    "â€œ": "\u201c",  # left double quote   "
    "â€\x9d": "\u201d",  # right double quote  "
    "â€\x9c": "\u201c",
    "â€“": "\u2013",  # en dash  –
    "â€”": "\u2014",  # em dash  —
    "â€¦": "\u2026",  # ellipsis …
    "Â·": "\u00b7",   # middle dot ·
    "Â ": "\u00a0",   # non-breaking space
    "â\x9c\x93": "\u2713",  # check mark ✓
    'âœ"': "\u2713",   # check mark ✓ (as seen in the screenshot, variant encoding)
    "âœ”": "\u2714",   # heavy check mark ✔
    "Â®": "\u00ae",   # registered trademark ®
    "Â©": "\u00a9",   # copyright ©
}

EXTENSIONS = (".js", ".jsx", ".ts", ".tsx")


def scan_file(path):
    """Return list of (mojibake, correct, count) found in file, or None if unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return None, None

    hits = []
    for bad, good in MOJIBAKE_MAP.items():
        count = content.count(bad)
        if count:
            hits.append((bad, good, count))
    return content, hits


def fix_file(path, content, hits, apply_changes):
    new_content = content
    for bad, good, _ in hits:
        new_content = new_content.replace(bad, good)

    if not apply_changes:
        return

    backup_path = path + ".bak"
    with open(backup_path, "w", encoding="utf-8") as f:
        f.write(content)

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, help="Directory to scan, e.g. frontend/src")
    parser.add_argument("--apply", action="store_true", help="Actually write fixes (default is dry-run)")
    args = parser.parse_args()

    root = os.path.abspath(args.dir)
    if not os.path.isdir(root):
        print(f"ERROR: directory not found: {root}")
        sys.exit(1)

    print(f"Scanning {root} for mojibake in {EXTENSIONS} files...")
    print(f"Mode: {'APPLY (will write changes + .bak backups)' if args.apply else 'DRY RUN (no changes will be made)'}")
    print()

    total_files_affected = 0
    total_replacements = 0

    for dirpath, dirnames, filenames in os.walk(root):
        # skip node_modules etc.
        dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git", "dist", "build")]
        for fname in filenames:
            if not fname.endswith(EXTENSIONS):
                continue
            fpath = os.path.join(dirpath, fname)
            content, hits = scan_file(fpath)
            if content is None or not hits:
                continue

            total_files_affected += 1
            rel = os.path.relpath(fpath, root)
            print(f"{rel}:")
            for bad, good, count in hits:
                print(f"    {count}x  {bad!r}  ->  {good!r}")
                total_replacements += count

            fix_file(fpath, content, hits, args.apply)

    print()
    if total_files_affected == 0:
        print("No mojibake found. If the bug is still visible in the UI, the source")
        print("string may use a different corrupted sequence than this script knows about.")
        print("Paste the exact raw bytes/text and I'll add it to MOJIBAKE_MAP.")
    else:
        print(f"Found {total_replacements} occurrence(s) across {total_files_affected} file(s).")
        if not args.apply:
            print()
            print("This was a DRY RUN. To apply the fixes, run:")
            print(f"    python fix_mojibake.py --dir {args.dir} --apply")
        else:
            print("Fixes applied. Original files backed up alongside as *.bak")
            print("Restart your Vite dev server (it should hot-reload, but a full restart is safest).")


if __name__ == "__main__":
    main()