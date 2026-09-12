#!/usr/bin/env python3
"""
apply_alert_window_consistency_fix.py — fixes the remaining reason the
Overview dashboard can show "Healthy · 0 Critical · 0 Warning" in the
summary tiles/account card while the alerts banner directly above it
says "7 CRITICAL · 2 WARNING — Active alerts require attention".

ROOT CAUSE (found by diffing the banner's query against the rollup's)
----------------------------------------------------------------------
apply_account_health_rollup_fix.py (already applied — see git log
409962c) correctly made the per-account health rollup resolve alerts
to accounts via `resources`, for every resource type instead of just
EC2. That fix is real and still in place. But its SQL
(_get_active_alert_counts_by_account in app/api/live_data.py) carries
one extra clause the banner's query does NOT have:

    AND a.triggered_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)

The alerts banner (app/api/alerts.py: open_alerts / GET /alerts/open,
the thing Overview.jsx sums into criticalAlerts/warningAlerts) defines
"active" as exactly `status = 'active' AND resolved_at IS NULL` — full
stop, no age cutoff. So an alert that has been open for MORE than 24
hours (arguably the one most deserving of a red tile, since it's had a
full day to get resolved and hasn't) is correctly included in the
banner's count, but silently DROPPED from the account-level rollup —
because it's the rollup, not the banner, carrying a stricter window
the banner never had. The account it belongs to is then computed as
"healthy" while the banner truthfully reports it as critical: two
queries that are supposed to answer the same question ("does this
account have an active critical/warning alert?") quietly drifted
apart again, the same failure mode the previous fix was written to
eliminate, just via a different clause this time.

A second, smaller drift: the rollup's JOIN to `resources` never gated
on the owning account being active (`aws_accounts.status = 'active'`),
which the banner's JOIN chain always has. An alert whose resource
still points at a soft-deleted/re-added account row could otherwise be
silently absorbed into — or vanish from — the wrong bucket if account
ids get reused. Adding the same `aws_accounts` gate here makes the two
queries structurally identical modulo the columns each one selects.

THE FIX
-------
app/api/live_data.py: _get_active_alert_counts_by_account()
  - Remove the `triggered_at > NOW() - 24h` clause entirely. Whether an
    alert counts toward account health is "is it active", full stop —
    exactly like the banner. (Staleness — "no fresh data in 20 min" —
    is already a separate, correct, DISPLAY-only concept in
    alerts.py/_STALE_AFTER_MINUTES; it marks an alert `stale: true`,
    it does not stop counting it. Health rollup should follow the same
    rule: age affects how an alert is *labelled*, never whether it
    exists.)
  - Add `JOIN aws_accounts acc ON acc.id = r.aws_account_id AND
    acc.status = 'active'`, matching alerts.py's open_alerts/get_alerts
    exactly, so both queries resolve "which account does this alert
    belong to" through the identical path.

This is the last divergent piece of the Overview health-consistency
work: banner, summary tiles, and per-account badges now all read
"active alert" off the exact same definition, with no independent
copy of the filter left anywhere to drift out of sync in the future.

Usage:
    python apply_alert_window_consistency_fix.py --dry-run
    python apply_alert_window_consistency_fix.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-alert-window-consistency-fix"


class PatchError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────
# Patches
# ─────────────────────────────────────────────────────────────────────────
PATCHES = [
    (
        "app/api/live_data.py",
        [
            (
                r'''def _get_active_alert_counts_by_account() -> dict:
    """
    THE authoritative source for account-level health: {aws_account_id:
    {"critical": N, "warning": N}}, counting DISTINCT alerting
    resources of each severity, resolved to an account via `resources`
    (which carries aws_account_id for every resource type this app
    discovers — EC2, EBS, RDS, Lambda, ELB, ECS), not just EC2.

    This replaces the previous _get_active_alert_resources(), which
    returned raw (critical_ids, warning_ids) sets that the caller then
    intersected against ONLY that account's EC2 instance_ids — meaning
    a critical alert on an EBS volume, S3 bucket, RDS instance, or
    Lambda function never counted toward that account's status at all.
    That's exactly how a dashboard can show "7 CRITICAL · 3 WARNING"
    in the alerts banner (built straight from the alerts table) while
    the account summary tiles above it say 0 critical, 0 healthy — the
    two were computed from different data. Routing both through this
    one function is what keeps them in agreement.
    """
    try:
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT r.aws_account_id, a.severity, COUNT(DISTINCT a.resource_id) AS cnt
            FROM alerts a
            JOIN resources r ON r.resource_id = a.resource_id
            WHERE a.status = 'active'
              AND a.resolved_at IS NULL
              AND a.triggered_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)
            GROUP BY r.aws_account_id, a.severity
        """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        out = {}
        for r in rows:
            bucket = out.setdefault(r["aws_account_id"], {"critical": 0, "warning": 0})
            sev = (r["severity"] or "").upper()
            if sev == "CRITICAL":
                bucket["critical"] += r["cnt"]
            elif sev == "WARNING":
                bucket["warning"] += r["cnt"]
        return out
    except Exception as e:
        logger.error(f"Active alert count fetch error: {e}")
        return {}
''',
                r'''def _get_active_alert_counts_by_account() -> dict:
    """
    THE authoritative source for account-level health: {aws_account_id:
    {"critical": N, "warning": N}}, counting DISTINCT alerting
    resources of each severity, resolved to an account via `resources`
    (which carries aws_account_id for every resource type this app
    discovers — EC2, EBS, RDS, Lambda, ELB, ECS), not just EC2.

    This replaces the previous _get_active_alert_resources(), which
    returned raw (critical_ids, warning_ids) sets that the caller then
    intersected against ONLY that account's EC2 instance_ids — meaning
    a critical alert on an EBS volume, S3 bucket, RDS instance, or
    Lambda function never counted toward that account's status at all.
    That's exactly how a dashboard can show "7 CRITICAL · 3 WARNING"
    in the alerts banner (built straight from the alerts table) while
    the account summary tiles above it say 0 critical, 0 healthy — the
    two were computed from different data. Routing both through this
    one function is what keeps them in agreement.

    "Active" is defined EXACTLY once here, identically to the banner's
    own query (app/api/alerts.py: open_alerts): status = 'active' AND
    resolved_at IS NULL, resolved to an ACTIVE account via the same
    `aws_accounts.status = 'active'` gate the banner uses. No extra
    age window is applied — an alert open for 10 minutes and one open
    for 10 days both count for as long as they remain unresolved.
    (A previous version of this query additionally required
    `triggered_at > NOW() - 24h`, a clause the banner never had; any
    alert older than a day was silently excluded from account health
    while still showing in the banner, reproducing the exact
    banner/tiles disagreement this function exists to prevent. Alert
    *age* is a display concern — see alerts.py's `stale` flag — never
    a reason to stop counting an alert that is still open.)
    """
    try:
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT r.aws_account_id, a.severity, COUNT(DISTINCT a.resource_id) AS cnt
            FROM alerts a
            JOIN resources r      ON r.resource_id = a.resource_id
            JOIN aws_accounts acc ON acc.id = r.aws_account_id
                                   AND acc.status = 'active'
            WHERE a.status = 'active'
              AND a.resolved_at IS NULL
            GROUP BY r.aws_account_id, a.severity
        """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        out = {}
        for r in rows:
            bucket = out.setdefault(r["aws_account_id"], {"critical": 0, "warning": 0})
            sev = (r["severity"] or "").upper()
            if sev == "CRITICAL":
                bucket["critical"] += r["cnt"]
            elif sev == "WARNING":
                bucket["warning"] += r["cnt"]
        return out
    except Exception as e:
        logger.error(f"Active alert count fetch error: {e}")
        return {}
''',
            ),
        ],
    ),
]


# ─────────────────────────────────────────────────────────────────────────
# Runner (same shape as apply_account_health_rollup_fix.py)
# ─────────────────────────────────────────────────────────────────────────
def preflight():
    print("=== Pre-flight: verifying anchors match exactly ===")
    problems = []

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        for old, _new in replacements:
            count = text.count(old)
            if count == 0:
                problems.append(f"{rel_path}: anchor not found (0 matches) — {old[:70]!r}")
            elif count > 1:
                problems.append(f"{rel_path}: anchor matched {count} times, expected exactly 1")
            else:
                print(f"  OK  {rel_path}: anchor matched exactly once")

    if problems:
        print("\n".join(problems))

        def _already(rel, new_text):
            p = REPO_ROOT / rel
            return p.exists() and new_text in p.read_text(encoding="utf-8")

        already_applied = all(_already(rel, new) for rel, repls in PATCHES for _old, new in repls)
        if already_applied:
            print("\nAll target text already present — patch appears to be already applied. Nothing to do.")
            sys.exit(0)
        raise PatchError("Pre-flight failed — aborting before touching any file. See problems above.")
    print("Pre-flight OK.\n")


def apply_all(dry_run: bool):
    changed_files = []
    report = []

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        text = full_path.read_text(encoding="utf-8")
        original_text = text
        for old, new in replacements:
            if new in text:
                continue  # already patched
            if old not in text:
                raise PatchError(f"{rel_path}: expected anchor vanished mid-patch — aborting")
            text = text.replace(old, new, 1)

        if text == original_text:
            continue

        if dry_run:
            report.append(f"[DRY RUN] would patch: {rel_path}")
        else:
            backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
            if not backup_path.exists():
                shutil.copy2(full_path, backup_path)
            full_path.write_text(text, encoding="utf-8")
            report.append(f"PATCHED: {rel_path}  (backup: {backup_path.name})")
            changed_files.append(full_path)

    for line in report:
        print(line)

    return changed_files


def validate_python_syntax(changed_files):
    print("\n=== Validating Python syntax (py_compile) ===")
    for f in changed_files:
        if f.suffix != ".py":
            continue
        try:
            py_compile.compile(str(f), doraise=True)
            print(f"  OK  {f.relative_to(REPO_ROOT)}")
        except py_compile.PyCompileError as e:
            raise PatchError(f"SYNTAX ERROR after patching {f}: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        preflight()
        changed = apply_all(args.dry_run)
        if not args.dry_run:
            validate_python_syntax(changed)
            print(f"\n=== Done. {len(changed)} file(s) touched. ===")
            print("\nNext steps:")
            print("  1. Backend: full uvicorn restart (not --reload) — the /api/live/accounts")
            print("     30s in-process cache also needs a fresh process to guarantee old")
            print("     data isn't served momentarily.")
            print("  2. Reload Overview: any account with an active alert older than 24h")
            print("     will now correctly turn Warning/Critical instead of staying Healthy,")
            print("     and will match the alerts banner's count exactly, regardless of how")
            print("     long that alert has been open.")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
