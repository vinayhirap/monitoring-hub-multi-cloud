# app/threshold_coverage.py
"""
Alert coverage + "apply recommended defaults" (audit F1: most enabled metrics had blank thresholds, so they were
collected - and paid for - but could never alert).

Why rows are blank: thresholds rows seeded before a metric had a real default in app/threshold_defaults.py were
created with the placeholder (1,000,000 / 5,000,000 / '>'), and INSERT IGNORE never revisits a row. The evaluator
treats a placeholder as "no static line" (anomaly detection only) and the UI shows it blank.

Each enabled threshold row is classified:
  alerting       a real static line (or dynamic band) is set
  upgradable     placeholder, but the code now ships a real default for that metric -> can be applied in one click
  collect_only   placeholder and no real default exists (volume / byte-count metrics where no honest static
                 number exists; covered by anomaly detection only)
  disabled       the threshold row is switched off

upgrade_placeholders() only ever rewrites rows that are EXACTLY the placeholder, so a value a person chose is
never overwritten. use_dynamic / dynamic_k / enabled / evaluation_period are left untouched.
"""
from app.threshold_defaults import DEFAULT_THRESHOLDS, PLACEHOLDER_THRESHOLD, is_placeholder_threshold

SELECT_ROWS = """
    SELECT t.id, t.resource_type, t.warning_value, t.critical_value, t.comparison,
           t.enabled, t.use_dynamic, mc.metric_name, mc.service
    FROM thresholds t
    JOIN metric_catalog mc ON mc.id = t.metric_id
    WHERE t.aws_account_id = %s
"""


def recommended(metric_name):
    """The shipped default for a metric if it is a real line, else None."""
    d = DEFAULT_THRESHOLDS.get(metric_name)
    if d is None or is_placeholder_threshold(*d):
        return None
    return d


def classify(row) -> str:
    if not row.get("enabled"):
        return "disabled"
    if row.get("use_dynamic"):
        return "alerting"
    if not is_placeholder_threshold(row.get("warning_value"), row.get("critical_value"), row.get("comparison")):
        return "alerting"
    return "upgradable" if recommended(row.get("metric_name")) else "collect_only"


def coverage(cursor, account_id: int) -> dict:
    cursor.execute(SELECT_ROWS, (account_id,))
    rows = cursor.fetchall() or []
    by_service, totals = {}, {"alerting": 0, "upgradable": 0, "collect_only": 0, "disabled": 0}
    upgradable = []
    for r in rows:
        state = classify(r)
        totals[state] += 1
        svc = r.get("service") or r.get("resource_type") or "other"
        bucket = by_service.setdefault(svc, {"alerting": 0, "upgradable": 0, "collect_only": 0, "disabled": 0})
        bucket[state] += 1
        if state == "upgradable":
            w, c, comp = recommended(r["metric_name"])
            upgradable.append({"id": r["id"], "service": svc, "metric": r["metric_name"],
                               "warning": w, "critical": c, "comparison": comp})
    enabled = totals["alerting"] + totals["upgradable"] + totals["collect_only"]
    return {
        "enabled_metrics": enabled,
        "alerting": totals["alerting"],
        "coverage_percent": round(100.0 * totals["alerting"] / enabled, 1) if enabled else None,
        "totals": totals,
        "by_service": by_service,
        "upgradable": sorted(upgradable, key=lambda u: (u["service"], u["metric"])),
    }


def upgrade_placeholders(cursor, account_id: int, dry_run: bool = True) -> dict:
    """Apply the shipped default to every upgradable row. Returns the list of changes either way."""
    cov = coverage(cursor, account_id)
    changes = cov["upgradable"]
    applied = 0
    if not dry_run:
        for ch in changes:
            cursor.execute(
                "UPDATE thresholds SET warning_value = %s, critical_value = %s, comparison = %s "
                "WHERE id = %s AND aws_account_id = %s AND warning_value = %s AND critical_value = %s "
                "AND comparison = %s",           # re-check: never touch a row someone edited meanwhile
                (ch["warning"], ch["critical"], ch["comparison"], ch["id"], account_id,
                 PLACEHOLDER_THRESHOLD[0], PLACEHOLDER_THRESHOLD[1], PLACEHOLDER_THRESHOLD[2]),
            )
            applied += cursor.rowcount
    return {"dry_run": dry_run, "candidates": len(changes), "applied": applied, "changes": changes}
