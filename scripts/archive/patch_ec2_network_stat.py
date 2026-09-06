#!/usr/bin/env python3
"""
patch_ec2_network_stat.py
Changes EC2 NetworkIn/NetworkOut catalog statistic from "Sum" to "Average"
so YACE publishes aws_ec2_network_in_average / aws_ec2_network_out_average,
matching what collector_direct.py's get_ec2_metric_series() already queries.

Without this, YACE publishes aws_ec2_network_in_sum / _out_sum instead,
and the EC2 detail-page chart silently shows "No data" forever even
though the metric exists in VictoriaMetrics under a different name.

Usage:
    python patch_ec2_network_stat.py --dry-run
    python patch_ec2_network_stat.py
"""
import argparse
import subprocess
import sys
from pathlib import Path

TARGET_FILE = "app/aws/metric_catalog_data.py"

OLD_IN  = '("NetworkIn",          "Bytes",   "Sum",     True,  "Inbound network traffic"),'
NEW_IN  = '("NetworkIn",          "Bytes",   "Average", True,  "Inbound network traffic"),'
OLD_OUT = '("NetworkOut",         "Bytes",   "Sum",     True,  "Outbound network traffic"),'
NEW_OUT = '("NetworkOut",         "Bytes",   "Average", True,  "Outbound network traffic"),'


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

    if OLD_IN not in text or OLD_OUT not in text:
        print(f"[ABORT] Expected lines not found verbatim in {TARGET_FILE}.")
        print("File may have drifted from what this patch was written against.")
        print("Not touching it. Check the file manually.")
        sys.exit(1)

    if text.count(OLD_IN) != 1 or text.count(OLD_OUT) != 1:
        print(f"[ABORT] Expected exactly 1 occurrence each, found "
              f"{text.count(OLD_IN)} (NetworkIn) / {text.count(OLD_OUT)} (NetworkOut).")
        sys.exit(1)

    new_text = text.replace(OLD_IN, NEW_IN).replace(OLD_OUT, NEW_OUT)

    if args.dry_run:
        print(f"[DRY-RUN] Would update {TARGET_FILE}: NetworkIn/NetworkOut statistic Sum -> Average")
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
