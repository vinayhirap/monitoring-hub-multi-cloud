#!/usr/bin/env python3
"""
apply_ebs_fix.py

Fixes two real bugs in app/aws/collector_direct.py:

1. _ebs_raw() and _get_ebs_metric_series() were querying VictoriaMetrics
   for the "_sum" suffix on read_ops / write_ops / read_bytes / write_bytes,
   but the Metric Catalog's configured statistic for these is "Average",
   so YACE only ever populates the "_average" series. The "_sum" series
   never had any samples -> UI showed "No data in last 6H" even though
   YACE/VM were working correctly.

2. burst_balance was never queried by the backend at all, despite the
   frontend already having a "Burst Balance %" card wired to
   metrics.burst_balance. This adds it to both functions.

Usage:
    python apply_ebs_fix.py app/aws/collector_direct.py

Safe by design: every change is applied via regex against text that is
verified present (with an expected occurrence count) before any write
happens. If anything doesn't match exactly, the script aborts and prints
exactly what it expected vs. what it found -- it will not partially patch
the file. A .bak copy of the original is written before any edit.
"""
import re
import sys
import shutil
import subprocess


def fail(msg):
    print(f"ABORTED - no changes written.\n{msg}")
    sys.exit(1)


def main():
    if len(sys.argv) != 2:
        fail("Usage: python apply_ebs_fix.py <path-to-collector_direct.py>")

    path = sys.argv[1]
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    original = src

    # ------------------------------------------------------------------
    # Fix 1: _sum -> _average for the 4 mis-suffixed metric names.
    # Each must appear exactly twice: once in _ebs_raw's vm_query_all call,
    # once in _get_ebs_metric_series's s(...) call.
    # ------------------------------------------------------------------
    suffix_fixes = [
        ('"aws_ebs_volume_read_ops_sum"',   '"aws_ebs_volume_read_ops_average"'),
        ('"aws_ebs_volume_write_ops_sum"',  '"aws_ebs_volume_write_ops_average"'),
        ('"aws_ebs_volume_read_bytes_sum"', '"aws_ebs_volume_read_bytes_average"'),
        ('"aws_ebs_volume_write_bytes_sum"','"aws_ebs_volume_write_bytes_average"'),
    ]
    for old, new in suffix_fixes:
        count = src.count(old)
        if count != 2:
            fail(f"Expected exactly 2 occurrences of {old}, found {count}.\n"
                 f"File may already be patched, or source has diverged from what was reviewed.")
        src = src.replace(old, new)

    # ------------------------------------------------------------------
    # Fix 2a: _ebs_raw() - add burst_map query right after queue_map query.
    # ------------------------------------------------------------------
    pattern_queue_map = re.compile(
        r'^([ \t]*)(\w+)(\s*=\s*)vm_query_all\("aws_ebs_volume_queue_length_average",\s*"dimension_VolumeId"\)[ \t]*$',
        re.MULTILINE,
    )
    matches = list(pattern_queue_map.finditer(src))
    if len(matches) != 1:
        fail(f"Expected exactly 1 queue_map vm_query_all(...) line in _ebs_raw, found {len(matches)}.")
    m = matches[0]
    indent = m.group(1)
    new_line = f'{indent}burst_map     = vm_query_all("aws_ebs_burst_balance_average", "dimension_VolumeId")'
    src = src[:m.end()] + "\n" + new_line + src[m.end():]

    # ------------------------------------------------------------------
    # Fix 2b: _ebs_raw() - add "burst_balance" field to the out.append(...) dict,
    # right after the "queue_length" field.
    # ------------------------------------------------------------------
    pattern_queue_field = re.compile(
        r'^([ \t]*)"queue_length":\s*round\((\w+)\.get\((\w+),\s*0\.0\),\s*4\),[ \t]*$',
        re.MULTILINE,
    )
    matches = list(pattern_queue_field.finditer(src))
    if len(matches) != 1:
        fail(f'Expected exactly 1 "queue_length" dict field in _ebs_raw, found {len(matches)}.')
    m = matches[0]
    indent, vid_var = m.group(1), m.group(3)
    new_field = f'{indent}"burst_balance":     round(burst_map.get({vid_var}, 0.0), 2),'
    src = src[:m.end()] + "\n" + new_field + src[m.end():]

    # ------------------------------------------------------------------
    # Fix 2c: _get_ebs_metric_series() - add burst_balance to the returned dict,
    # right after the "queue_length" entry.
    # ------------------------------------------------------------------
    pattern_series_field = re.compile(
        r'^([ \t]*)"queue_length":\s*s\("aws_ebs_volume_queue_length_average"\),[ \t]*$',
        re.MULTILINE,
    )
    matches = list(pattern_series_field.finditer(src))
    if len(matches) != 1:
        fail(f'Expected exactly 1 "queue_length": s(...) line in _get_ebs_metric_series, found {len(matches)}.')
    m = matches[0]
    indent = m.group(1)
    new_series_field = f'{indent}"burst_balance": s("aws_ebs_burst_balance_average"),'
    src = src[:m.end()] + "\n" + new_series_field + src[m.end():]

    # ------------------------------------------------------------------
    # Fix 2d: _get_ebs_metric_series() - add "burst_balance": [] to the
    # exception-fallback return dict.
    # ------------------------------------------------------------------
    old_fallback = '"queue_length": []}'
    count = src.count(old_fallback)
    if count != 1:
        fail(f'Expected exactly 1 occurrence of the fallback tail {old_fallback!r}, found {count}.')
    src = src.replace(old_fallback, '"queue_length": [], "burst_balance": []}')

    if src == original:
        fail("No changes were actually made (unexpected) - aborting without writing.")

    backup_path = path + ".bak"
    shutil.copy2(path, backup_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    print(f"Patched: {path}")
    print(f"Backup saved: {backup_path}")

    # Compile check
    result = subprocess.run([sys.executable, "-m", "py_compile", path])
    if result.returncode != 0:
        print("\npy_compile FAILED - restoring backup.")
        shutil.copy2(backup_path, path)
        sys.exit(1)
    else:
        print("py_compile OK.")


if __name__ == "__main__":
    main()
