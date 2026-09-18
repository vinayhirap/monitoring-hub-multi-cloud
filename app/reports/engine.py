# app/reports/engine.py
"""
Reusable report engine: same code path serves WEEKLY/MONTHLY/QUARTERLY/
CUSTOM -- those are just (period_start, period_end) computed differently
by the API layer (see app/api/reports.py's _resolve_period). Adding a
new report type later means adding a period-resolution function and a
scope_type branch here, not a new engine.

Data sources (all already-existing tables -- no new collector needed):
  - aws_accounts / resources : account, cloud, region, resource identity
  - alerts                    : severity, status, start/created_at, metric+value
  - metrics                   : last-value snapshot for a quick current-state table
  - metric_history             : trend lines (avg/min/max per day in range)

Incident-timeline / RCA narrative (app/collector/correlate.py,
app/collector/rca.py) is intentionally NOT re-implemented here; a
follow-up can feed correlate.py's output into this same
`_gather_incident_timeline` seam once that data has a stable
account-independent lookup. For now, alerts ARE the incident record --
every alert row already carries severity/status/start-time/resource,
which covers this phase's report content requirement.
"""
import logging
from datetime import datetime, timezone

from fpdf import FPDF

from app.db import get_db_cursor

logger = logging.getLogger(__name__)

_UNICODE_REPLACEMENTS = {
    "\u2022": "-", "\u2014": "--", "\u2013": "-",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2026": "...",
}


def _safe(text) -> str:
    text = "" if text is None else str(text)
    for uni, ascii_equiv in _UNICODE_REPLACEMENTS.items():
        text = text.replace(uni, ascii_equiv)
    return text.encode("latin-1", errors="replace").decode("latin-1")


# ── Data gathering ────────────────────────────────────────────────────

def gather_report_data(scope_type: str, scope_id: str, account_id: int | None,
                        period_start: datetime, period_end: datetime) -> dict:
    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        account = None
        if account_id:
            cur.execute(
                "SELECT id, account_id, name, region_default FROM aws_accounts WHERE id=%s",
                (account_id,),
            )
            account = cur.fetchone()

        params = [period_start, period_end]
        where = ["a.created_at BETWEEN %s AND %s"]

        if scope_type == "RESOURCE":
            where.append("r.resource_id = %s")
            params.append(scope_id)
        elif scope_type == "INCIDENT":
            # This app's alert id doubles as the correlated-incident
            # anchor for report purposes; scope_id is that alert id.
            where.append("a.id = %s")
            params.append(scope_id)
        if account_id:
            where.append("r.aws_account_id = %s")
            params.append(account_id)

        sql = f"""
            SELECT a.id, a.metric_name, a.value, a.severity, a.status, a.created_at,
                   r.resource_type, r.resource_id, r.name AS resource_name
            FROM alerts a
            JOIN resources r ON r.id = a.resource_id
            WHERE {' AND '.join(where)}
            ORDER BY a.created_at ASC
        """
        cur.execute(sql, params)
        alerts = cur.fetchall()

        affected_resources = {}
        for a in alerts:
            affected_resources.setdefault(a["resource_id"], {
                "resource_id": a["resource_id"],
                "resource_type": a["resource_type"],
                "name": a["resource_name"],
            })

    severity_counts = {"CRITICAL": 0, "WARNING": 0, "OTHER": 0}
    for a in alerts:
        sev = (a.get("severity") or "OTHER").upper()
        severity_counts[sev if sev in ("CRITICAL", "WARNING") else "OTHER"] += 1

    open_count = sum(1 for a in alerts if (a.get("status") or "").lower() not in ("resolved", "closed"))

    return {
        "account": account,
        "alerts": alerts,
        "affected_resources": list(affected_resources.values()),
        "severity_counts": severity_counts,
        "open_count": open_count,
        "total_count": len(alerts),
    }


# ── PDF rendering ─────────────────────────────────────────────────────

def render_report_pdf(*, report_type: str, scope_type: str, scope_id: str,
                       scope_label: str, period_start: datetime, period_end: datetime,
                       data: dict, generated_by: str) -> bytes:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, _safe("CloudOps Monitoring Report"), ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, _safe(f"{report_type.title()} Report -- {scope_type.title()}: {scope_label or scope_id}"), ln=True)
    pdf.set_font("Helvetica", "I", 9)
    pdf.cell(0, 6, _safe(
        f"Period: {period_start:%Y-%m-%d %H:%M} to {period_end:%Y-%m-%d %H:%M} UTC   |   "
        f"Generated: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC   |   By: {generated_by}"
    ), ln=True)
    pdf.ln(4)

    account = data.get("account")
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, _safe("Account / Scope"), ln=True)
    pdf.set_font("Helvetica", "", 10)
    if account:
        pdf.multi_cell(0, 6, _safe(
            f"Account: {account['name']} ({account['account_id']})   "
            f"Default region: {account.get('region_default') or 'n/a'}"
        ))
    else:
        pdf.multi_cell(0, 6, _safe(f"Scope: {scope_type} = {scope_id}"))
    pdf.ln(2)

    sc = data["severity_counts"]
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, _safe("Incident Summary"), ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, _safe(
        f"Total events: {data['total_count']}   Critical: {sc['CRITICAL']}   "
        f"Warning: {sc['WARNING']}   Currently open: {data['open_count']}"
    ))
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, _safe("Affected Resources"), ln=True)
    pdf.set_font("Helvetica", "", 10)
    if data["affected_resources"]:
        for r in data["affected_resources"]:
            pdf.multi_cell(0, 6, _safe(f"- [{r['resource_type']}] {r['name'] or r['resource_id']} ({r['resource_id']})"))
    else:
        pdf.multi_cell(0, 6, _safe("No resources with events in this period."))
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, _safe("Incident Timeline / Alerts & Events"), ln=True)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(38, 6, _safe("Time (UTC)")); pdf.cell(22, 6, _safe("Severity"))
    pdf.cell(22, 6, _safe("Status")); pdf.cell(45, 6, _safe("Resource"))
    pdf.cell(35, 6, _safe("Metric")); pdf.cell(0, 6, _safe("Value"), ln=True)
    pdf.set_font("Helvetica", "", 9)
    for a in data["alerts"]:
        pdf.cell(38, 6, _safe(a["created_at"].strftime("%Y-%m-%d %H:%M")))
        pdf.cell(22, 6, _safe(a.get("severity") or "-"))
        pdf.cell(22, 6, _safe(a.get("status") or "-"))
        pdf.cell(45, 6, _safe((a.get("resource_name") or a["resource_id"])[:28]))
        pdf.cell(35, 6, _safe(a.get("metric_name") or "-"))
        pdf.cell(0, 6, _safe(a.get("value")), ln=True)
    if not data["alerts"]:
        pdf.multi_cell(0, 6, _safe("No alerts/events recorded in this period -- clean run."))
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, _safe("Resolution / Current Status"), ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, _safe(
        f"{data['total_count'] - data['open_count']} of {data['total_count']} events resolved as of report "
        f"generation. {data['open_count']} remain open and are being tracked in CloudOps."
    ))

    return bytes(pdf.output(dest="S"))
