#!/usr/bin/env python3
"""
apply_account_health_rollup_fix.py — fixes the account-status
architecture bug where the Overview dashboard's summary tiles (Total /
Healthy / Warning / Critical) and per-account cards could show 0
critical / 0 healthy even while the alerts banner correctly says
"7 CRITICAL · 3 WARNING" for the very same account.

ROOT CAUSE
Three different places on the dashboard each computed "how bad is this
account" a different way:

  1. The alerts banner ("X CRITICAL · Y WARNING") counts the real
     `alerts` table directly — correct, and the reason the number 7
     shows up at all.
  2. The account status pill / summary tiles (Total/Healthy/Warning/
     Critical) came from app/api/live_data.py's `status` field, which
     only checked for critical/warning alerts by intersecting against
     that account's EC2 instance IDs. A critical alert on an EBS
     volume, S3 bucket, RDS instance, or Lambda function — anything
     that isn't an EC2 instance — was silently invisible to this
     calculation. It also fell back to an avg-CPU heuristic that could
     mask a real alert if the intersection above found nothing.
  3. Each account/region card's own "N critical / N warning" badge
     used a THIRD method: `alerts.filter(a => a.resource.includes(
     account_id))`, a fragile substring match against a raw AWS
     account id that most resource identifiers (i-xxxx, vol-xxxx,
     function ARNs, ...) never contain — so this mostly undercounts
     too, independently of (2).

     None of the three ever had to agree with each other, so they
     didn't.

THE FIX — one source of truth
  - app/aws/collector_direct.py is untouched; the fix is entirely in
    app/api/live_data.py: _get_active_alert_resources() (returned raw
    resource-id sets) is replaced with
    _get_active_alert_counts_by_account(), which joins `alerts` to
    `resources` on resource_id to resolve EVERY alerting resource —
    regardless of type — to its aws_account_id in one grouped SQL
    query. live_accounts() now derives `status`, `alerts`,
    `critical_alerts`, and `warning_alerts` from this per-account
    count instead of an EC2-only intersection. (The previous code also
    hardcoded the account's "alerts" field to 0 unconditionally —
    fixed as a side effect of the same change.) avg_cpu remains as a
    fallback ONLY when an account has zero active alerts of any kind;
    it can never mask a real one anymore.

  - frontend/src/pages/Overview.jsx: AccountGroupCard and RegionRow
    each had their own independent, fragile substring-match
    computation of an account/region's alert counts. Both are replaced
    with the SAME critical_alerts/warning_alerts fields the backend
    now computes authoritatively (summed across regions via
    aggregateStats for the account-level card) — so the account
    card's badge, its health ring, its status pill, and the top
    summary tiles can never disagree again, because they all read
    from the same number. The now-unused `alerts` prop threaded through
    AccountGroupCard -> RegionRow is removed along with the dead code.

Usage:
    python apply_account_health_rollup_fix.py --dry-run
    python apply_account_health_rollup_fix.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-account-health-rollup-fix"


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
                r'''def _get_active_alert_resources() -> set:
    try:
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT resource_id, severity
            FROM alerts
            WHERE status = 'active'
              AND (resolved_at IS NULL)
              AND triggered_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)
        """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        critical = {r["resource_id"] for r in rows if (r["severity"] or "").upper() == "CRITICAL"}
        warning  = {r["resource_id"] for r in rows if (r["severity"] or "").upper() == "WARNING"}
        return critical, warning
    except Exception as e:
        logger.error(f"Active alert fetch error: {e}")
        return set(), set()


@router.get("/accounts")
def live_accounts():
    global _accounts_cache

    now = time.time()
    if _accounts_cache["data"] is not None and now - _accounts_cache["ts"] < CACHE_TTL:
        return _accounts_cache["data"]

    accounts = _get_db_accounts()

    critical_resources, warning_resources = _get_active_alert_resources()

    def process_account(acc):
        region  = acc.get("default_region")
        summary = get_account_summary(region)
        running = summary.get("ec2_running", 0)
        total   = summary.get("ec2_total",   0)
        avg_cpu = summary.get("ec2_avg_cpu", 0)

        ec2_list     = summary.get("instances", [])
        instance_ids = {i["instance_id"] for i in ec2_list}

        has_critical = bool(critical_resources & instance_ids)
        has_warning  = bool(warning_resources  & instance_ids)

        if has_critical:
            health = "critical"
        elif has_warning:
            health = "warning"
        elif avg_cpu > 80:
            health = "critical"
        elif avg_cpu > 60:
            health = "warning"
        else:
            health = "healthy"

        unhealthy_ids   = (critical_resources | warning_resources) & instance_ids
        healthy_count   = running - len(unhealthy_ids)
        unhealthy_count = len(unhealthy_ids)

        services = []
        if summary.get("ec2_total", 0) > 0:
            services.append({
                "name":           "EC2",
                "status":         "ok",
                "instance_count": running,
                "cpu":            avg_cpu,
                "memory":         0,
            })
        if summary.get("rds_total", 0) > 0:
            services.append({
                "name":           "RDS",
                "status":         "ok",
                "instance_count": summary["rds_total"],
            })
        if summary.get("lambda_total", 0) > 0:
            services.append({
                "name":           "Lambda",
                "status":         "ok",
                "instance_count": summary["lambda_total"],
            })

        return _serialize({
            "id":               acc["id"],
            "account_name":     acc["account_name"],
            "account_id":       acc["account_id"],
            "region":           region,
            "status":           health,
            "environment":      acc.get("environment", "PROD"),
            "owner_team":       acc.get("owner_team", acc.get("team", "")),
            "ec2_total":        total,
            "ec2_running":      running,
            "ec2_stopped":      summary.get("ec2_stopped", 0),
            "ebs_total":        summary.get("ebs_total",    0),
            "rds_total":        summary.get("rds_total",    0),
            "lambda_total":     summary.get("lambda_total", 0),
            "s3_total":         summary.get("s3_total",     0),
            "elb_total":        summary.get("elb_total",    0),
            "ecs_total":        summary.get("ecs_total",    0),
            "avg_cpu":          avg_cpu,
            "alerts":           0,
            "instance_count":   total,
            "healthy_resources":   max(healthy_count, 0),
            "unhealthy_resources": unhealthy_count,
            "services":         services,
            "created_at":       acc.get("created_at"),
            "last_synced_at":   acc.get("last_synced_at"),
        })

    result = []
    with ThreadPoolExecutor(max_workers=min(len(accounts), 8)) as ex:
        futures = {ex.submit(process_account, acc): acc for acc in accounts}
        for f in as_completed(futures):
            try:
                result.append(f.result())
            except Exception as e:
                logger.error(f"Account processing error: {e}")

    status_order = {"critical": 0, "warning": 1, "healthy": 2}
    result.sort(key=lambda a: status_order.get(a.get("status", "healthy"), 9))

    _accounts_cache = {"data": result, "ts": now}
    return result


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


@router.get("/accounts")
def live_accounts():
    global _accounts_cache

    now = time.time()
    if _accounts_cache["data"] is not None and now - _accounts_cache["ts"] < CACHE_TTL:
        return _accounts_cache["data"]

    accounts = _get_db_accounts()

    alert_counts_by_account = _get_active_alert_counts_by_account()

    def process_account(acc):
        region  = acc.get("default_region")
        summary = get_account_summary(region)
        running = summary.get("ec2_running", 0)
        total   = summary.get("ec2_total",   0)
        avg_cpu = summary.get("ec2_avg_cpu", 0)

        counts        = alert_counts_by_account.get(acc["id"], {"critical": 0, "warning": 0})
        acct_critical = counts["critical"]
        acct_warning  = counts["warning"]

        # Real active alerts (any resource type) are authoritative.
        # avg_cpu is only a fallback heuristic for the rare case where
        # nothing has alerted yet at all — it must never override an
        # actual open alert, critical or warning.
        if acct_critical > 0:
            health = "critical"
        elif acct_warning > 0:
            health = "warning"
        elif avg_cpu > 80:
            health = "critical"
        elif avg_cpu > 60:
            health = "warning"
        else:
            health = "healthy"

        unhealthy_count = min(acct_critical + acct_warning, running) if running else (acct_critical + acct_warning)
        healthy_count   = max(running - unhealthy_count, 0)

        services = []
        if summary.get("ec2_total", 0) > 0:
            services.append({
                "name":           "EC2",
                "status":         "ok",
                "instance_count": running,
                "cpu":            avg_cpu,
                "memory":         0,
            })
        if summary.get("rds_total", 0) > 0:
            services.append({
                "name":           "RDS",
                "status":         "ok",
                "instance_count": summary["rds_total"],
            })
        if summary.get("lambda_total", 0) > 0:
            services.append({
                "name":           "Lambda",
                "status":         "ok",
                "instance_count": summary["lambda_total"],
            })

        return _serialize({
            "id":               acc["id"],
            "account_name":     acc["account_name"],
            "account_id":       acc["account_id"],
            "region":           region,
            "status":           health,
            "environment":      acc.get("environment", "PROD"),
            "owner_team":       acc.get("owner_team", acc.get("team", "")),
            "ec2_total":        total,
            "ec2_running":      running,
            "ec2_stopped":      summary.get("ec2_stopped", 0),
            "ebs_total":        summary.get("ebs_total",    0),
            "rds_total":        summary.get("rds_total",    0),
            "lambda_total":     summary.get("lambda_total", 0),
            "s3_total":         summary.get("s3_total",     0),
            "elb_total":        summary.get("elb_total",    0),
            "ecs_total":        summary.get("ecs_total",    0),
            "avg_cpu":          avg_cpu,
            # Was hardcoded to 0 before this fix, regardless of reality.
            "alerts":           acct_critical + acct_warning,
            "critical_alerts":  acct_critical,
            "warning_alerts":   acct_warning,
            "instance_count":   total,
            "healthy_resources":   healthy_count,
            "unhealthy_resources": unhealthy_count,
            "services":         services,
            "created_at":       acc.get("created_at"),
            "last_synced_at":   acc.get("last_synced_at"),
        })

    result = []
    with ThreadPoolExecutor(max_workers=min(len(accounts), 8)) as ex:
        futures = {ex.submit(process_account, acc): acc for acc in accounts}
        for f in as_completed(futures):
            try:
                result.append(f.result())
            except Exception as e:
                logger.error(f"Account processing error: {e}")

    status_order = {"critical": 0, "warning": 1, "healthy": 2}
    result.sort(key=lambda a: status_order.get(a.get("status", "healthy"), 9))

    _accounts_cache = {"data": result, "ts": now}
    return result


''',
            ),
        ],
    ),
    (
        "frontend/src/pages/Overview.jsx",
        [
            (
                """function aggregateStats(regions) {
  return regions.reduce(
    (acc, r) => ({
      ec2_total:    acc.ec2_total    + (r.ec2_total    || 0),
      ec2_running:  acc.ec2_running  + (r.ec2_running  || 0),
      ebs_total:    acc.ebs_total    + (r.ebs_total    || 0),
      s3_total:     acc.s3_total     + (r.s3_total     || 0),
      lambda_total: acc.lambda_total + (r.lambda_total || 0),
      rds_total:    acc.rds_total    + (r.rds_total    || 0),
    }),
    { ec2_total: 0, ec2_running: 0, ebs_total: 0, s3_total: 0, lambda_total: 0, rds_total: 0 }
  );
}""",
                """function aggregateStats(regions) {
  return regions.reduce(
    (acc, r) => ({
      ec2_total:       acc.ec2_total       + (r.ec2_total       || 0),
      ec2_running:     acc.ec2_running     + (r.ec2_running     || 0),
      ebs_total:       acc.ebs_total       + (r.ebs_total       || 0),
      s3_total:        acc.s3_total        + (r.s3_total        || 0),
      lambda_total:    acc.lambda_total    + (r.lambda_total    || 0),
      rds_total:       acc.rds_total       + (r.rds_total       || 0),
      // Authoritative per-account alert counts computed server-side in
      // app/api/live_data.py (resolved via resources.aws_account_id
      // across every resource type, not just EC2) -- summed across
      // this account's regions the same way every other stat here is.
      critical_alerts: acc.critical_alerts + (r.critical_alerts || 0),
      warning_alerts:  acc.warning_alerts  + (r.warning_alerts  || 0),
    }),
    {
      ec2_total: 0, ec2_running: 0, ebs_total: 0, s3_total: 0, lambda_total: 0, rds_total: 0,
      critical_alerts: 0, warning_alerts: 0,
    }
  );
}""",
            ),
            (
                """            <AccountGroupCard
              key={group.account_id}
              group={group}
              alerts={alerts}
              expanded={expandedIds.has(group.account_id)}""",
                """            <AccountGroupCard
              key={group.account_id}
              group={group}
              expanded={expandedIds.has(group.account_id)}""",
            ),
            (
                """function AccountGroupCard({ group, alerts, expanded, onToggle, onRegionClick, onDelete }) {
  const status = aggregateStatus(group.regions);
  const stats  = aggregateStats(group.regions);
  const regionCount = group.regions.length;

  // Aggregate alert counts across all regions
  const acctAlerts = alerts.filter(a => {
    const r = a.resource || a.resource_id || "";
    return r.includes(group.account_id || "____");
  });
  const activeAcctAlerts = acctAlerts.filter(a => (a.status || "").toLowerCase() === "active");
  const acctCritical     = activeAcctAlerts.filter(a => (a.severity || "").toUpperCase() === "CRITICAL").length;
  const acctWarning      = activeAcctAlerts.filter(a => (a.severity || "").toUpperCase() === "WARNING").length;""",
                """function AccountGroupCard({ group, expanded, onToggle, onRegionClick, onDelete }) {
  const status = aggregateStatus(group.regions);
  const stats  = aggregateStats(group.regions);
  const regionCount = group.regions.length;

  // Same server-computed counts that drove this account's status pill
  // above (via aggregateStatus/aggregateStats) -- the badge below can
  // never disagree with the ring/pill the way the old
  // alerts.filter(a => a.resource.includes(account_id)) substring
  // match sometimes did (most resource ids never literally contain
  // the AWS account id string).
  const acctCritical = stats.critical_alerts;
  const acctWarning  = stats.warning_alerts;""",
            ),
            (
                """            <RegionRow
              key={regionRow.id}
              regionRow={regionRow}
              alerts={alerts}
              onClick={() => onRegionClick(regionRow)}""",
                """            <RegionRow
              key={regionRow.id}
              regionRow={regionRow}
              onClick={() => onRegionClick(regionRow)}""",
            ),
            (
                """function RegionRow({ regionRow, alerts, onClick, onDelete }) {
  const status = regionRow.status || "healthy";

  const acctAlerts = alerts.filter(a => {
    const r = a.resource || a.resource_id || "";
    return r.includes(regionRow.account_id || "____");
  });
  const activeAlerts = acctAlerts.filter(a => (a.status || "").toLowerCase() === "active");
  const critical     = activeAlerts.filter(a => (a.severity || "").toUpperCase() === "CRITICAL").length;
  const warning      = activeAlerts.filter(a => (a.severity || "").toUpperCase() === "WARNING").length;""",
                """function RegionRow({ regionRow, onClick, onDelete }) {
  const status = regionRow.status || "healthy";

  // Same authoritative per-region counts the backend used to set
  // regionRow.status -- no more independent (and fragile) re-derivation
  // of severity from a raw alerts list here.
  const critical = regionRow.critical_alerts || 0;
  const warning  = regionRow.warning_alerts  || 0;""",
            ),
        ],
    ),
]

# ─────────────────────────────────────────────────────────────────────────
# Preflight / apply / validate
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
            print("  1. Backend: full uvicorn restart (not --reload).")
            print("  2. Frontend: cd frontend && npm install (if needed) && npm run build")
            print("  3. Reload Overview: the Critical/Warning/Healthy tiles, the alert")
            print("     banner, and each account card's badge should now all agree —")
            print("     an account with any active critical alert (on ANY resource")
            print("     type, not just EC2) shows as Critical everywhere, consistently.")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
