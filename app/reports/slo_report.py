# app/reports/slo_report.py
"""
SLO and error-budget report for one account (or one SLO): target, uptime, how much of the error budget is used, and
whether each SLO is met, at risk or breached.

scope_type SLO, scope_id "<account id>" or "<account id>:<slo id>". Needs slo.view; account-scoped like the SLO page.

The figures are computed by the SLO page's own function (app/api/slo.py::_compute_status), so the PDF cannot disagree
with the page. That makes this a report of the state AT GENERATION TIME: every SLO is measured over its OWN rolling window
(its window_days, ending now), not over the report period. The cover says "as of", and the period chosen in the Reports
page does not change any figure.
"""
import logging
from datetime import datetime, timezone

from app import pdf_kit as kit
from app.db import get_db_cursor
from app.reports import common as c

logger = logging.getLogger(__name__)

STATUS_ORDER = ("breached", "warning", "ok", "no_data")
STATUS_LABEL = {"breached": "BREACHED", "warning": "AT RISK", "ok": "MET", "no_data": "NO DATA"}
STATUS_COLOR = {"breached": kit.SEVERITY_COLORS["CRITICAL"], "warning": kit.SEVERITY_COLORS["WARNING"],
                "ok": kit.STATUS_COLORS["RESOLVED"], "no_data": kit.MUTED}


def parse_scope(scope_id):
    from app.reports.common import parse_account_scope
    return parse_account_scope(scope_id)


def gather_slo_data(account_id, slo_id, period_start, period_end, _compute=None):
    if _compute is None:
        from app.api.slo import _compute_status as _compute
    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        cur.execute("SELECT id, account_id, account_name, provider FROM aws_accounts WHERE id = %s", (account_id,))
        account = cur.fetchone()
        sql = """SELECT s.*, sc.name AS synthetic_check_name,
                        (SELECT r.name FROM resources r WHERE r.resource_id = s.resource_id
                           AND r.aws_account_id = s.aws_account_id LIMIT 1) AS resource_name
                 FROM slo_definitions s
                 LEFT JOIN synthetic_checks sc ON sc.id = s.synthetic_check_id AND sc.aws_account_id = s.aws_account_id
                 WHERE s.aws_account_id = %s"""
        params = [account_id]
        if slo_id:
            sql += " AND s.id = %s"
            params.append(slo_id)
        cur.execute(sql + " ORDER BY s.name", params)
        slos = cur.fetchall()
        for s in slos:
            s.update(_compute(cur, s))
    return {"account": account, "slos": slos, "now": datetime.now(timezone.utc).replace(tzinfo=None),
            "period_start": c.naive(period_start), "period_end": c.naive(period_end)}


# ── numbers (pure, tested) ───────────────────────────────────────────

def measures(s):
    if s.get("synthetic_check_id"):
        return f"Synthetic check: {s.get('synthetic_check_name') or s['synthetic_check_id']}"
    what = s.get("resource_name") or s.get("resource_id") or "resource"
    return f"{what}" + (f" / {s['metric_name']}" if s.get("metric_name") else " (any critical alert)")


def budget_minutes(s):
    """Minutes of bad time the target allows over the SLO's window."""
    return round(int(s["window_days"]) * 24 * 60 * (1 - float(s["target_pct"]) / 100), 1)


def summarize(data):
    slos = data["slos"]
    by = {k: [s for s in slos if s.get("status") == k] for k in STATUS_ORDER}
    other = [s for s in slos if s.get("status") not in STATUS_ORDER]
    by["no_data"] += other
    ordered = sorted(slos, key=lambda s: (STATUS_ORDER.index(s["status"]) if s.get("status") in STATUS_ORDER else 3,
                                          -(s.get("budget_consumed_pct") or 0), s["name"]))
    with_data = [s for s in slos if s.get("uptime_pct") is not None]
    return {"slos": ordered, "total": len(slos), "by": by,
            "avg_uptime": round(sum(float(s["uptime_pct"]) for s in with_data) / len(with_data), 3) if with_data else None,
            "paused": [s for s in slos if not s.get("enabled")]}


def summary_paragraphs(summ):
    if not summ["total"]:
        return [("SLOs", "No SLOs are defined for this account.")]
    b = summ["by"]
    out = [("Status", f"{summ['total']} {c.plural(summ['total'], 'SLO')}: {len(b['ok'])} met, {len(b['warning'])} at risk, "
                      f"{len(b['breached'])} breached, {len(b['no_data'])} with no data yet.")]
    if b["breached"]:
        w = max(b["breached"], key=lambda s: s.get("budget_consumed_pct") or 0)
        out.append(("Breached", f"{', '.join(s['name'] for s in b['breached'][:5])}. Worst: {w['name']} has used "
                                f"{w['budget_consumed_pct']:.0f}% of its error budget."))
    if b["warning"]:
        out.append(("At risk", f"{', '.join(s['name'] for s in b['warning'][:5])} have used 75% or more of their error budget."))
    if summ["avg_uptime"] is not None:
        out.append(("Uptime", f"Average uptime across SLOs with data: {summ['avg_uptime']:.3f}%."))
    if summ["paused"]:
        out.append(("Paused", f"{len(summ['paused'])} {c.plural(len(summ['paused']), 'SLO')} "
                              f"{'is' if len(summ['paused']) == 1 else 'are'} switched off but still shown."))
    return out


def recommendations(summ):
    recs = []
    for s in summ["by"]["breached"]:
        recs.append(f"{s['name']} broke its {float(s['target_pct']):g}% target over {s['window_days']} days. Review the outages that used the budget, "
                    "or reset the target if it was never realistic.")
    for s in summ["by"]["warning"]:
        recs.append(f"{s['name']} has {s['budget_remaining_pct']:.0f}% of its error budget left. Hold risky changes until the window rolls forward.")
    for s in summ["by"]["no_data"]:
        recs.append(f"{s['name']} has no data in its window. For a synthetic check, confirm it is running; for a resource, confirm the metric is collected.")
    return recs


# ── PDF ──────────────────────────────────────────────────────────────

def render_slo_pdf(*, report_type, scope_id, scope_label, period_start, period_end, data, generated_by):
    summ = summarize(data)
    b = summ["by"]
    title = "SLO Report"
    if b["breached"]:
        status = ("SLO BREACHED", kit.SEVERITY_COLORS["CRITICAL"])
    elif b["warning"]:
        status = ("AT RISK", kit.SEVERITY_COLORS["WARNING"])
    elif summ["total"] and b["ok"]:
        status = ("ALL MET", kit.STATUS_COLORS["RESOLVED"])
    else:
        status = ("NO DATA", kit.MUTED)
    kpis = [
        {"label": "SLOs", "value": f"{summ['total']}", "note": f"{len(summ['paused'])} paused" if summ["paused"] else "all active", "tone": "ink"},
        {"label": "MET", "value": f"{len(b['ok'])}", "note": "under 75% of budget used", "tone": "ok"},
        {"label": "AT RISK", "value": f"{len(b['warning'])}", "note": "75-100% of budget used", "tone": "warn" if b["warning"] else "ok"},
        {"label": "BREACHED", "value": f"{len(b['breached'])}", "note": "budget fully used", "tone": "crit" if b["breached"] else "ok"},
    ]
    account = data.get("account")
    rows = []
    if account:
        rows += [("Account", f"{account['account_name']} ({account['account_id']})"), ("Provider", (account.get("provider") or "aws").upper())]
    pdf, now = c.open_report(title=title, label=scope_label, report_type=report_type, period_start=period_start, period_end=period_end,
                             generated_by=generated_by, status=status, kpis=kpis, keywords="CloudOps, SLO, error budget",
                             detail_rows=rows, as_of_only=True)
    kit.section_header(pdf, "Executive Summary")
    for lead, text in summary_paragraphs(summ):
        kit.paragraph(pdf, lead, text)
    if summ["slos"]:
        kit.section_header(pdf, "Error Budget by SLO")
        kit.data_table(pdf, [("SLO", 40, "L"), ("Measures", 46, "L"), ("Target", 16, "R"), ("Window", 15, "R"),
                             ("Uptime", 20, "R"), ("Budget used", 20, "R"), ("Status", 23, "C")],
                       [(s["name"] + ("" if s.get("enabled") else " (paused)"), measures(s), f"{float(s['target_pct']):g}%", f"{s['window_days']} d",
                         c.pct_text(s["uptime_pct"], 3) if s.get("uptime_pct") is not None else "-",
                         c.pct_text(s["budget_consumed_pct"], 0) if s.get("budget_consumed_pct") is not None else "-",
                         c.chip(STATUS_LABEL.get(s.get("status"), "NO DATA"), STATUS_COLOR.get(s.get("status"), kit.MUTED))) for s in summ["slos"]])
        c.note(pdf, "Each SLO is measured over its own rolling window ending now, not over the report period. Budget used is bad time divided by the "
                    "bad time the target allows (for example 99.9% over 30 days allows about 43 minutes). Met is under 75% used, at risk 75-100%, breached 100% or more.")
        kit.section_header(pdf, "Error Budget Allowed")
        kit.data_table(pdf, [("SLO", 70, "L"), ("Target", 24, "R"), ("Window", 26, "R"), ("Bad time allowed", 44, "R")],
                       [(s["name"], f"{float(s['target_pct']):g}%", f"{s['window_days']} d", c.duration(budget_minutes(s))) for s in summ["slos"]])
    recs = recommendations(summ)
    kit.section_header(pdf, "Recommendations")
    if recs:
        for r in recs:
            kit.bullet(pdf, r)
    else:
        kit.bullet(pdf, "Nothing needs attention. Every SLO is inside its error budget.")
    return c.finish(pdf, scope_label, now, generated_by)


def build_pdf(job):
    """Entry point used by app/reports/registry.py. -> (pdf_bytes, scope_label)."""
    acct_id, slo_id = parse_scope(job["scope_id"])
    if job["account_id"] is not None and int(job["account_id"]) != acct_id:
        raise ValueError("SLO report scope does not match its account")
    data = gather_slo_data(acct_id, slo_id, job["period_start"], job["period_end"])
    label = (data.get("account") or {}).get("account_name") or job["scope_id"]
    if slo_id and data["slos"]:
        label = f"{label} / {data['slos'][0]['name']}"
    pdf = render_slo_pdf(report_type=job["report_type"], scope_id=job["scope_id"], scope_label=label,
                         period_start=job["period_start"], period_end=job["period_end"], data=data, generated_by=job["requested_by"])
    return pdf, label
