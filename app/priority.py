"""
Alert priority P1-P4 (phase 4b).

Priority answers "who gets woken up first", and is worked out from two things the platform already knows:
the resource's environment and the alert's severity.

                 critical   not critical (warning / info)
    PROD            P1            P2
    NON-PROD        P3            P4

* Only NON-PROD (dev, test, sit, uat, qa, staging ...) is ranked lower. Anything else -- PROD, a resource nobody has
  placed yet, a legacy or free-text label -- is ranked like PROD: an unclassified resource is never given a weaker
  priority than a production one.
* A resource tagged Criticality=critical goes up one level (never above P1), so an important NON-PROD system can be
  lifted to P2 / P3 by tagging it.

Flags (both default off):
    PRIORITY_ENABLED          compute and store the label. Changes no severity, escalation or notification.
    PRIORITY_ROUTING_ENABLED  (patch 2) escalation by priority and channel routing. Needs PRIORITY_ENABLED too.

The pure functions below have no database or framework imports so they can be tested on their own.
refresh_open_alert_priorities() is the only function that touches the database, and only through the cursor
it is given.
"""
import json
import os

from app.env_tiers import PROD, NONPROD, to_tier

P1, P2, P3, P4 = 1, 2, 3, 4
PRIORITIES = (P1, P2, P3, P4)

# (critical, not critical) per tier
_TABLE = {PROD: (P1, P2), NONPROD: (P3, P4)}

_TRUE = ("true", "1", "yes")


def enabled():
    return os.getenv("PRIORITY_ENABLED", "false").strip().lower() in _TRUE


def routing_enabled():
    """Routing needs the label to exist, so it is only on when both flags are."""
    return enabled() and os.getenv("PRIORITY_ROUTING_ENABLED", "false").strip().lower() in _TRUE


def tier_for(environment):
    """Any environment label -> prod | nonprod. Everything unrecognised is prod on purpose."""
    return to_tier(environment)


def is_critical_resource(tags):
    """True when the resource carries Criticality=critical (key and value case-insensitive).
    `tags` may be a dict, a JSON string, None or garbage; this never raises."""
    if isinstance(tags, (str, bytes)):
        try:
            tags = json.loads(tags or "{}")
        except Exception:
            return False
    if not isinstance(tags, dict):
        return False
    for k, v in tags.items():
        if str(k).strip().lower() == "criticality" and str(v).strip().lower() == "critical":
            return True
    return False


def compute_priority(environment, severity, tags=None):
    """-> 1..4. See the module docstring for the rule."""
    critical = str(severity or "").strip().upper() == "CRITICAL"
    p = _TABLE[tier_for(environment)][0 if critical else 1]
    if is_critical_resource(tags):
        p = max(P1, p - 1)
    return p


def label(priority):
    return f"P{int(priority)}" if priority in PRIORITIES else None


def refresh_open_alert_priorities(cursor, use_resolved_env=False, apply=True):
    """Work out the priority of every OPEN alert (active or acknowledged), whichever part of the platform
    raised it, and store it when it differs. Resolved alerts keep whatever they had.

    The environment comes from the resource's classification (resources.env_resolved) when
    `use_resolved_env` is true and the resource has one, otherwise from the label stored on the alert.
    Reclassifying a resource therefore moves its open alerts on the next call.

    With apply=False nothing is written (used by the backfill dry run).
    Returns {"checked", "changed", "by_priority": {1: n, ...}, "changes": [(alert_id, old, new)]}.
    """
    env_col = ", r.env_resolved AS env_resolved" if use_resolved_env else ""
    cursor.execute(f"""
        SELECT a.id AS id, a.severity AS severity, a.environment AS environment,
               a.priority AS priority, r.tags AS tags{env_col}
        FROM alerts a
        LEFT JOIN resources r
               ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
        WHERE a.status IN ('active', 'acknowledged')
    """)
    rows = cursor.fetchall()
    by_priority = {p: 0 for p in PRIORITIES}
    changes = []
    for row in rows:
        env = (row.get("env_resolved") if use_resolved_env else None) or row.get("environment")
        new = compute_priority(env, row.get("severity"), row.get("tags"))
        by_priority[new] += 1
        if row.get("priority") != new:
            changes.append((row["id"], row.get("priority"), new))
    if apply:
        for alert_id, _old, new in changes:
            cursor.execute("UPDATE alerts SET priority = %s WHERE id = %s", (new, alert_id))
    return {"checked": len(rows), "changed": len(changes), "by_priority": by_priority, "changes": changes}
