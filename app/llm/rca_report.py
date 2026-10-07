# app/llm/rca_report.py
"""
Downloadable incident RCA (Root Cause Analysis) report generation
(originally shipped 2026-09-14 as "postmortem", renamed 2026-09-17 for
a more professional, client-facing name -- no behavior change). Works
for ANY alert (standalone or part of a multi-alert incident) -- reuses
app/collector/rca.py's explain_alert() for all of its signal-gathering
(deployment correlation, CloudTrail events, config changes, trend,
flapping, related alerts) rather than re-querying the database itself,
so a report's facts are always identical to what the Alerts page's own
RCA panel already shows for that alert.

STRUCTURE: the timeline, resource info, severity, and duration are
assembled DETERMINISTICALLY from real rows -- never touched by an LLM.
Only the "Executive Summary" and "Recommendations" sections are
optionally LLM-written (app/llm/summarizer.py's
generate_rca_narrative(), same strict fact-grounding contract as every
other LLM feature in this app). If the LLM is disabled or the call
fails, those two sections fall back to a plain bullet-point rendering
of the same facts -- a report is ALWAYS produced, with or without the
LLM configured.
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from app.db import get_connection
from app.collector.rca import explain_alert
from app.llm.summarizer import generate_rca_summary, is_enabled
from app.llm.aws_docs import get_references
from app.metric_labels import metric_label, format_metric_value, metric_unit_name

logger = logging.getLogger(__name__)

# AI/ML audit Phase 1 (2026-10-02): the LLM narrative is generated on a background thread
# and cached in `rca_narratives` (migration 076). A download request waits at most
# LLM_RCA_WAIT_SECONDS for it, then returns the deterministic template immediately
# (narrative_pending=True) while generation continues; the next download gets the cached
# LLM version. Before this, the request thread blocked for the full LLM call (60-90 s on
# the current hardware) and then usually timed out into the template anyway.
_DEFAULT_WAIT_SECONDS = 8
_FAILURE_COOLDOWN_SECONDS = 300   # after a failed generation, don't re-run it on every click

_inflight = {}        # (alert_id, facts_hash) -> threading.Event, set when that generation ends
_failed_until = {}    # (alert_id, facts_hash) -> monotonic deadline
_state_lock = threading.Lock()


def _limit_kind(cursor, alert):
    """'learned' when the limit comes from this resource's own history (dynamic / anomaly), 'configured' when a person typed it.
    Best effort: the report is still produced without it."""
    try:
        from app.threshold_effective import limit_kind_for
        return limit_kind_for(cursor, alert.get("aws_account_id"), alert.get("resource_type"), alert["metric_name"])
    except Exception:
        return None


def _gather_facts(alert_id: int) -> dict:
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT a.id, a.resource_id, a.metric_name, a.severity, a.status,
                   a.triggered_at, a.resolved_at, a.current_value, a.threshold,
                   a.region, a.environment, acc.default_region, a.acked_at, a.acked_by, a.aws_account_id,
                   r.name AS resource_name, r.resource_type, acc.account_name
            FROM alerts a
            JOIN resources r      ON r.resource_id = a.resource_id
                                   AND r.aws_account_id = a.aws_account_id
            JOIN aws_accounts acc ON acc.id = r.aws_account_id
            WHERE a.id = %s
        """, (alert_id,))
        alert = cursor.fetchone()
        if not alert:
            return None

        explanation = explain_alert(alert_id) or {}

        duration_minutes = None
        if alert["resolved_at"] and alert["triggered_at"]:
            duration_minutes = round((alert["resolved_at"] - alert["triggered_at"]).total_seconds() / 60, 1)

        timeline = []
        if explanation.get("recent_deployment"):
            d = explanation["recent_deployment"]
            timeline.append({"time": str(d["created_at"]), "event": f"Deployment: {d['message']}"})
        for ce in (explanation.get("probable_trigger") and [explanation["probable_trigger"]] or []):
            timeline.append({
                "time": str(ce["event_time"]),
                "event": f"AWS activity: {ce['event_name']} by {ce['username'] or 'unknown'}",
            })
        timeline.append({"time": str(alert["triggered_at"]),
                         "event": f"Alert opened: {metric_label(alert['metric_name'])} on "
                                  f"{alert['resource_name'] or alert['resource_id']}"})
        if alert.get("acked_at"):
            who = alert.get("acked_by")
            timeline.append({"time": str(alert["acked_at"]), "event": f"Acknowledged{f' by {who}' if who else ''}"})
        if alert["resolved_at"]:
            timeline.append({"time": str(alert["resolved_at"]), "event": "Alert resolved"})
        timeline.sort(key=lambda e: e["time"])

        # Genuine, deterministic (never LLM-touched) -- both values were
        # already gathered above and shown in the Alerts table's own
        # Value/Threshold column, just never surfaced in the report
        # itself until now. Guards div-by-zero for a threshold of 0.
        threshold_delta_pct = None
        if alert["current_value"] is not None and alert["threshold"] not in (None, 0):
            threshold_delta_pct = round(
                (alert["current_value"] - alert["threshold"]) / abs(alert["threshold"]) * 100, 1
            )

        return {
            "alert_id": alert["id"],
            "resource_id": alert["resource_id"],
            "resource_name": alert["resource_name"],
            "resource_type": alert["resource_type"],
            "account_name": alert["account_name"],
            "metric_name": alert["metric_name"],
            "metric_label": metric_label(alert["metric_name"]),
            "metric_unit": metric_unit_name(alert["metric_name"]),
            "limit_kind": _limit_kind(cursor, alert),
            "region": alert.get("region") or alert.get("default_region"),
            "environment": alert.get("environment"),
            "severity": alert["severity"],
            "status": alert["status"],
            "triggered_at": str(alert["triggered_at"]),
            "resolved_at": str(alert["resolved_at"]) if alert["resolved_at"] else None,
            "duration_minutes": duration_minutes,
            "current_value": alert["current_value"],
            "threshold": alert["threshold"],
            "threshold_delta_pct": threshold_delta_pct,
            "confidence": explanation.get("confidence"),
            # NOT open_minutes: that changes every minute and would change the facts hash, i.e. throw away the
            # cached AI narrative on every download. "Open for ..." is computed at render time instead.
            "confidence_reason": explanation.get("confidence_reason"),
            "persistence": explanation.get("persistence"),
            "recurrences_30d": explanation.get("recurrences_30d"),
            "dependents": explanation.get("dependents") or [],
            "dependent_count": explanation.get("dependent_count"),
            "trend": explanation.get("trend"),
            "is_likely_flapping": explanation.get("is_likely_flapping"),
            "capacity_forecast": explanation.get("capacity_forecast"),
            "probable_trigger": explanation.get("probable_trigger"),
            "recent_deployment": explanation.get("recent_deployment"),
            "related_alert_count": explanation.get("related_alert_count"),
            "template_summary": explanation.get("template_summary"),
            "timeline": timeline,
            "references": get_references(alert["resource_type"], alert["metric_name"]),
        }
    finally:
        cursor.close()
        conn.close()


# ── presentation helpers (audit of the first real PDF: raw keys, unformatted numbers, no zone, no context) ────────

_RESOURCE_TYPE_LABELS = {
    "ec2": "EC2 instance", "ebs": "EBS volume", "rds": "RDS database", "s3": "S3 bucket", "lambda": "Lambda function",
    "elb": "Load balancer", "alb": "Application Load Balancer", "nlb": "Network Load Balancer",
    "natgateway": "NAT gateway", "dynamodb": "DynamoDB table", "sqs": "SQS queue", "sns": "SNS topic",
    "ecs": "ECS service", "eks": "EKS cluster", "elasticache": "ElastiCache cluster", "cloudfront": "CloudFront distribution",
    "apigateway": "API Gateway", "kms": "KMS key", "efs": "EFS file system", "redshift": "Redshift cluster",
    "backup": "Backup vault", "events": "EventBridge bus", "logs": "CloudWatch log group",
}


def _type_label(resource_type) -> str:
    t = str(resource_type or "").lower()
    return _RESOURCE_TYPE_LABELS.get(t) or (t.upper() if t else "resource")


def _fmt_utc(value) -> str:
    """'2026-10-04 16:13:45' -> '04 Oct 2026, 16:13:45 UTC' (same shape as the app's own timestamps)."""
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).strftime("%d %b %Y, %H:%M:%S") + " UTC"
        except ValueError:
            continue
    return text or "-"


def _minutes_text(minutes) -> str:
    try:
        m = int(round(float(minutes)))
    except (TypeError, ValueError):
        return "-"
    if m < 1:
        return "less than a minute"
    if m < 60:
        return f"{m} minute{'s' if m != 1 else ''}"
    if m < 1440:
        h, r = divmod(m, 60)
        return f"{h} hour{'s' if h != 1 else ''}" + (f" {r} min" if r else "")
    d, r = divmod(m, 1440)
    h = r // 60
    return f"{d} day{'s' if d != 1 else ''}" + (f" {h} hr" if h else "")


def _open_minutes(facts: dict, now=None):
    """Minutes the alert has been (or was) open. Active alerts use the current time, so this is render-time only."""
    if facts.get("duration_minutes") is not None:
        return facts["duration_minutes"]
    text = str(facts.get("triggered_at") or "")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            started = datetime.strptime(text, fmt)
            break
        except ValueError:
            started = None
    if started is None:
        return None
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return max(0, int((now - started).total_seconds() // 60))


def _is_active(facts: dict) -> bool:
    return not facts.get("resolved_at") and str(facts.get("status") or "").lower() != "resolved"


def _reading(facts: dict, key: str) -> str:
    return format_metric_value(facts.get("metric_name"), facts.get(key), grouped=True)


def _over_text(facts: dict):
    d = facts.get("threshold_delta_pct")
    if d is None:
        return None
    return f"{abs(d):g}% {'over' if d >= 0 else 'under'}"


_FORECAST_HORIZON_DAYS = 90     # same horizon as app/collector/rca.py: beyond it, say "slow", not a date


def _forecast_text(cf: dict) -> str:
    days = cf["days_to_exhaustion"]
    if days > _FORECAST_HORIZON_DAYS:
        if cf.get("counts_up", True):
            return (f"Usage is growing only slowly (about {abs(cf['slope_per_day']):.2f} percentage points per day), "
                    f"so it is not projected to fill within the next {_FORECAST_HORIZON_DAYS} days.")
        return (f"Free space is shrinking only slowly, so it is not projected to run out within the "
                f"next {_FORECAST_HORIZON_DAYS} days.")
    when = "under a day" if days < 1 else (f"about {days:.1f} days" if days < 10 else f"about {round(days)} days")
    if cf.get("counts_up", True):
        return (f"At the recent growth rate (about {abs(cf['slope_per_day']):.1f} percentage points per day), "
                f"usage is projected to reach 100% in {when}.")
    return f"At the recent rate of decline, free space is projected to run out in {when}."


def _lead(label: str, text: str) -> str:
    return f"**{label}.** {text}"


# Suggested first checks by metric family. Deliberately generic best practice, worded as suggestions: nothing here claims
# to know the cause. Matched on the lower-cased stored metric name; first match wins.
_GUIDANCE = (
    (("volumeread", "volumewrite", "volumequeue", "volumeidle", "volumethroughput", "burstbalance", "volumeconsumed"),
     "Suggested first checks: find which instance and process drives this volume's I/O, compare it with the volume's "
     "provisioned IOPS and throughput, and look for a backup, batch job or index rebuild running at that time."),
    (("disk_used_percent", "mem_used_percent", "freestoragespace", "diskspace", "freeablememory", "percentagediskspaceused"),
     "Suggested first checks: identify what is consuming the space or memory (logs, temp files, database growth, a leaking "
     "process), then clean up, restart the leaking service or extend capacity."),
    (("cpuutilization", "cpucredit", "cpu_"),
     "Suggested first checks: find which process or workload is driving CPU, compare it with recent load and deployments, "
     "and check whether the instance size still fits the workload."),
    (("networkin", "networkout", "networkpackets", "bytesin", "bytesout", "bytesinfromsource", "bytesouttodestination"),
     "Suggested first checks: find which workload or client is generating the traffic and whether it matches a known "
     "transfer, backup or release window."),
    (("healthyhost", "unhealthyhost", "healthcheck"),
     "Suggested first checks: look at the failing targets' health-check results and application logs, and at any recent "
     "deployment to those targets."),
    (("5xx", "errors5xx", "httpcode_target_5xx", "httpcode_elb_5xx", "faultrequest"),
     "Suggested first checks: review application logs for the failing requests and any deployment shortly before the "
     "errors began."),
    (("throttle", "concurrentexecutions", "errors", "failed", "timedout", "deadletter"),
     "Suggested first checks: look at the service's recent error logs, its concurrency or capacity limits, and the "
     "downstream dependencies it calls."),
    (("databaseconnections", "connections"),
     "Suggested first checks: look for connection leaks and long-running queries, and compare against the connection "
     "limit for this instance size."),
    (("statuscheckfailed",),
     "Suggested first checks: open the instance's status checks in the console; a failed system check usually means an "
     "impaired host, which a stop and start (not a reboot) normally clears."),
    (("daystoexpiry",),
     "Suggested first checks: renew or replace the certificate before it expires, and confirm automatic renewal is "
     "configured."),
)


def _metric_guidance(metric_name):
    name = str(metric_name or "").lower()
    for needles, text in _GUIDANCE:
        if any(n in name for n in needles):
            return text
    return None


def _build_summary_paragraphs(facts: dict) -> list:
    label = facts.get("metric_label") or metric_label(facts.get("metric_name"))
    rid = facts.get("resource_id")
    name = facts.get("resource_name") or rid
    where = f"{name} ({_type_label(facts.get('resource_type'))})" if name == rid else \
        f"{name} ({_type_label(facts.get('resource_type'))}, {rid})"
    account = facts.get("account_name")
    d = facts.get("threshold_delta_pct")
    verb = "crossed" if d is None else ("went above" if d >= 0 else "fell below")
    limit_word = "learned limit" if facts.get("limit_kind") == "learned" else "alert limit"
    over = _over_text(facts)
    head = (f"{label} on {where}" + (f" in {account}" if account else "") +
            f" {verb} its {limit_word} at {_fmt_utc(facts.get('triggered_at'))}: the reading was {_reading(facts, 'current_value')} "
            f"against a limit of {_reading(facts, 'threshold')}" + (f" ({over})" if over else "") + ".")
    if _is_active(facts):
        om = _open_minutes(facts)
        head += f" The alert is still active and has been open for {_minutes_text(om)}." if om is not None else \
            " The alert is still active."
    else:
        head += f" It resolved after {_minutes_text(facts.get('duration_minutes'))}."
    paras = [_lead("What happened", head)]

    pattern = []
    if facts.get("persistence"):
        pattern.append(facts["persistence"])
    elif facts.get("recurrences_30d") and facts["recurrences_30d"] >= 3:
        pattern.append(f"This alert has triggered {facts['recurrences_30d']} other times in the last 30 days.")
    trend = (facts.get("trend") or {}).get("description") if isinstance(facts.get("trend"), dict) else None
    if trend:
        pattern.append(trend)
    if facts.get("is_likely_flapping"):
        pattern.append("The metric's normal variability occasionally crosses the limit, so this looks more like noise "
                       "than a genuine incident.")
    cf = facts.get("capacity_forecast")
    if cf and cf.get("days_to_exhaustion") is not None:
        pattern.append(_forecast_text(cf))
    if pattern:
        paras.append(_lead("Pattern", " ".join(pattern)))

    impact = []
    n_dep = facts.get("dependent_count") or len(facts.get("dependents") or [])
    if n_dep:
        names = facts.get("dependents") or []
        shown = ", ".join(names) + (f" and {n_dep - len(names)} more" if n_dep > len(names) and names else "")
        impact.append(f"{n_dep} other {'resource relies' if n_dep == 1 else 'resources rely'} on this one"
                      + (f" ({shown})" if shown else "") + ", so the impact may be wider than this single alert.")
    n_rel = facts.get("related_alert_count") or 0
    if n_rel:
        impact.append(f"It is part of a wider incident with {n_rel} related {'alert' if n_rel == 1 else 'alerts'}.")
    if impact:
        paras.append(_lead("Impact", " ".join(impact)))

    dep = facts.get("recent_deployment")
    trig = facts.get("probable_trigger")
    if dep:
        paras.append(_lead("Probable cause",
                           f"A deployment shortly before the alert is the most likely trigger, though not confirmed: "
                           f"{dep.get('message')}."))
    elif trig:
        paras.append(_lead("Probable cause",
                           f"A change on AWS shortly before the alert is the most likely trigger, though not confirmed: "
                           f"{trig.get('event_name')} by {trig.get('username') or 'an unknown user'}."))
    else:
        paras.append(_lead("Probable cause",
                           "No deployment or AWS change was found around when it started, so the cause cannot be "
                           "determined from the signals available."))
    return paras


def _build_recommendations(facts: dict) -> list:
    recs = []
    if facts.get("recent_deployment"):
        recs.append("Review the deployment listed in the timeline above for a possible causal link.")
    cf = facts.get("capacity_forecast")
    if cf and cf.get("days_to_exhaustion") is not None and cf["days_to_exhaustion"] <= 30:
        days = cf["days_to_exhaustion"]
        when = "under a day" if days < 1 else (f"about {days:.1f} days" if days < 10 else f"about {round(days)} days")
        recs.append(f"Capacity: at the recent rate this resource reaches its limit in {when} - clean up or extend "
                    f"storage before then.")
    om = _open_minutes(facts) if _is_active(facts) else None
    if om is not None and om >= 1440:
        recs.append(f"Open for {_minutes_text(om)} with no sign of clearing: decide whether this level is the new normal "
                    f"(then adjust the limit under Settings > Metric thresholds) or an unresolved fault.")
    rec30 = facts.get("recurrences_30d") or 0
    if rec30 >= 10 and facts.get("limit_kind") == "learned":
        recs.append(f"This alert has fired {rec30} other times in 30 days against a limit learned from this resource's own "
                    f"history. If this level is now normal for the workload, mark the repeats as not genuine so the "
                    f"baseline absorbs it; if it is not normal, find what keeps driving it.")
    elif rec30 >= 10:
        recs.append(f"This alert has fired {rec30} other times in 30 days. If this level is normal for the workload, "
                    f"raise the limit or mark the repeats as not genuine so auto-tuning can learn it; if it is not "
                    f"normal, find what keeps driving it.")
    if facts.get("is_likely_flapping"):
        recs.append("Consider widening this metric's threshold - this alert shows signs of flapping on normal variance.")
    names = facts.get("dependents") or []
    n_dep = facts.get("dependent_count") or len(names)
    if n_dep and names:
        recs.append(f"Check the dependent {'resource' if n_dep == 1 else 'resources'} for impact: {', '.join(names)}"
                    + (f" and {n_dep - len(names)} more." if n_dep > len(names) else "."))
    if facts.get("related_alert_count"):
        recs.append("This alert was part of a wider correlated incident - review related alerts for a shared root cause.")
    guidance = _metric_guidance(facts.get("metric_name"))
    if guidance:
        recs.append(guidance)
    if not recs:
        recs.append("No specific recommendation could be derived automatically from the signals gathered for this alert.")
    return recs


# What the AI may write is ONLY a short "In brief" lead paragraph (or, with LLM_RCA_SUMMARY_MODE=replace, the whole
# summary section); the labelled paragraphs, every figure, the recommendations and the timeline are always rule-based.
# 2026-10-05: the previous design let the model write Recommendations too; the stored 3B output for alert 7885 advised
# reviewing a deployment history when none existed and adjusting the threshold of a full disk.
def _summary_mode() -> str:
    return "replace" if os.getenv("LLM_RCA_SUMMARY_MODE", "lead").strip().lower() == "replace" else "lead"


def _compose_narrative(facts: dict, ai_summary: str = None) -> str:
    paras = _build_summary_paragraphs(facts)
    if not facts.get("metric_name") and facts.get("template_summary"):
        paras = [facts["template_summary"]]                          # only reachable with stripped-down facts
    if ai_summary:
        paras = [ai_summary] if _summary_mode() == "replace" else [_lead("In brief", ai_summary)] + paras
    lines = ["## Executive Summary", ""]
    for p in paras:
        lines += [p, ""]
    lines += ["## Recommendations", ""]
    lines += [f"- {r}" for r in _build_recommendations(facts)]
    return "\n".join(lines)


def _fallback_narrative(facts: dict) -> str:
    """Deterministic Executive Summary + Recommendations, used when the AI summary is disabled, pending or rejected.
    Every sentence is assembled from a fact already in `facts`; nothing is invented. Structured as short labelled
    paragraphs (What happened / Pattern / Impact / Probable cause) so it can be scanned, not read as one block."""
    return _compose_narrative(facts, None)


def _draft_for_llm(facts: dict) -> str:
    """The rule-based summary as plain text (lead-ins turned into 'Label: text'), the only thing the model sees."""
    out = []
    for p in _build_summary_paragraphs(facts):
        m = re.match(r"^\*\*(.+?)\.\*\*\s+(.*)$", p, re.S)
        out.append(f"{m.group(1)}: {m.group(2)}" if m else p)
    return "\n".join(out)


# Bumping this orphans every cached row on purpose: rows written before 2026-10-05 hold a FULL AI narrative (summary AND
# recommendations); rows written now hold only the AI summary paragraph. Same table, different meaning.
_CACHE_VERSION = "summary-v1"


def _facts_hash(facts: dict) -> str:
    payload = _CACHE_VERSION + json.dumps(facts, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_cache(alert_id: int, facts_hash: str):
    """Cached LLM narrative for exactly these facts, or None. Never raises: a missing
    table (migration 076 not applied yet) or any DB hiccup just means 'no cache'."""
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT narrative_markdown FROM rca_narratives WHERE alert_id = %s AND facts_hash = %s",
                (alert_id, facts_hash),
            )
            row = cursor.fetchone()
            conn.commit()
            return row["narrative_markdown"] if row else None
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        logger.warning(f"[rca_report] narrative cache read skipped: {e}")
        return None


def _write_cache(alert_id: int, facts_hash: str, narrative: str) -> None:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO rca_narratives (alert_id, facts_hash, narrative_markdown)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE facts_hash = VALUES(facts_hash),
                                        narrative_markdown = VALUES(narrative_markdown),
                                        generated_at = CURRENT_TIMESTAMP
                """,
                (alert_id, facts_hash, narrative),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        logger.warning(f"[rca_report] narrative cache write skipped: {e}")


def _generate_in_background(alert_id: int, facts: dict, facts_hash: str, done: threading.Event):
    key = (alert_id, facts_hash)
    try:
        summary = generate_rca_summary(facts, _draft_for_llm(facts))
        if summary:
            _write_cache(alert_id, facts_hash, summary)
        else:
            with _state_lock:
                _failed_until[key] = time.monotonic() + _FAILURE_COOLDOWN_SECONDS
    except Exception:
        logger.exception(f"[rca_report] background narrative generation failed for alert {alert_id}")
        with _state_lock:
            _failed_until[key] = time.monotonic() + _FAILURE_COOLDOWN_SECONDS
    finally:
        with _state_lock:
            _inflight.pop(key, None)
        done.set()


def _start_or_join(alert_id: int, facts: dict, facts_hash: str):
    """Returns the Event for this generation (starting it if nobody has), or None when this
    exact generation recently failed and is cooling down."""
    key = (alert_id, facts_hash)
    with _state_lock:
        if _failed_until.get(key, 0) > time.monotonic():
            return None
        event = _inflight.get(key)
        if event is None:
            event = threading.Event()
            _inflight[key] = event
            threading.Thread(
                target=_generate_in_background, args=(alert_id, facts, facts_hash, event),
                name=f"rca-narrative-{alert_id}", daemon=True,
            ).start()
        return event


def generate_rca_report(alert_id: int) -> dict:
    """
    Returns None if the alert doesn't exist, otherwise:
        {"facts": {...}, "narrative_markdown": "## Executive Summary...",
         "narrative_source": "llm" | "template", "narrative_pending": bool}
    narrative_pending is True when an LLM narrative is still being generated in the
    background (the template is returned now; download again shortly for the LLM version).
    """
    facts = _gather_facts(alert_id)
    if facts is None:
        return None

    if not is_enabled() or os.getenv("LLM_RCA_SUMMARY_ENABLED", "true").strip().lower() == "false":
        return {"facts": facts, "narrative_markdown": _fallback_narrative(facts),
                "narrative_source": "template", "narrative_pending": False}

    facts_hash = _facts_hash(facts)
    cached = _read_cache(alert_id, facts_hash)
    if cached:
        return {"facts": facts, "narrative_markdown": _compose_narrative(facts, cached),
                "narrative_source": "llm", "narrative_pending": False}

    event = _start_or_join(alert_id, facts, facts_hash)
    if event is not None:
        wait = float(os.getenv("LLM_RCA_WAIT_SECONDS", _DEFAULT_WAIT_SECONDS))
        if event.wait(timeout=wait):
            cached = _read_cache(alert_id, facts_hash)
            if cached:
                return {"facts": facts, "narrative_markdown": _compose_narrative(facts, cached),
                        "narrative_source": "llm", "narrative_pending": False}
            # finished but produced nothing usable (timeout, verifier rejection, ...)
            return {"facts": facts, "narrative_markdown": _fallback_narrative(facts),
                    "narrative_source": "template", "narrative_pending": False}
        return {"facts": facts, "narrative_markdown": _fallback_narrative(facts),
                "narrative_source": "template", "narrative_pending": True}
    return {"facts": facts, "narrative_markdown": _fallback_narrative(facts),
            "narrative_source": "template", "narrative_pending": False}


def report_title(report: dict) -> str:
    """Short, human title: 'Volume Read Operations above its limit'."""
    f = report["facts"]
    label = f.get("metric_label") or metric_label(f.get("metric_name"))
    d = f.get("threshold_delta_pct")
    tail = "alert" if d is None else ("above its limit" if d >= 0 else "below its limit")
    return f"{label} {tail}"


def _reading_note(f: dict) -> str:
    """'Network In (Bytes)': the metric name with its unit, so a bare 2.48M is never left to guesswork."""
    label = f.get("metric_label") or metric_label(f.get("metric_name"))
    unit = f.get("metric_unit") or ""
    return f"{label} ({unit})" if unit and unit.lower() not in ("percent", "none") else label


def report_kpis(report: dict) -> list:
    """Four headline figures for the PDF's key-figures strip: [{label, value, note, tone}]."""
    f = report["facts"]
    d = f.get("threshold_delta_pct")
    active = _is_active(f)
    om = _open_minutes(f)
    return [
        {"label": "READING", "value": _reading(f, "current_value"), "note": _reading_note(f), "tone": "ink"},
        {"label": "LEARNED LIMIT" if f.get("limit_kind") == "learned" else "ALERT LIMIT", "value": _reading(f, "threshold"),
         "note": "learned for this resource" if f.get("limit_kind") == "learned"
                 else ("configured limit" if f.get("limit_kind") == "configured" else "limit that was crossed"), "tone": "ink"},
        {"label": "OVER LIMIT BY" if (d is None or d >= 0) else "UNDER LIMIT BY",
         "value": "n/a" if d is None else f"{abs(d):g}%",
         "note": "above the limit" if (d is None or d >= 0) else "below the limit", "tone": "severity"},
        {"label": "OPEN FOR" if active else "DURATION",
         "value": _minutes_text(om) if om is not None else "n/a",
         "note": "still active" if active else "resolved", "tone": "ink"},
    ]


def render_markdown(report: dict) -> str:
    f = report["facts"]
    label = f.get("metric_label") or metric_label(f.get("metric_name"))
    rid = f["resource_id"]
    name = f.get("resource_name") or rid
    active = _is_active(f)
    om = _open_minutes(f)
    status = (f"Active, open for {_minutes_text(om)}" if om is not None else "Active") if active \
        else f"Resolved after {_minutes_text(f.get('duration_minutes'))}"
    over = _over_text(f)
    rows = [
        ("Alert", f"#{f['alert_id']}"),
        ("Account", f.get("account_name")),
        ("Resource", f"{name} ({_type_label(f.get('resource_type'))})"),
    ]
    if name != rid:
        rows.append(("Resource ID", rid))
    if f.get("region"):
        rows.append(("Region", f["region"]))
    env = str(f.get("environment") or "")
    if env and env.lower() not in ("unknown", "none"):
        rows.append(("Environment", env.upper() if len(env) <= 4 else env.title()))
    rows += [
        ("Severity", str(f.get("severity") or "").title()),
        ("Status", status),
        ("Triggered", _fmt_utc(f.get("triggered_at"))),
        ("Reading vs limit", f"{_reading(f, 'current_value')} against a "
                             f"{'learned limit' if f.get('limit_kind') == 'learned' else 'limit'} of {_reading(f, 'threshold')}"
                             + (f" ({over})" if over else "")),
    ]
    conf = str(f.get("confidence") or "").title()
    reason = f.get("confidence_reason")
    rows.append(("RCA confidence", f"{conf}. {reason}" if conf and reason else (conf or "-")))
    rows.append(("Report generated", datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M:%S") + " UTC"))

    lines = [f"# RCA Report: {label} on {name}", ""]
    lines += [f"- **{k}:** {v}" for k, v in rows]
    lines.append("")
    if report.get("narrative_pending"):
        # Plain text on purpose: the PDF renderer prints any line it does not recognise literally, so markdown
        # emphasis here showed up as stray asterisks (2026-10-03).
        lines += ["Note: an AI-written summary is being generated for this alert. This copy shows the standard "
                  "rule-based summary; download the report again in a couple of minutes for the AI-written version.", ""]
    lines += [
        report["narrative_markdown"],
        "",
        "## Timeline",
        "",
    ]
    for event in f["timeline"]:
        lines.append(f"- **{_fmt_utc(event['time'])}** \u2014 {event['event']}")
    if f.get("references"):
        lines += ["", "## References", ""]
        for ref in f["references"]:
            lines.append(f"- [{ref['title']}]({ref['url']})")
    source = "AI-written" if report.get("narrative_source") == "llm" else "rule-based"
    lines += [
        "",
        f"*Generated automatically by AurionPro CloudOps ({source} summary). Verify before external distribution.*",
    ]
    return "\n".join(lines)
