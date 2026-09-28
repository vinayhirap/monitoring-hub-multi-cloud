#!/usr/bin/env python3
"""
apply_cleanup_disk_mount_noise.py
========================================
Removes disk_used_percent__<slug> rows that got auto-registered by
ensure_disk_mount_metric_registered() for pseudo/ephemeral mount
points -- BEFORE app/collector/disk_mounts.py's all_cwagent_disk_dims()
was fixed (see chat) to filter these out at discovery time. Those
mounts were never a genuine "is this disk filling up" concern --
snap's squashfs loopback mounts in particular are READ-ONLY images
sized exactly to their content, so they report ~100% used
PERMANENTLY: any threshold row registered for one fires CRITICAL the
moment it's next collected, for something that was never actionable.

CONFIRMED LIVE ON THIS ACCOUNT: a `SELECT metric_name FROM
metric_catalog WHERE metric_name LIKE 'disk_used_percent__%'` returned
45 rows -- every one a snap loopback (/snap/*/<rev>) or a pseudo-fs
(/run, /run/lock, /run/user/<uid>, /run/snapd/ns, /dev, /dev/shm,
/boot/efi) from one instance whose CWAgent reports disk_used_percent
for literally every mount in /proc/mounts, not just real data volumes.

WHY A HEURISTIC, NOT A FRESH CLOUDWATCH LOOKUP
------------------------------------------------------------------
metric_catalog doesn't store the fstype dimension CWAgent originally
reported (ensure_disk_mount_metric_registered() only clones
metric_name and a human description) -- there's no live signal left to
re-check against. The only path back to "was this pseudo" is reversing
the metric_name's slug, and slugify_mount_path() is lossy (collapses
'/', ':', and non-alnum chars to '_'), so this can't perfectly
reconstruct the original path either.

The prefix/token match below is intentionally conservative -- it only
flags the exact classes confirmed live above (a LEADING token of
snap_/run/dev/proc/sys, or the exact slug boot_efi) -- and --dry-run
(the default) prints every matched metric_name for your own eyeball
check before anything is deleted. A real mount whose slug happens to
start with one of these tokens (e.g. a literal `/devops-data` mount ->
slug `devops_data`) WOULD currently false-positive on the `dev` token;
the dry-run output is exactly where you'd catch that and either
exclude it with --exclude or skip this script and delete that one row
by hand. Nothing here is silently destructive.

DELETES, IN FK-SAFE ORDER (metric_history/metrics are keyed by the
metric_name STRING, not metric_catalog.id -- confirmed against
db/backups/pre_alert_hardening_20260825_104729.sql's actual CREATE
TABLE statements, not assumed):
    metric_history -> metrics -> thresholds -> account_metric_selections -> metric_catalog

thresholds' real unique key, also now confirmed from that same backup
(disk_mounts.py's own docstring previously flagged this as
UNCONFIRMED): uniq_threshold (aws_account_id, resource_type, metric_id)
AND uniq_acc_metric (aws_account_id, metric_id) -- i.e. at most ONE
threshold row per (account, metric_id) regardless of resource_type.
Worth updating that docstring's CONFIDENCE note separately; not done
by this script, which only touches disk_used_percent__* noise rows.

ALERTS ARE HANDLED SEPARATELY, NOT DELETED (apply_fix_cleanup_script_orphaned_alerts.py)
------------------------------------------------------------------
Originally this script's delete chain stopped at metric_catalog and
never touched `alerts` at all -- confirmed live: after running this
script for real, 29 already-active `disk_used_percent__snap_*` alert
rows on one account were left behind, permanently frozen (their
metric_id linkage was gone, so nothing could ever re-evaluate or
resolve them). Same "can never self-heal" shape as the BurstBalance
gp3 issue, just reached via this script's own gap instead of a missing
collector-side filter.

Fix: find_noise_alert_rows() below scans `alerts.metric_name` directly
against the SAME noise heuristic, independently of whatever's
currently in metric_catalog -- deliberately independent, because by
the time you have zombies to clean, metric_catalog may *already* be
empty of the offending rows (e.g. a previous run of this same script
already deleted the catalog side, exactly what happened live). Matched
active alerts are RESOLVED (status='resolved', resolved_at=NOW()), not
deleted -- alerts are historical/audit records elsewhere in this app
(see the BurstBalance and NetworkIn cleanups, both UPDATEs not
DELETEs), so this script now follows that same convention instead of
introducing a second, inconsistent disposal method.

Usage:
    python3 apply_cleanup_disk_mount_noise.py                # dry-run (default) -- lists matches, changes nothing
    python3 apply_cleanup_disk_mount_noise.py --dry-run       # same, explicit
    python3 apply_cleanup_disk_mount_noise.py --apply         # deletes matched catalog rows AND resolves matched alerts, for real
    python3 apply_cleanup_disk_mount_noise.py --apply --exclude data,var_lib_mysql
                                                                # skip specific slugs even if they'd otherwise match

Idempotent: matched-row count (catalog AND alerts) drops to 0 on a
second run once cleaned; safe to re-run any time after new mounts get
registered (e.g. after raising this account's CWAgent config to also
stop reporting pseudo mounts is a separate, better long-term fix --
this script just cleans up what's already in the DB either way).
"""
import argparse
import sys

# app/db.py reads DB_PASSWORD straight from os.environ with no
# fallback -- normally populated by app/main.py's load_dotenv() at
# app startup, which this standalone script never goes through.
# Load .env the same way main.py does, before importing app.db.
from dotenv import load_dotenv
load_dotenv()

from app.db import get_connection

NOISE_LEADING_TOKENS = ("snap_", "var_snap", "run", "dev", "proc", "sys")
NOISE_EXACT_SLUGS = ("boot_efi",)

PREFIX = "disk_used_percent__"


def _looks_like_noise(slug: str) -> bool:
    if slug in NOISE_EXACT_SLUGS:
        return True
    return any(slug == tok or slug.startswith(tok) for tok in NOISE_LEADING_TOKENS)


def find_noise_rows(cursor, excluded_slugs):
    cursor.execute(
        "SELECT id, metric_name FROM metric_catalog WHERE metric_name LIKE %s",
        (f"{PREFIX}%",),
    )
    rows = cursor.fetchall()
    matched = []
    for row_id, metric_name in rows:
        slug = metric_name[len(PREFIX):]
        if slug in excluded_slugs:
            continue
        if _looks_like_noise(slug):
            matched.append((row_id, metric_name))
    return matched


def find_noise_alert_rows(cursor, excluded_slugs):
    """
    Independent of find_noise_rows() / metric_catalog on purpose -- see
    the module docstring's "ALERTS ARE HANDLED SEPARATELY" section.
    Only ever looks at currently-'active' alerts; resolved/historical
    alert rows are left untouched regardless of their metric_name.
    """
    cursor.execute(
        "SELECT id, metric_name FROM alerts WHERE status = 'active' AND metric_name LIKE %s",
        (f"{PREFIX}%",),
    )
    rows = cursor.fetchall()
    matched = []
    for row_id, metric_name in rows:
        slug = metric_name[len(PREFIX):]
        if slug in excluded_slugs:
            continue
        if _looks_like_noise(slug):
            matched.append((row_id, metric_name))
    return matched


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true", help="delete matched rows for real")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes (default)")
    parser.add_argument(
        "--exclude", default="",
        help="comma-separated slugs to skip even if they'd otherwise match, e.g. --exclude data,var_lib_mysql",
    )
    args = parser.parse_args()
    apply_ = args.apply and not args.dry_run
    excluded_slugs = {s.strip() for s in args.exclude.split(",") if s.strip()}

    conn = get_connection()
    cursor = conn.cursor()

    matched = find_noise_rows(cursor, excluded_slugs)
    matched_alerts = find_noise_alert_rows(cursor, excluded_slugs)

    if not matched and not matched_alerts:
        print("No disk_used_percent__* rows matched the noise heuristic in metric_catalog or alerts -- nothing to do.")
        cursor.close(); conn.close()
        return

    if matched:
        print(f"Matched {len(matched)} pseudo/ephemeral mount row(s) in metric_catalog:")
        for _id, name in matched:
            print(f"  {name}")
    else:
        print("No matching metric_catalog rows (already cleaned, or never registered).")

    if matched_alerts:
        print(f"\nMatched {len(matched_alerts)} currently-ACTIVE alert(s) on the same noise pattern:")
        for _id, name in matched_alerts:
            print(f"  {name}")
    else:
        print("\nNo matching active alerts.")

    if not apply_:
        print("\n[dry-run] No changes made. Re-run with --apply to act on these for real:")
        print("          - metric_catalog matches get DELETED (with their thresholds/selections/metrics/history)")
        print("          - alert matches get RESOLVED (status='resolved'), not deleted -- alerts stay as history")
        print("          If any of the above is a real mount, exclude it: --apply --exclude <slug>")
        cursor.close(); conn.close()
        return

    deleted_catalog = deleted_thresholds = deleted_selections = 0
    deleted_metrics = deleted_history = 0
    if matched:
        metric_names = [name for _id, name in matched]
        metric_ids = [row_id for row_id, _name in matched]

        placeholders_names = ",".join(["%s"] * len(metric_names))
        placeholders_ids = ",".join(["%s"] * len(metric_ids))

        cursor.execute(f"DELETE FROM metric_history WHERE metric_name IN ({placeholders_names})", metric_names)
        deleted_history = cursor.rowcount
        cursor.execute(f"DELETE FROM metrics WHERE metric_name IN ({placeholders_names})", metric_names)
        deleted_metrics = cursor.rowcount
        cursor.execute(f"DELETE FROM thresholds WHERE metric_id IN ({placeholders_ids})", metric_ids)
        deleted_thresholds = cursor.rowcount
        cursor.execute(f"DELETE FROM account_metric_selections WHERE metric_id IN ({placeholders_ids})", metric_ids)
        deleted_selections = cursor.rowcount
        cursor.execute(f"DELETE FROM metric_catalog WHERE id IN ({placeholders_ids})", metric_ids)
        deleted_catalog = cursor.rowcount

    resolved_alerts = 0
    if matched_alerts:
        alert_ids = [row_id for row_id, _name in matched_alerts]
        placeholders_alert_ids = ",".join(["%s"] * len(alert_ids))
        cursor.execute(
            f"UPDATE alerts SET status='resolved', resolved_at=NOW() "
            f"WHERE id IN ({placeholders_alert_ids}) AND status='active'",
            alert_ids,
        )
        resolved_alerts = cursor.rowcount

    conn.commit()
    cursor.close()
    conn.close()

    print(
        f"\nDeleted: {deleted_catalog} metric_catalog, {deleted_thresholds} thresholds, "
        f"{deleted_selections} account_metric_selections, {deleted_metrics} metrics, "
        f"{deleted_history} metric_history row(s)."
    )
    print(f"Resolved: {resolved_alerts} active alert row(s).")


if __name__ == "__main__":
    main()
