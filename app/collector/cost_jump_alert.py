# app/collector/cost_jump_alert.py
"""
Cost-jump alert: "CloudOps cost is more than 30% above its 7-day average".

WHAT IS COMPARED
  The latest SETTLED day of CloudOps's own billed cost for one cloud account (Cost Explorer for AWS, Cost Management
  for Azure, the BigQuery billing export for GCP: the same figures the Cloud cost page shows, CloudOps metric calls
  only, onboarded-region scope for AWS) against the average of the 7 days before it. Settled = at least
  PROVISIONAL_DAYS old: billing data lags real usage by about 48 h, so today and yesterday are never judged (they
  would look like a drop, then a jump, as they fill in). The alert is therefore about 2-3 days behind the spend; it
  catches a step change in polling cost, it is not a real-time meter.

  Threshold:  day_cost > average * (1 + threshold_pct / 100)                          (default 30)
  Noise floor: AND day_cost - average >= min_increase_usd                              (default 0.10)
  SETTINGS: on/off, threshold_pct and min_increase_usd are set on the Cloud cost page (usage.manage), for all accounts
  and per account (app/cost_jump_settings.py, table cost_jump_settings). Order: the account's own value, the
  all-accounts value, the server environment (COST_JUMP_ALERT_ENABLED, COST_JUMP_THRESHOLD_PCT,
  COST_JUMP_MIN_INCREASE_USD), the built-in default (off, 30, 0.10). Switching an account off resolves its open alert
  with reason 'alert_disabled'.
  Baseline:   needs at least MIN_BASELINE_DAYS of the 7 days present; fewer -> "no data", nothing is raised or cleared.

LIFECYCLE (same rules as host_agent_watchdog.py, through the existing alert pipeline)
  * One alert per account, metric 'cloud_cost_jump', severity WARNING, deduped on account + resource + metric, hung
    on a `resources` row of type 'cloud_cost' created here (the technique synthetic.py and the host-agent watchdog use),
    so correlation, priority, escalation, the status page, maintenance silences and notifications all work unchanged.
  * Back inside the limit on the latest settled day -> auto-resolved with reason 'recovered'.
  * Data missing or too short -> the alert is left alone and simply goes stale on its own (no fresh reading is the truth).
  * The check reads only the database (no cloud call, no cost). It runs at most every CHECK_EVERY_SECONDS from the
    scheduler's critical tick rather than once a day on a clock, because an open alert needs its last_seen_at refreshed
    inside the alert-staleness window; re-checking does not re-notify, a notification goes out only when an alert opens.

OFF BY DEFAULT: until someone turns it on (page or environment) no cost data is read and nothing is raised. The settings
table is read every check (one tiny query).
"""
import logging
import time
from datetime import date, timedelta

from app import cost_jump_settings
from app.db import get_connection

logger = logging.getLogger(__name__)

METRIC = "cloud_cost_jump"
RESOURCE_TYPE = "cloud_cost"
WINDOW_DAYS = 7
MIN_BASELINE_DAYS = 5
PROVISIONAL_DAYS = 2                    # keep equal to app/api/usage.py PROVISIONAL_DAYS
LOOKBACK_DAYS = WINDOW_DAYS + PROVISIONAL_DAYS + 2
CHECK_EVERY_SECONDS = 600               # well inside the 20-minute alert staleness window
DEFAULT_THRESHOLD_PCT = 30.0                 # = cost_jump_settings.BUILTIN, used only by evaluate()'s signature
DEFAULT_MIN_INCREASE_USD = 0.10
DETAIL_MAX_LEN = 500
PROVIDERS = ("aws", "azure", "gcp")
PROVIDER_NAMES = {"aws": "AWS", "azure": "Azure", "gcp": "GCP"}

_last_run = 0.0


# ── the rule (pure) ──────────────────────────────────────────────────────────────────

def evaluate(daily, today, pct=DEFAULT_THRESHOLD_PCT, min_increase=DEFAULT_MIN_INCREASE_USD):
    """daily: {date: usd}. -> {"state": "jump" | "ok" | "no_data", ...numbers}. Never raises on missing days."""
    day = today - timedelta(days=PROVISIONAL_DAYS)
    if day not in daily:
        return {"state": "no_data", "reason": "latest settled day not billed or not read yet", "day": day}
    base = [daily[day - timedelta(days=k)] for k in range(1, WINDOW_DAYS + 1) if (day - timedelta(days=k)) in daily]
    if len(base) < MIN_BASELINE_DAYS:
        return {"state": "no_data", "reason": "not enough history for a 7-day average", "day": day, "baseline_days": len(base)}
    avg = sum(base) / len(base)
    usd = float(daily[day])
    limit = avg * (1.0 + pct / 100.0)
    jump = usd > limit and (usd - avg) >= min_increase
    return {"state": "jump" if jump else "ok", "day": day, "usd": usd, "baseline_avg": avg, "baseline_days": len(base),
            "limit": limit, "increase_pct": (usd / avg - 1.0) * 100.0 if avg > 0 else None}


def build_detail(account_name, provider, r, pct):
    inc = r["increase_pct"]
    rise = "with no cost in the days before" if inc is None else f"{inc:.0f}% above"
    text = (f"CloudOps's {PROVIDER_NAMES.get(provider, provider)} cost for {account_name or 'this account'} was "
            f"${r['usd']:.2f} on {r['day'].isoformat()}, {rise} its {r['baseline_days']}-day average of "
            f"${r['baseline_avg']:.2f} (alert limit ${r['limit']:.2f}, +{pct:g}%). Billed cost of CloudOps's own metric "
            f"calls, tax not included. Likely causes: more resources discovered, a polling-tier or environment-polling "
            f"change, or more pages opened. See Cloud cost > Cost by polling tier.")
    return text[:DETAIL_MAX_LEN]


# ── data access ──────────────────────────────────────────────────────────────────────

def _load_daily(cursor, provider, start, end):
    """{account pk: {"name": str, "daily": {date: usd}}} of CloudOps-only billed cost, from the tables the Cost page reads."""
    out = {}

    def add(pk, name, d, amt):
        a = out.setdefault(pk, {"name": name, "daily": {}})
        a["daily"][d] = a["daily"].get(d, 0.0) + float(amt or 0)

    if provider == "aws":
        from app.aws.cost_explorer import usage_category, usage_region, CAT_CLOUDOPS
        cursor.execute("""
            SELECT a.id AS pk, a.account_name, a.default_region, c.usage_type, c.usage_date, c.amount_usd
            FROM aws_cost_actuals c
            JOIN aws_accounts a ON a.account_id = c.billed_account_id AND a.provider = 'aws' AND a.status = 'active'
            WHERE c.usage_date BETWEEN %s AND %s
        """, (start, end))
        for r in cursor.fetchall():
            if usage_category(r["usage_type"]) != CAT_CLOUDOPS:
                continue
            if r.get("default_region") and usage_region(r["usage_type"]) != r["default_region"]:
                continue                                  # same scope as the Cost page: only the region the account is onboarded in
            d = r["usage_date"]
            add(r["pk"], r["account_name"], d.date() if hasattr(d, "date") else d, r["amount_usd"])
        return out
    from app import cloud_cost
    key = "COALESCE(a.subscription_id, a.account_id)" if provider == "azure" else "COALESCE(a.project_id, a.account_id)"
    cursor.execute(f"""
        SELECT a.id AS pk, a.account_name, c.sku, c.usage_date, c.amount_usd
        FROM cloud_cost_actuals c
        JOIN aws_accounts a ON a.provider = c.provider AND a.status = 'active' AND {key} = c.billed_account
        WHERE c.provider = %s AND c.usage_date BETWEEN %s AND %s
    """, (provider, start, end))
    for r in cursor.fetchall():
        if cloud_cost.cloudops_category(provider, r["sku"]) != cloud_cost.CAT_CLOUDOPS:
            continue
        d = r["usage_date"]
        add(r["pk"], r["account_name"], d.date() if hasattr(d, "date") else d, r["amount_usd"])
    return out


def _rid(account_id):
    return f"cloud-cost-{account_id}"


def _ensure_target(cursor, account_id, name):
    rid = _rid(account_id)
    # last_seen_at deliberately omitted (NULL): alert_evaluator's "resource_gone" sweep only ages rows that HAVE one.
    cursor.execute("""
        INSERT INTO resources (aws_account_id, resource_type, resource_id, name, tags)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE name = VALUES(name)
    """, (account_id, RESOURCE_TYPE, rid, f"CloudOps cost: {name or account_id}", '{"environment": "prod"}'))
    return rid


def _open_alert(cursor, account_id, rid):
    cursor.execute("""
        SELECT id, status, silenced FROM alerts
        WHERE aws_account_id = %s AND resource_id = %s AND metric_name = %s AND status IN ('active', 'acknowledged')
        ORDER BY id LIMIT 1
    """, (account_id, rid, METRIC))
    return cursor.fetchone()


def _resolve(cursor, alert_id, reason):
    cursor.execute("""
        UPDATE alerts SET status = 'resolved', resolved_at = UTC_TIMESTAMP(), last_seen_at = UTC_TIMESTAMP(),
               resolution_reason = %s, resolved_by = 'system'
        WHERE id = %s AND status IN ('active', 'acknowledged')
    """, (reason, alert_id))
    return cursor.rowcount


def _notify(alert_id, rid, account_id, account_name, usd, limit, detail):
    """After commit; each step independent and never raises (same fan-out as the host-agent watchdog)."""
    try:
        from app.ws.publisher import publish_alert
        publish_alert(alert_id=alert_id, severity="WARNING", metric=METRIC, value=usd, threshold=limit,
                      account_id=account_id, account_name=account_name, region=None, priority=None)
    except Exception:
        logger.warning("[cost-jump] publish_alert failed", exc_info=True)
    try:
        from app.notifications.sender import notify_alert_event
        notify_alert_event("opened", {"id": alert_id, "severity": "WARNING", "metric_name": METRIC, "resource_id": rid,
                                      "account_name": account_name, "aws_account_id": account_id, "value": usd,
                                      "threshold": limit, "silenced": False, "priority": None, "detail": detail})
    except Exception:
        logger.warning("[cost-jump] notify_alert_event failed", exc_info=True)


# ── entry point ──────────────────────────────────────────────────────────────────────

def run_cost_jump_check(force=False, today=None) -> dict:
    """One pass over every active account of every cloud. Returns counters. Throttled to CHECK_EVERY_SECONDS."""
    global _last_run
    now = time.time()
    if not force and now - _last_run < CHECK_EVERY_SECONDS:
        return {"skipped": "throttled"}
    _last_run = now
    today = today or _utc_today()
    start, end = today - timedelta(days=LOOKBACK_DAYS), today
    stats = {"accounts": 0, "opened": 0, "updated": 0, "resolved": 0, "no_data": 0, "switched_off": 0}
    opened = []
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        settings = cost_jump_settings.load(cursor)
        # An account switched off (or everything switched off) must not keep an open alert behind.
        cursor.execute("SELECT id, aws_account_id FROM alerts WHERE metric_name = %s AND status IN ('active', 'acknowledged')",
                       (METRIC,))
        for a in cursor.fetchall():
            if not cost_jump_settings.effective(settings, a["aws_account_id"])["enabled"]:
                if _resolve(cursor, a["id"], "alert_disabled"):
                    stats["switched_off"] += 1
        anywhere = cost_jump_settings.effective(settings)["enabled"] or any(
            cost_jump_settings.effective(settings, pk)["enabled"] for pk in settings["accounts"])
        if not anywhere:
            conn.commit()
            return {**stats, "skipped": "disabled"}
        silenced_map = {}
        try:
            from app.collector.maintenance import active_silenced_map
            silenced_map = active_silenced_map(cursor)
        except Exception:
            logger.warning("[cost-jump] could not read maintenance windows", exc_info=True)
        for provider in PROVIDERS:
            try:
                accounts = _load_daily(cursor, provider, start, end)
            except Exception as e:
                if "doesn't exist" in str(e) or "1146" in str(e):
                    continue                              # that cloud's cost tables are not migrated yet
                raise
            for pk, acct in accounts.items():
                eff = cost_jump_settings.effective(settings, pk)
                if not eff["enabled"]:
                    continue
                stats["accounts"] += 1
                pct, floor = eff["threshold_pct"], eff["min_increase_usd"]
                r = evaluate(acct["daily"], today, pct, floor)
                rid = _rid(pk)
                existing = _open_alert(cursor, pk, rid)
                if r["state"] == "no_data":
                    stats["no_data"] += 1                 # leave any open alert alone: it ages to stale by itself
                elif r["state"] == "ok":
                    if existing and _resolve(cursor, existing["id"], "recovered"):
                        stats["resolved"] += 1
                else:
                    detail = build_detail(acct["name"], provider, r, pct)
                    if existing:
                        cursor.execute("UPDATE alerts SET current_value = %s, threshold = %s, last_seen_at = UTC_TIMESTAMP() "
                                       "WHERE id = %s", (r["usd"], r["limit"], existing["id"]))
                        stats["updated"] += 1
                    else:
                        _ensure_target(cursor, pk, acct["name"])
                        silence = silenced_map.get((pk, rid))
                        cursor.execute("""
                            INSERT INTO alerts
                                (aws_account_id, resource_id, metric_name, severity, environment, group_key, status,
                                 triggered_at, last_seen_at, cycle_at, healthy_streak, current_value, threshold,
                                 breach_value, breach_threshold, silenced, silenced_reason)
                            VALUES (%s, %s, %s, 'WARNING', 'prod', %s, 'active', UTC_TIMESTAMP(), UTC_TIMESTAMP(),
                                    UTC_TIMESTAMP(), 0, %s, %s, %s, %s, %s, %s)
                        """, (pk, rid, METRIC, f"{pk}:{RESOURCE_TYPE}:{METRIC}", r["usd"], r["limit"], r["usd"], r["limit"],
                              1 if silence else 0, silence))
                        stats["opened"] += 1
                        if not silence:
                            opened.append((cursor.lastrowid, rid, pk, acct["name"], r["usd"], r["limit"], detail))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
    for alert_id, rid, pk, name, usd, limit, detail in opened:
        _notify(alert_id, rid, pk, name or "", usd, limit, detail)
    if stats["opened"] or stats["resolved"] or stats["switched_off"]:
        logger.info(f"[cost-jump] {stats}")
    return stats


def _utc_today():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date()
