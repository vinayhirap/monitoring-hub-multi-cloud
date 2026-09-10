# app/collector/disk_mounts.py
"""
Shared helpers for EC2 CWAgent multi-mount-point disk support (Section 3,
"Disk multi-mount-point-support -- not started" in the handover).

BACKGROUND -- what changed and why
------------------------------------
CWAgent publishes disk_used_percent as ONE METRIC PER MOUNT POINT (path/
device/fstype dimensions) -- an instance with "/" and "/data" both
mounted produces two distinct CloudWatch series. Before this, both
app/aws/collector_direct.py (on-demand instance-detail chart) and
app/collector/metrics/runner.py (scheduled threshold collection) picked
ONE mount (root -- "/" or "C:" -- else whichever CloudWatch returned
first) and silently discarded the rest. A data volume filling up while
root stayed healthy would never show on a chart or fire a threshold
alert.

DESIGN -- why metric-name suffixing instead of a schema migration
--------------------------------------------------------------------
metric_name is the join key threaded through `metrics`, `metric_history`,
`metric_catalog`, `thresholds`, and `alerts` (see
app/collector/alert_evaluator.py's core query: `mc.metric_name =
m.metric_name`) -- adding a "which mount" column would mean touching
every one of those tables and every query that joins them. Instead:
  - the root mount keeps its existing name (`disk_used_percent`) --
    ZERO behavior change for every account that only has one mount;
  - each ADDITIONAL mount gets its own synthetic name:
    `disk_used_percent__<slug>`, slug = a sanitized form of the mount
    path (e.g. "/data" -> "data", "/var/lib/mysql" -> "var_lib_mysql").

The first time a synthetic name is seen for an account,
ensure_disk_mount_metric_registered() clones that account's existing
disk_used_percent metric_catalog/threshold/selection rows under the new
name (falling back to threshold_defaults.py's default if none exist
yet). After that one-time registration, the new name flows through
every existing table/join/alert path completely unmodified -- exactly
like any other real metric_name already does. Nothing in
alert_evaluator.py needed to change.

SCOPE BOUNDARY, on purpose: the EC2 instance-detail page's chart
currently renders `disk_used_percent` as a single hardcoded series.
This module makes the full per-mount data available -- both to
metrics/runner.py's scheduled path (which now ALERTS per mount) and to
collector_direct.py's on-demand path (which returns ALL mounts under a
new `disk_used_percent_by_mount` key, additive alongside the unchanged
`disk_used_percent` key for backward compatibility) -- but actually
drawing multiple lines on that chart needs a small, separate frontend
change. Guessing at unreviewed React component structure risks shipping
a broken UI rather than no UI change, so that part is intentionally
left as a documented follow-up rather than attempted here.

CONFIDENCE: the metric_catalog unique key (namespace, metric_name) is
confirmed from db/migrations/003_metric_catalog_full.sql. The
account_metric_selections unique key (aws_account_id, metric_id) is
confirmed from its CREATE TABLE. The thresholds table's unique keys
are now confirmed too, from an actual mysqldump
(db/backups/pre_alert_hardening_20260825_104729.sql): `uniq_threshold`
(aws_account_id, resource_type, metric_id) AND a SEPARATE
`uniq_acc_metric` (aws_account_id, metric_id) -- i.e. at most one
threshold row per (account, metric_id) regardless of resource_type.
ensure_disk_mount_metric_registered() still does a SELECT-before-INSERT
check rather than relying on ON DUPLICATE KEY / INSERT IGNORE, which
remains correct (and arguably clearer) under either key. Not yet
verified against a live account with multiple real mount points -- no
AWS credentials available in this environment. Mount-noise filtering
(pseudo/ephemeral filesystems like snap loopbacks) WAS confirmed
against a live account -- see apply_cleanup_disk_mount_noise.py.
"""
import logging
import re

from app.db import get_connection
from app.threshold_defaults import DEFAULT_THRESHOLDS

logger = logging.getLogger(__name__)

ROOT_PATHS = ("/", "C:")
BASE_METRIC_NAME = "disk_used_percent"

# Pseudo/ephemeral filesystems that CWAgent will happily report
# disk_used_percent for alongside real mounts, but which are never a
# genuine "is this disk filling up" concern -- worst offender: snap's
# squashfs loopback mounts are READ-ONLY images sized exactly to their
# content, so they permanently report ~100% used. Registering one as a
# tracked metric means its threshold card fires CRITICAL the moment
# it's first collected, for something that was never actionable.
# Confirmed live: a real account's CWAgent reported 45 such mounts
# (every /snap/*/<rev>, /run, /run/lock, /run/user/<uid>,
# /run/snapd/ns, /dev, /dev/shm, /boot/efi) alongside its one real
# root mount -- see chat history for the full list.
IGNORED_FSTYPES = {
    "squashfs", "tmpfs", "devtmpfs", "overlay", "proc", "sysfs",
    "cgroup", "cgroup2", "devpts", "mqueue", "tracefs", "debugfs",
    "securityfs", "pstore", "bpf", "autofs", "nsfs", "binfmt_misc",
    "hugetlbfs", "fusectl", "configfs", "efivarfs",
}
# Secondary, independent check on the path itself -- kept in addition
# to the fstype check (not instead of it) because CWAgent's dimension
# set is config-dependent and an older/misconfigured agent could omit
# `fstype` entirely; path-prefix matching still catches the common
# cases in that situation. NOT applied to ROOT_PATHS, which are always
# treated as real regardless of what either check says.
IGNORED_PATH_PREFIXES = ("/snap/", "/var/snap/", "/run", "/dev", "/proc", "/sys", "/boot/efi")


def _is_real_mount(path: str, dims: dict) -> bool:
    fstype = (dims.get("fstype") or "").lower()
    if fstype in IGNORED_FSTYPES:
        return False
    if any(path == p or path.startswith(p) for p in IGNORED_PATH_PREFIXES):
        return False
    return True


def slugify_mount_path(path: str) -> str:
    """'/data' -> 'data', '/var/lib/mysql' -> 'var_lib_mysql', 'D:' -> 'd'."""
    s = (path or "").strip().strip("/").replace(":", "").replace("\\", "_").replace("/", "_")
    s = re.sub(r"[^a-zA-Z0-9_]", "_", s).strip("_").lower()
    return s or "root"


def metric_name_for_mount(path: str) -> str:
    """Root mount keeps the existing, unmodified name -- every account
    not using multiple mount points sees zero change in metric_name,
    stored data, thresholds, or alerts."""
    if path in ROOT_PATHS:
        return BASE_METRIC_NAME
    return f"{BASE_METRIC_NAME}__{slugify_mount_path(path)}"


def all_cwagent_disk_dims(cw, instance_id):
    """
    Returns [(dimensions, path, metric_name), ...] for EVERY mount point
    CWAgent has published disk_used_percent under for this instance
    that's a genuine, actionable mount -- pseudo/ephemeral filesystems
    (snap loopbacks, tmpfs, /proc, /sys, etc. -- see _is_real_mount())
    are filtered out before registration, since those would otherwise
    get treated as real disk-space metrics and could fire permanently-
    stuck thresholds. Root is always included regardless of fstype.
    Empty list if CWAgent isn't reporting disk_used_percent at all for
    this instance.
    """
    try:
        resp = cw.list_metrics(
            Namespace="CWAgent", MetricName=BASE_METRIC_NAME,
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        )
        metrics = resp.get("Metrics", [])
    except Exception as e:
        logger.warning(f"CWAgent disk mount lookup [{instance_id}]: {e}")
        return []

    out = []
    seen_paths = set()
    skipped = []
    for m in metrics:
        dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
        path = dims.get("path") or "/"
        if path in seen_paths:
            continue  # CWAgent can report the same path under >1 device/fstype combo
        seen_paths.add(path)
        if path not in ROOT_PATHS and not _is_real_mount(path, dims):
            skipped.append(path)
            continue
        out.append((m["Dimensions"], path, metric_name_for_mount(path)))
    if skipped:
        logger.info(f"disk_mounts: skipped {len(skipped)} pseudo/ephemeral mount(s) for {instance_id}: {sorted(skipped)}")
    return out


def _clone_row(cursor, table, select_sql, select_params, overrides: dict):
    """
    Generic 'clone an existing row into a new one with some columns
    overridden' helper -- SELECT the source row, splice in overrides,
    INSERT IGNORE. Avoids hardcoding a full column list that could
    drift from the real schema. Returns the new row dict, or None if
    the source row didn't exist.
    """
    cursor.execute(select_sql, select_params)
    row = cursor.fetchone()
    if not row:
        return None
    row = dict(row)
    row.pop("id", None)
    row.update(overrides)
    cols = list(row.keys())
    placeholders = ",".join(["%s"] * len(cols))
    col_list = ",".join(f"`{c}`" for c in cols)
    cursor.execute(
        f"INSERT IGNORE INTO {table} ({col_list}) VALUES ({placeholders})",
        [row[c] for c in cols],
    )
    return row


def ensure_disk_mount_metric_registered(aws_account_id: int, resource_type: str, metric_name: str, path: str):
    """
    Idempotently registers a non-root disk mount's synthetic metric_name
    so it flows through the EXISTING metric_catalog -> thresholds ->
    alert_evaluator join unmodified:
      1. Clone the base 'disk_used_percent' metric_catalog row (same
         namespace/service/unit/statistic/category) under the new name,
         if not already present.
      2. Clone this account's existing disk_used_percent threshold (or
         fall back to threshold_defaults.py's default) for the new
         metric_id, if not already present.
      3. Add an account_metric_selections row (source='discovered'),
         same auto-enable precedent as GCP extended-tier discovery uses
         (app/api/metric_catalog.py's enable_metrics_for_services).
    Safe to call every collection cycle -- steps 1 and 3 are INSERT
    IGNORE on confirmed unique keys; step 2 does its own existence
    check first rather than relying on an unconfirmed unique key (see
    module docstring).
    """
    if metric_name == BASE_METRIC_NAME:
        return  # root mount already has a real, curated catalog entry

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id FROM metric_catalog WHERE metric_name = %s", (metric_name,))
        existing = cursor.fetchone()
        if existing:
            metric_id = existing["id"]
        else:
            base = _clone_row(
                cursor, "metric_catalog",
                "SELECT * FROM metric_catalog WHERE metric_name = %s LIMIT 1",
                (BASE_METRIC_NAME,),
                overrides={
                    "metric_name": metric_name,
                    "description": f"Disk space utilized -- mount {path}",
                },
            )
            if not base:
                logger.warning(
                    f"disk_mounts: no base '{BASE_METRIC_NAME}' metric_catalog row to "
                    f"clone from -- run scripts/seed_metric_catalog.py first. Skipping "
                    f"registration of {metric_name}."
                )
                conn.rollback()
                return
            cursor.execute("SELECT id FROM metric_catalog WHERE metric_name = %s", (metric_name,))
            metric_id = cursor.fetchone()["id"]

        cursor.execute(
            "SELECT id FROM thresholds WHERE aws_account_id = %s AND resource_type = %s AND metric_id = %s",
            (aws_account_id, resource_type, metric_id),
        )
        if not cursor.fetchone():
            cloned = _clone_row(
                cursor, "thresholds",
                """SELECT t.* FROM thresholds t JOIN metric_catalog mc ON mc.id = t.metric_id
                   WHERE mc.metric_name = %s AND t.aws_account_id = %s AND t.resource_type = %s
                   LIMIT 1""",
                (BASE_METRIC_NAME, aws_account_id, resource_type),
                overrides={"metric_id": metric_id},
            )
            if not cloned:
                warning, critical, comparison = DEFAULT_THRESHOLDS.get(BASE_METRIC_NAME, (80, 90, ">"))
                cursor.execute(
                    """INSERT IGNORE INTO thresholds
                           (aws_account_id, resource_type, metric_id, warning_value,
                            critical_value, comparison, evaluation_period, enabled)
                       VALUES (%s, %s, %s, %s, %s, %s, 5, 1)""",
                    (aws_account_id, resource_type, metric_id, warning, critical, comparison),
                )

        cursor.execute(
            """INSERT IGNORE INTO account_metric_selections
                   (aws_account_id, metric_id, enabled, source)
               VALUES (%s, %s, 1, 'discovered')""",
            (aws_account_id, metric_id),
        )
        conn.commit()
        logger.info(f"disk_mounts: registered {metric_name} (mount {path}) for account {aws_account_id}")
    except Exception as e:
        logger.warning(f"disk_mounts: registering {metric_name} failed: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()
