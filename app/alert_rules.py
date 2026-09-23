# app/alert_rules.py
"""
THE single definition of what an alert's state is, and how alerts are
counted, everywhere in the app.

WHY THIS FILE EXISTS (2026-09-20 alerts audit)
-----------------------------------------------
Before this module, at least six places each had their own idea of "an
active alert":

  * Overview banner/account tiles  -> DISTINCT resources, status='active'
  * Alerts page tabs               -> rows; "Active" hid stale, "Critical" did not
  * Services page tile badges      -> browser-side resource-id SUBSTRING
                                      matching over EVERY account's alerts
  * EC2 row badge                  -> first alert found for that resource id
  * "Need attention"               -> resource_health rows (15-min lag)
  * public status page / SLO /
    escalation                     -> raw status='active', no staleness,
                                      no silencing, no muting, no account key

so no two numbers on screen could ever be expected to agree, and a fake or
stale alert leaked into paging (escalation), the public status page and SLO
budgets while still being "hidden" on the Alerts page.

Everything now derives from ONE row-level `state` and ONE rollup:

    state (derived, mutually exclusive, evaluated in this order)
      resolved      status = 'resolved'
      acknowledged  status = 'acknowledged'   (a human took ownership)
      suppressed    silenced = 1 (maintenance window) OR muted_until in future
      stale         active, but no fresh reading inside the cadence-aware
                    freshness window for this metric's collection tier
      firing        active and fresh  <-- the ONLY state that paints a
                    resource/account red or amber and the ONLY state that
                    counts toward "N critical / N warning"

    unit of counting: ALERT ROWS everywhere (Overview banner, account
    chips, Services tiles, Alerts tabs). "Resources affected" is exposed
    separately and is never presented under an "alerts" label.

This module is dependency-free on purpose (it takes a cursor, imports
nothing from app.api.*), so app/api/alerts.py, app/api/live_data.py,
app/collector/*, app/api/status_page.py etc. can all import it without
creating a circular import (see alert_visibility.py's docstring for the
last time that bit us).
"""
from app.alert_visibility import hidden_metrics_sql, HIDDEN_FROM_ALERTS_UI_METRICS  # noqa: F401  (re-exported)

# ── Collection cadence classes ───────────────────────────────────────
# Tier facts come from app/collector/scheduler.py + metrics/extended.py:
#   core     -> 2/5/15 min tiers (ec2, ebs, rds, lambda, elb, ecs) and every
#               non-AWS provider (unchanged behaviour)
#   extended -> AWS extended-tier services, collected hourly
#   slow     -> SLOW_EXTENDED_SERVICES, collected once every 24h, and S3's
#               storage metrics can additionally lag up to 48h at the source
# A 20-minute freshness rule (right for `core`) marked every extended/slow
# alert "stale" 20 minutes after each collection, permanently. Freshness is
# therefore judged per class.
CORE_AWS_RESOURCE_TYPES = ("ec2", "ebs", "rds", "lambda", "elb", "ecs")
SLOW_AWS_RESOURCE_TYPES = ("s3", "logs", "backup", "cloudfront", "wafv2")

# minutes without a fresh reading before an ACTIVE alert is shown as stale
STALE_MINUTES = {"core": 20, "extended": 180, "slow": 3000}
# how far back a `metrics` last-value row may be and still be evaluated
EVAL_WINDOW_MINUTES = {"core": 10, "extended": 75, "slow": 1560}
# how long an alert may sit in `stale` before it is auto-resolved with an
# auditable reason. Deliberately long: "no data" is NOT "healthy" (see
# db/migrations/008_revert_falsely_resolved_alerts.sql), but an alert that
# has had no data for days is not a live signal either.
HARD_EXPIRY_HOURS = {"core": 72, "extended": 72, "slow": 168}

# kept for backwards compatibility with code importing the old constant
STALE_AFTER_MINUTES = STALE_MINUTES["core"]

# Internal, non-threshold alert sources: never auto-resolved for "threshold
# disabled", they have their own lifecycle in their own module.
SYSTEM_METRICS = ("multivariate_anomaly", "synthetic_uptime")


def _in(values):
    return ", ".join(f"'{v}'" for v in values)


# ── Metric-level cadence (polling audit 2026-09-23) ─────────────────────
# The resource-type class above assumes every metric of a resource type is
# polled at the same cadence; it isn't (e.g. EBS ops at 15 min, SQS age at
# 5 min, Azure/GCP low/extended tiers at 15/60 min). When a metric column is
# supplied, these helpers first match the metric's REAL polling interval
# from app/collector/polling_model.py, falling back to the class.
_OVERRIDE_GROUPS = None


def _sql_str(v):
    return "'" + str(v).replace(chr(92), chr(92) * 2).replace("'", "''") + "'"


def _override_groups():
    """{interval_seconds: [(provider, resource_type, lower_metric), ...]} and
    prefix rules; built once per process from the polling model."""
    global _OVERRIDE_GROUPS
    if _OVERRIDE_GROUPS is None:
        from app.collector import polling_model as pm
        groups = {}
        for key, seconds in pm.metric_interval_overrides().items():
            groups.setdefault(seconds, []).append(key)
        _OVERRIDE_GROUPS = (groups, list(pm.PREFIX_INTERVAL_OVERRIDES))
    return _OVERRIDE_GROUPS


def _metric_case(metric, r, acc, value_for_interval, fallback_sql):
    groups, prefixes = _override_groups()
    parts = []
    for seconds in sorted(groups):
        tuples = ", ".join(
            f"({_sql_str(p)}, {_sql_str(t)}, {_sql_str(m)})" for p, t, m in sorted(groups[seconds]))
        parts.append(f"WHEN ({acc}.provider, {r}.resource_type, LOWER({metric})) IN ({tuples}) "
                     f"THEN {value_for_interval(seconds)} ")
    for provider, rtype, prefix, seconds in prefixes:
        like = prefix.replace("_", "\\_") + "%"
        parts.append(f"WHEN {acc}.provider = {_sql_str(provider)} AND {r}.resource_type = {_sql_str(rtype)} "
                     f"AND LOWER({metric}) LIKE {_sql_str(like)} THEN {value_for_interval(seconds)} ")
    return "CASE " + "".join(parts) + f"ELSE {fallback_sql} END"


def _interval_value(table):
    from app.collector import polling_model as pm
    src = getattr(pm, table)

    def f(seconds):
        return int(src.get(seconds, src[min(src, key=lambda k: abs(k - seconds))]))
    return f


def cadence_class_sql(r="r", acc="acc", metric=None):
    """SQL expression -> 'core' | 'extended' | 'slow' for an alert's resource
    (and, when `metric` is given, that metric's real polling cadence)."""
    if metric is not None:
        from app.collector import polling_model as pm
        return _metric_case(metric, r, acc,
                            lambda s: _sql_str(pm.cadence_for_interval(s)),
                            cadence_class_sql(r, acc))
    return (
        f"CASE "
        f"WHEN {acc}.provider = 'aws' AND {r}.resource_type IN ({_in(SLOW_AWS_RESOURCE_TYPES)}) THEN 'slow' "
        f"WHEN {acc}.provider = 'aws' AND {r}.resource_type NOT IN ({_in(CORE_AWS_RESOURCE_TYPES)}) THEN 'extended' "
        f"ELSE 'core' END"
    )


def _by_class_sql(mapping, r="r", acc="acc"):
    return (
        f"CASE {cadence_class_sql(r, acc)} "
        f"WHEN 'slow' THEN {int(mapping['slow'])} "
        f"WHEN 'extended' THEN {int(mapping['extended'])} "
        f"ELSE {int(mapping['core'])} END"
    )


def stale_minutes_sql(r="r", acc="acc", metric=None):
    if metric is not None:
        return _metric_case(metric, r, acc, _interval_value("STALE_MIN_BY_INTERVAL"),
                            _by_class_sql(STALE_MINUTES, r, acc))
    return _by_class_sql(STALE_MINUTES, r, acc)


def eval_window_sql(r="r", acc="acc", metric=None):
    if metric is not None:
        return _metric_case(metric, r, acc, _interval_value("EVAL_WINDOW_MIN_BY_INTERVAL"),
                            _by_class_sql(EVAL_WINDOW_MINUTES, r, acc))
    return _by_class_sql(EVAL_WINDOW_MINUTES, r, acc)


def hard_expiry_hours_sql(r="r", acc="acc"):
    return _by_class_sql(HARD_EXPIRY_HOURS, r, acc)


def is_stale_sql(a="a", r="r", acc="acc"):
    """True for an ACTIVE alert with no fresh reading in its class window.
    A NULL last_seen_at (legacy rows) falls back to triggered_at rather than
    being treated as forever-fresh, which is how they used to hide."""
    return (
        f"({a}.status = 'active' AND COALESCE({a}.last_seen_at, {a}.triggered_at) "
        f"< DATE_SUB(UTC_TIMESTAMP(), INTERVAL {stale_minutes_sql(r, acc, f'{a}.metric_name')} MINUTE))"
    )


def is_suppressed_sql(a="a"):
    return (
        f"({a}.silenced = 1 OR ({a}.muted_until IS NOT NULL "
        f"AND {a}.muted_until > UTC_TIMESTAMP()))"
    )


def state_sql(a="a", r="r", acc="acc"):
    """SQL CASE producing the derived, mutually-exclusive alert state."""
    return (
        f"CASE "
        f"WHEN {a}.status = 'resolved' THEN 'resolved' "
        f"WHEN {a}.status = 'acknowledged' THEN 'acknowledged' "
        f"WHEN {is_suppressed_sql(a)} THEN 'suppressed' "
        f"WHEN {is_stale_sql(a, r, acc)} THEN 'stale' "
        f"ELSE 'firing' END"
    )


def alert_base_from(a="a", r="r", acc="acc"):
    """FROM/JOIN clause shared by every reader. Account-scoped join (a
    resource_id is only unique WITHIN an account -- migrations 045-048) and
    active-account gate; hidden internal metrics excluded."""
    return (
        f"FROM alerts {a} "
        f"JOIN resources {r} ON {r}.resource_id = {a}.resource_id "
        f"AND {r}.aws_account_id = {a}.aws_account_id "
        f"JOIN aws_accounts {acc} ON {acc}.id = {a}.aws_account_id "
        f"AND {acc}.status = 'active'"
    )


def base_where(a="a"):
    return f"{a}.metric_name NOT IN ({hidden_metrics_sql()})"


def firing_where(a="a", r="r", acc="acc"):
    """WHERE fragment (no leading AND) matching exactly the FIRING state.
    Use this from any module that needs 'is this alert really live' --
    status page, SLO, escalation, reports."""
    return (
        f"({a}.status = 'active' AND NOT {is_suppressed_sql(a)} "
        f"AND NOT {is_stale_sql(a, r, acc)})"
    )


# ── Rollups ──────────────────────────────────────────────────────────

def _svc_key(resource_type, resource_id):
    # ELB rows are stored as resource_type 'elb' but Services tiles are keyed
    # alb/nlb; same helper live_resource_counts() uses.
    try:
        from app.threshold_defaults import normalize_service_key
        return normalize_service_key(resource_type, resource_id)
    except Exception:  # pragma: no cover - defensive
        return resource_type


def fetch_open_alert_rows(cursor, account_ids=None):
    """
    One row per OPEN (active or acknowledged) alert with its derived state.
    account_ids: None = unrestricted, otherwise an iterable of accounts the
    caller may see (empty -> no rows).
    """
    params = []
    scope = ""
    if account_ids is not None:
        account_ids = list(account_ids)
        if not account_ids:
            return []
        scope = f" AND a.aws_account_id IN ({', '.join(['%s'] * len(account_ids))})"
        params = account_ids
    cursor.execute(
        f"""
        SELECT a.id, a.aws_account_id AS account_id, a.resource_id, a.metric_name,
               r.resource_type, UPPER(a.severity) AS severity,
               {state_sql()} AS state
        {alert_base_from()}
        WHERE a.status IN ('active', 'acknowledged')
          AND {base_where()}{scope}
        """,
        params,
    )
    return cursor.fetchall()


def _blank():
    return {"critical": 0, "warning": 0, "info": 0, "stale": 0,
            "acknowledged": 0, "suppressed": 0, "resources": set(),
            "critical_resources": set(), "warning_resources": set()}


def _finish(b):
    out = {k: v for k, v in b.items() if not isinstance(v, set)}
    out["resources_affected"] = len(b["resources"])
    out["critical_resources"] = len(b["critical_resources"])
    out["warning_resources"] = len(b["warning_resources"])
    out["firing"] = b["critical"] + b["warning"] + b["info"]
    return out


def _add(bucket, row):
    state = row["state"]
    if state == "firing":
        sev = (row["severity"] or "").upper()
        key = {"CRITICAL": "critical", "WARNING": "warning"}.get(sev, "info")
        bucket[key] += 1
        bucket["resources"].add(row["resource_id"])
        if key == "critical":
            bucket["critical_resources"].add(row["resource_id"])
        elif key == "warning":
            bucket["warning_resources"].add(row["resource_id"])
    elif state in ("stale", "acknowledged", "suppressed"):
        bucket[state] += 1


def rollup(rows):
    """
    {"accounts": {acct_id: {..counts.., "services": {svc: {..counts..}}}},
     "resources": {(acct_id, resource_id): {worst, critical, warning, ...}}}
    Every screen reads from this so they cannot disagree.
    """
    accounts, services, resources = {}, {}, {}
    for row in rows:
        acct = row["account_id"]
        _add(accounts.setdefault(acct, _blank()), row)
        svc = _svc_key(row["resource_type"], row["resource_id"])
        _add(services.setdefault((acct, svc), _blank()), row)
        res = resources.setdefault((acct, row["resource_id"]), {
            "service": svc, "critical": 0, "warning": 0, "info": 0,
            "stale": 0, "acknowledged": 0, "suppressed": 0,
        })
        state, sev = row["state"], (row["severity"] or "").upper()
        if state == "firing":
            res[{"CRITICAL": "critical", "WARNING": "warning"}.get(sev, "info")] += 1
        elif state in ("stale", "acknowledged", "suppressed"):
            res[state] += 1

    out_accounts = {}
    for acct, b in accounts.items():
        d = _finish(b)
        d["services"] = {}
        out_accounts[acct] = d
    for (acct, svc), b in services.items():
        out_accounts[acct]["services"][svc] = _finish(b)

    out_resources = {}
    for key, r in resources.items():
        r["worst"] = ("CRITICAL" if r["critical"] else "WARNING" if r["warning"]
                      else "INFO" if r["info"] else None)
        r["total"] = r["critical"] + r["warning"] + r["info"]
        out_resources[key] = r
    return {"accounts": out_accounts, "resources": out_resources}
