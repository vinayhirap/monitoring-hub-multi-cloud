# scripts/seed_metric_catalog.py
"""
Seeds the metric_catalog table from app/aws/metric_catalog_data.py.

Idempotent — safe to re-run any time the data file changes (e.g. after
adding a new curated service). Existing rows are updated in place;
nothing is deleted, so any account_metric_selections / thresholds rows
stay valid.

Handles reconciling pre-existing metric_catalog rows from the original
seed_thresholds.sql, which used different service keys/casing than the
curated data here (e.g. service='elb' vs 'alb', service='ecs_service'
vs 'ecs', 'dbconnections' vs 'DatabaseConnections'). Most of those
collide automatically via the case-insensitive (namespace, metric_name)
unique key, but a handful don't match textually at all and were left
behind as unlabeled orphan rows (display_service IS NULL). Those are
remapped in place by id; if a curated row already exists under the new
name (e.g. from a run before this reconcile step existed), any
threshold / account_metric_selections references are migrated onto it
and the now-redundant orphan is removed.

Usage:
    python scripts/seed_metric_catalog.py
Requires DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME env vars,
same as the app (see app/db.py) -- DB_PASSWORD has no fallback default
of any kind (by design, see app/db.py's _require_db_password()); this
script now fails immediately at import time if it's unset, rather than
silently falling back to anything.
"""
import os
from dotenv import load_dotenv
load_dotenv()
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_connection
from app.aws.metric_catalog_data import CURATED, DIRECTORY

# SECURITY: previously built its own mysql.connector.connect(**DB_CONFIG)
# with password=os.getenv("DB_PASSWORD", "root123") -- a hardcoded
# insecure fallback, the exact anti-pattern app/db.py's
# _require_db_password() was specifically hardened against (see that
# function's own docstring). If DB_PASSWORD were ever unset in whatever
# shell/environment this script gets run from (e.g. .env not found
# because it's invoked from a different working directory), this would
# have silently connected with a guessable default instead of failing
# loudly. Now reuses app.db.get_connection() -- the same pooled,
# already-hardened connection every other part of this app uses, which
# raises immediately at import time if DB_PASSWORD is missing, with no
# fallback of any kind.

# (old_service, old_namespace, old_metric_name) -> (new_service, curated_metric_name)
# Only needed for rows from the original seed_thresholds.sql that don't
# case-insensitively match their curated equivalent's metric_name.

# ── Cost-optimization: per-metric polling tier ─────────────────────────
# Every curated metric was previously seeded at a flat 60s interval — the
# #1 driver of CloudWatch GetMetricData cost, since it means AWS/S3
# BucketSizeBytes (which AWS itself only publishes once a day) was being
# polled just as fast as EC2 CPUUtilization. Tiering below fixes that:
#   critical (60s)  — core-service, is_default metrics: the handful of
#                     signals that genuinely need near-real-time polling.
#   standard (300s) — extended-service is_default metrics.
#   trend    (900s) — everything non-default (rarely alerted on).
# TIER_OVERRIDES covers metrics that don't fit that heuristic: things AWS
# only emits daily (no benefit from fast polling) and status/health
# signals worth keeping fast even though they're not "core".
TIER_INTERVAL = {"critical": 60, "standard": 300, "trend": 900}

TIER_OVERRIDES = {
    ("s3", "BucketSizeBytes"): "trend",
    ("s3", "NumberOfObjects"): "trend",
    ("alb", "HealthyHostCount"):          "critical",
    ("alb", "UnHealthyHostCount"):        "critical",
    ("route53", "HealthCheckStatus"):     "critical",
    ("vpn", "TunnelState"):               "critical",
    ("directconnect", "ConnectionState"): "critical",
}


def _tier_for(service_key: str, metric_name: str, category: str, is_default: bool) -> str:
    override = TIER_OVERRIDES.get((service_key, metric_name))
    if override:
        return override
    if category == "core" and is_default:
        return "critical"
    if is_default:
        return "standard"
    return "trend"


def _interval_for(service_key: str, metric_name: str, category: str, is_default: bool) -> int:
    return TIER_INTERVAL[_tier_for(service_key, metric_name, category, is_default)]


LEGACY_REMAP = {
    ("elb", "AWS/ApplicationELB", "errors5xx"):      ("alb", "HTTPCode_Target_5XX_Count"),
    ("elb", "AWS/ApplicationELB", "errors4xx"):      ("alb", "HTTPCode_Target_4XX_Count"),
    ("elb", "AWS/ApplicationELB", "responselatency"):("alb", "TargetResponseTime"),
    ("elb", "AWS/ApplicationELB", "healthyhosts"):   ("alb", "HealthyHostCount"),
    ("elb", "AWS/ApplicationELB", "unhealthyhosts"): ("alb", "UnHealthyHostCount"),
    ("ecs_service", "AWS/ECS", "memutilization"):    ("ecs", "MemoryUtilization"),
    ("rds", "AWS/RDS", "dbconnections"):             ("rds", "DatabaseConnections"),
    ("rds", "AWS/RDS", "freestorage"):                ("rds", "FreeStorageSpace"),
}


def reconcile_legacy_rows(cur):
    """
    Point orphan legacy rows at their curated metadata in place. If the
    curated target row already exists (e.g. seeded by an earlier run before
    this reconcile step existed), migrate any references off the orphan and
    remove the now-redundant duplicate instead of renaming into a collision.
    """
    fixed = 0
    merged = 0
    curated_lookup = {}
    for service_key, (display_name, namespace, category, metrics) in CURATED.items():
        for metric_name, unit, statistic, is_default, description in metrics:
            curated_lookup[(namespace, metric_name)] = (
                service_key, display_name, statistic, unit, category, description, is_default
            )

    for (old_service, old_namespace, old_metric_name), (new_service, new_metric_name) in LEGACY_REMAP.items():
        meta = curated_lookup.get((old_namespace, new_metric_name))
        if not meta:
            continue
        service_key, display_name, statistic, unit, category, description, is_default = meta

        cur.execute(
            "SELECT id FROM metric_catalog WHERE service=%s AND namespace=%s AND metric_name=%s",
            (old_service, old_namespace, old_metric_name)
        )
        old_row = cur.fetchone()
        if not old_row:
            continue
        old_id = old_row[0]

        cur.execute(
            "SELECT id FROM metric_catalog WHERE namespace=%s AND metric_name=%s",
            (old_namespace, new_metric_name)
        )
        target_row = cur.fetchone()

        if target_row and target_row[0] != old_id:
            # Curated row already exists (from an earlier run) — migrate any
            # references off the orphan, then drop the duplicate.
            target_id = target_row[0]
            cur.execute(
                "UPDATE IGNORE thresholds SET metric_id=%s WHERE metric_id=%s",
                (target_id, old_id)
            )
            cur.execute(
                "UPDATE IGNORE account_metric_selections SET metric_id=%s WHERE metric_id=%s",
                (target_id, old_id)
            )
            cur.execute("DELETE FROM thresholds WHERE metric_id=%s", (old_id,))
            cur.execute("DELETE FROM account_metric_selections WHERE metric_id=%s", (old_id,))
            cur.execute("DELETE FROM metric_catalog WHERE id=%s", (old_id,))
            merged += 1
        else:
            # No collision — safe to rename the orphan in place.
            cur.execute("""
                UPDATE metric_catalog
                   SET service = %s, metric_name = %s, display_service = %s,
                       statistic = %s, unit = %s, category = %s,
                       description = %s, is_default = %s
                 WHERE id = %s
            """, (service_key, new_metric_name, display_name, statistic, unit,
                  category, description, int(is_default), old_id))
            fixed += 1
    return fixed, merged


def main():
    conn = get_connection()
    cur = conn.cursor()

    reconciled, merged = reconcile_legacy_rows(cur)

    curated_count = 0
    tier_counts = {"critical": 0, "standard": 0, "trend": 0}
    for service_key, (display_name, namespace, category, metrics) in CURATED.items():
        for metric_name, unit, statistic, is_default, description in metrics:
            tier = _tier_for(service_key, metric_name, category, is_default)
            interval = TIER_INTERVAL[tier]
            tier_counts[tier] += 1
            cur.execute("""
                INSERT INTO metric_catalog
                    (service, namespace, display_service, metric_name,
                     statistic, unit, default_interval, category, description,
                     is_default, enabled)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
                ON DUPLICATE KEY UPDATE
                    service          = VALUES(service),
                    metric_name      = VALUES(metric_name),
                    display_service  = VALUES(display_service),
                    statistic        = VALUES(statistic),
                    unit             = VALUES(unit),
                    default_interval = VALUES(default_interval),
                    category         = VALUES(category),
                    description      = VALUES(description),
                    is_default       = VALUES(is_default)
            """, (service_key, namespace, display_name, metric_name,
                  statistic, unit, interval, category, description, int(is_default)))
            curated_count += 1

    directory_count = 0
    curated_namespaces = {ns for _, ns, _, _ in CURATED.values()}
    for display_name, namespace in DIRECTORY:
        if namespace in curated_namespaces:
            continue
        service_key = namespace.split("/")[-1].lower().replace(" ", "-")
        # metric_name uses a '' sentinel (not NULL) so the (namespace, metric_name)
        # unique key dedupes correctly on re-seed — MySQL treats NULLs as distinct.
        cur.execute("""
            INSERT INTO metric_catalog
                (service, namespace, display_service, metric_name,
                 statistic, unit, default_interval, category, description,
                 is_default, enabled)
            VALUES (%s,%s,%s,'',NULL,NULL,900,'directory',
                    'Namespace registered — use Discover to fetch live metric names',
                    0,1)
            ON DUPLICATE KEY UPDATE
                display_service  = VALUES(display_service),
                default_interval = VALUES(default_interval)
        """, (service_key, namespace, display_name))
        directory_count += 1

    # Safety net: anything still unlabeled at this point is a genuine leftover
    # we don't have a mapping for. Don't delete it (thresholds may reference
    # it) — just tag it so it's filtered out of the UI instead of rendering blank.
    cur.execute("""
        UPDATE metric_catalog
           SET display_service = CONCAT('Legacy: ', service)
         WHERE display_service IS NULL AND (metric_name IS NOT NULL AND metric_name != '')
    """)
    orphans_labeled = cur.rowcount

    conn.commit()
    cur.close()
    conn.close()
    print(f"Reconciled {reconciled} legacy rows in place, merged/removed {merged} duplicate(s), "
          f"seeded {curated_count} curated metrics "
          f"(critical/60s={tier_counts['critical']}, standard/300s={tier_counts['standard']}, "
          f"trend/900s={tier_counts['trend']}), {directory_count} directory namespaces "
          f"(900s), labeled {orphans_labeled} remaining orphan(s).")


if __name__ == "__main__":
    main()
