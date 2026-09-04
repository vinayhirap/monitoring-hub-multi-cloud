#!/usr/bin/env python3
"""
apply_alert_toast_account_region_fix.py — fixes the real-time alert toast
(top-right WARNING/CRITICAL popup, AlertToast.jsx) showing an opaque
"Account #7" instead of anything a human can act on.

ROOT CAUSE
----------
The WebSocket payload published on every new alert (app/ws/publisher.py:
publish_alert, called from app/collector/alert_evaluator.py) only ever
carried the numeric `account_id`. AlertToast.jsx just renders
`Account #{toast.account_id}` because that's the only account info it
was ever given — the account's display name and region never left the
backend. This is the same "which account/region is this?" gap the REST
alerts list already solved (app/api/alerts.py joins `aws_accounts acc`
and selects `acc.account_name`, `COALESCE(a.region, acc.default_region)
AS region`) — the live WebSocket push just never got the same fields.

THE FIX
-------
app/collector/alert_evaluator.py
  - SELECT aa.account_name and aa.default_region alongside the existing
    r.region, so a display region is available even for a resource row
    with no region of its own.
  - Pass account_name and region (resource region, falling back to the
    account's default region) into publish_alert().

app/ws/publisher.py
  - publish_alert() gains account_name and region parameters and puts
    them in the WebSocket payload.

frontend/src/components/AlertToast.jsx
  - Reads account_name/region off the WS message and renders
    "<account name> · <region>" instead of "Account #<id>". Falls back
    to "Account #<id>" if account_name is ever missing (older backend,
    mid-deploy), so the toast never renders blank.

Usage:
    python apply_alert_toast_account_region_fix.py --dry-run
    python apply_alert_toast_account_region_fix.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-alert-toast-account-region-fix"


class PatchError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────
# Patches
# ─────────────────────────────────────────────────────────────────────────
PATCHES = [
    (
        "app/collector/alert_evaluator.py",
        [
            (
                '''    cursor.execute("""
        SELECT
            m.resource_id          AS db_resource_id,
            r.resource_id          AS aws_resource_id,
            r.resource_type,
            r.aws_account_id,
            r.tags,
            r.region,
            m.metric_name,
            m.metric_value,
            m.metric_timestamp,
            t.id                   AS threshold_id,
            t.warning_value,
            t.critical_value,
            t.comparison,
            t.evaluation_period
        FROM metrics m
        JOIN resources r
            ON r.id = m.resource_id
        JOIN aws_accounts aa
            ON aa.id = r.aws_account_id
           AND aa.status = 'active'
        JOIN metric_catalog mc
            ON mc.metric_name = m.metric_name
        JOIN thresholds t
            ON t.metric_id       = mc.id
           AND t.resource_type   = r.resource_type
           AND t.aws_account_id  = r.aws_account_id
           AND t.enabled         = 1
        JOIN (
            SELECT resource_id, metric_name, MAX(metric_timestamp) AS ts
            FROM metrics
            GROUP BY resource_id, metric_name
        ) latest
            ON latest.resource_id = m.resource_id
           AND latest.metric_name = m.metric_name
           AND latest.ts          = m.metric_timestamp
        WHERE m.metric_timestamp >= DATE_SUB(NOW(), INTERVAL 10 MINUTE)
    """)''',
                '''    cursor.execute("""
        SELECT
            m.resource_id          AS db_resource_id,
            r.resource_id          AS aws_resource_id,
            r.resource_type,
            r.aws_account_id,
            r.tags,
            r.region,
            aa.account_name,
            aa.default_region,
            m.metric_name,
            m.metric_value,
            m.metric_timestamp,
            t.id                   AS threshold_id,
            t.warning_value,
            t.critical_value,
            t.comparison,
            t.evaluation_period
        FROM metrics m
        JOIN resources r
            ON r.id = m.resource_id
        JOIN aws_accounts aa
            ON aa.id = r.aws_account_id
           AND aa.status = 'active'
        JOIN metric_catalog mc
            ON mc.metric_name = m.metric_name
        JOIN thresholds t
            ON t.metric_id       = mc.id
           AND t.resource_type   = r.resource_type
           AND t.aws_account_id  = r.aws_account_id
           AND t.enabled         = 1
        JOIN (
            SELECT resource_id, metric_name, MAX(metric_timestamp) AS ts
            FROM metrics
            GROUP BY resource_id, metric_name
        ) latest
            ON latest.resource_id = m.resource_id
           AND latest.metric_name = m.metric_name
           AND latest.ts          = m.metric_timestamp
        WHERE m.metric_timestamp >= DATE_SUB(NOW(), INTERVAL 10 MINUTE)
    """)''',
            ),
            (
                '''            publish_alert(
                alert_id   = new_alert_id,
                severity   = promoted_severity,
                metric     = metric_name,
                value      = metric_value,
                threshold  = threshold_value,
                account_id = aws_account_id,
            )''',
                '''            publish_alert(
                alert_id     = new_alert_id,
                severity     = promoted_severity,
                metric       = metric_name,
                value        = metric_value,
                threshold    = threshold_value,
                account_id   = aws_account_id,
                account_name = row["account_name"],
                region       = row["region"] or row["default_region"],
            )''',
            ),
        ],
    ),
    (
        "app/ws/publisher.py",
        [
            (
                '''def publish_alert(alert_id: int, severity: str, metric: str,
                  value: float, threshold: float, account_id: int):
    publish("alerts", {
        "type": "new_alert",
        "alert_id": alert_id,
        "severity": severity,
        "metric": metric,
        "value": round(value, 2),
        "threshold": round(threshold, 2),
        "account_id": account_id,
    })''',
                '''def publish_alert(alert_id: int, severity: str, metric: str,
                  value: float, threshold: float, account_id: int,
                  account_name: str = None, region: str = None):
    publish("alerts", {
        "type": "new_alert",
        "alert_id": alert_id,
        "severity": severity,
        "metric": metric,
        "value": round(value, 2),
        "threshold": round(threshold, 2),
        "account_id": account_id,
        "account_name": account_name,
        "region": region,
    })''',
            ),
        ],
    ),
    (
        "frontend/src/components/AlertToast.jsx",
        [
            (
                '''    const toast = {
      id:        Date.now(),
      severity:  alertMsg.severity  || "WARNING",
      metric:    alertMsg.metric    || "Unknown",
      value:     alertMsg.value     ?? 0,
      threshold: alertMsg.threshold ?? 0,
      account_id:alertMsg.account_id,
    };''',
                '''    const toast = {
      id:          Date.now(),
      severity:    alertMsg.severity   || "WARNING",
      metric:      alertMsg.metric     || "Unknown",
      value:       alertMsg.value      ?? 0,
      threshold:   alertMsg.threshold  ?? 0,
      account_id:  alertMsg.account_id,
      account_name:alertMsg.account_name,
      region:      alertMsg.region,
    };''',
            ),
            (
                '''        <div style={{ fontSize: 11, color: "#4a5f80" }}>Account #{toast.account_id}</div>''',
                '''        <div style={{ fontSize: 11, color: "#4a5f80" }}>
          {toast.account_name || `Account #${toast.account_id}`}
          {toast.region ? ` \u00b7 ${toast.region}` : ""}
        </div>''',
            ),
        ],
    ),
]


# ─────────────────────────────────────────────────────────────────────────
# Runner (same shape as apply_alert_window_consistency_fix.py)
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
            print("  1. Backend: full restart of the monitoring-hub service (systemctl restart")
            print("     monitoring-hub) so alert_evaluator.py / publisher.py pick up the change.")
            print("  2. Frontend: rebuild (npm run build) and redeploy the static bundle, then")
            print("     hard-refresh the browser tab — AlertToast.jsx is client-side JS.")
            print("  3. Trigger or wait for a new alert: the toast should now read")
            print('     "<Account Name> · <region>" instead of "Account #7". Existing alerts')
            print("     already in the `alerts` table are unaffected — this only changes the")
            print("     live WebSocket push for NEW alerts going forward.")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
