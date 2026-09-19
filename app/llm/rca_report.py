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
import logging

from app.db import get_connection
from app.collector.rca import explain_alert
from app.llm.summarizer import generate_rca_narrative
from app.llm.aws_docs import get_references

logger = logging.getLogger(__name__)


def _gather_facts(alert_id: int) -> dict:
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT a.id, a.resource_id, a.metric_name, a.severity, a.status,
                   a.triggered_at, a.resolved_at, a.current_value, a.threshold,
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
        timeline.append({"time": str(alert["triggered_at"]), "event": f"Alert triggered: {alert['metric_name']} on {alert['resource_name'] or alert['resource_id']}"})
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
            "severity": alert["severity"],
            "status": alert["status"],
            "triggered_at": str(alert["triggered_at"]),
            "resolved_at": str(alert["resolved_at"]) if alert["resolved_at"] else None,
            "duration_minutes": duration_minutes,
            "current_value": alert["current_value"],
            "threshold": alert["threshold"],
            "threshold_delta_pct": threshold_delta_pct,
            "confidence": explanation.get("confidence"),
            "trend": explanation.get("trend"),
            "is_likely_flapping": explanation.get("is_likely_flapping"),
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


def _fallback_narrative(facts: dict) -> str:
    """Deterministic Executive Summary + Recommendations, used when the
    LLM is disabled or its call fails -- see module docstring. Slightly
    more detailed than a bare template_summary dump, but every added
    sentence below is assembled from a fact already present in `facts`
    (current_value/threshold/resource_type/account_name) -- nothing
    here is invented, it's just surfacing numbers this app already
    gathered but previously left out of the narrative."""
    summary_parts = [facts["template_summary"] or "No summary available."]

    if facts.get("threshold_delta_pct") is not None:
        direction = "above" if facts["threshold_delta_pct"] >= 0 else "below"
        summary_parts.append(
            f"The triggering value was {abs(facts['threshold_delta_pct'])}% {direction} "
            f"the configured threshold for {facts['metric_name']} on this {facts['resource_type']} resource."
        )

    lines = ["## Executive Summary", "", " ".join(summary_parts), "", "## Recommendations", ""]
    if facts.get("recent_deployment"):
        lines.append("- Review the deployment listed in the timeline above for a possible causal link.")
    if facts.get("is_likely_flapping"):
        lines.append("- Consider widening this metric's threshold -- this alert shows signs of flapping on normal variance.")
    if facts.get("related_alert_count"):
        lines.append("- This alert was part of a wider correlated incident -- review related alerts for a shared root cause.")
    if not facts.get("resolved_at"):
        lines.append("- This alert is still active -- prioritize resolution before drawing final conclusions.")
    if len(lines) == 6:  # no bullets were added above
        lines.append("- No specific recommendation could be derived automatically from the signals gathered for this alert.")
    lines.append("- See References below for AWS's own documentation on this metric and how to investigate it further.")
    return "\n".join(lines)


def generate_rca_report(alert_id: int) -> dict:
    """
    Returns None if the alert doesn't exist, otherwise:
        {"facts": {...}, "narrative_markdown": "## Executive Summary...",
         "narrative_source": "llm" | "template"}
    """
    facts = _gather_facts(alert_id)
    if facts is None:
        return None

    narrative = generate_rca_narrative(facts)
    if narrative:
        return {"facts": facts, "narrative_markdown": narrative, "narrative_source": "llm"}
    return {"facts": facts, "narrative_markdown": _fallback_narrative(facts), "narrative_source": "template"}


def render_markdown(report: dict) -> str:
    f = report["facts"]
    duration = f"{f['duration_minutes']} minutes" if f["duration_minutes"] is not None else "still active"
    lines = [
        f"# RCA Report: {f['metric_name']} on {f['resource_name'] or f['resource_id']}",
        "",
        f"- **Account:** {f['account_name']}",
        f"- **Resource:** {f['resource_name'] or f['resource_id']} ({f['resource_type']})",
        f"- **Severity:** {f['severity']}",
        f"- **Status:** {f['status']}",
        f"- **Triggered:** {f['triggered_at']}",
        f"- **Duration:** {duration}",
        f"- **Current Value / Threshold:** {f['current_value']} / {f['threshold']}",
        f"- **RCA confidence:** {f['confidence']}",
        "",
        report["narrative_markdown"],
        "",
        "## Timeline",
        "",
    ]
    for event in f["timeline"]:
        lines.append(f"- **{event['time']}** \u2014 {event['event']}")
    if f.get("references"):
        lines += ["", "## References", ""]
        for ref in f["references"]:
            lines.append(f"- [{ref['title']}]({ref['url']})")
    lines += [
        "",
        f"*Generated automatically ({report['narrative_source']} narrative) by AurionPro CloudOps -- verify before external distribution.*",
    ]
    return "\n".join(lines)
