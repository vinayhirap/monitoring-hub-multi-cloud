# app/reports/environment_report.py
"""
Environment and tag compliance report: how one account's resources are placed in PROD and NON-PROD, how that was decided,
how well they follow the tagging standard (docs/TAGGING_STANDARD.md), what is still waiting for a placement, and how tag
compliance moved over the period.

scope_type ENVIRONMENT, scope_id "<account id>". Needs environments.view; account-scoped like the Environments page.

Everything except the trend is the state AT GENERATION TIME (the Environments page has no history of placements); the
trend comes from the daily snapshots in environment_compliance_snapshots (migration 099) and covers the chosen period.
The report says so on its cover, so a quarterly report is not mistaken for a quarter's worth of placement history.

The numbers are the Environments page's own: compliance and the tagging-gap list come from app/env_governance_db.py, so
the PDF and the page (and its CSV exports) cannot disagree. The gap list uses the four tags CloudOps itself reads
(Environment, Criticality, Owner, Application); the wider standard is cost and governance data the page does not nag about.
"""
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from app import pdf_kit as kit
from app.db import get_db_cursor
from app.reports import common as c

logger = logging.getLogger(__name__)

ENV_LABELS = {"prod": "PROD", "nonprod": "NON-PROD", "unclassified": "NOT PLACED YET"}
SOURCE_LABELS = {"manual": "Set by a person", "tag": "Environment tag", "name": "Resource name", "related": "Related resource",
                 "account": "Account default", "unknown": "Could not be decided", "none": "Not evaluated yet"}
TARGET_OK_PCT = 90.0            # reference value for the status chip, not a policy
WEEKLY_AFTER_POINTS = 62
_MAX_GAP_ROWS = 30
_MAX_TYPE_ROWS = 20


def parse_scope(scope_id):
    from app.reports.common import parse_account_scope
    acct, extra = parse_account_scope(scope_id)
    if extra is not None:
        raise ValueError("an environment report is for a whole account: scope must be '<account id>'")
    return acct


# ── data ─────────────────────────────────────────────────────────────

def gather_environment_data(account_id, period_start, period_end):
    from app import env_governance_db as gov
    from app import environment_classifier as ec
    start, end = c.naive(period_start), c.naive(period_end)
    today = datetime.now(timezone.utc).date()
    live, live_p = gov._live_where()
    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        cur.execute("SELECT id, account_id, account_name, provider, default_env FROM aws_accounts WHERE id = %s", (account_id,))
        account = cur.fetchone()
        cur.execute(f"""SELECT COALESCE(r.env_resolved, 'unclassified') AS env, COALESCE(r.env_source, 'none') AS source, COUNT(*) AS n
                        FROM resources r WHERE r.aws_account_id = %s AND {live} GROUP BY env, source""",
                    [account_id, *live_p])
        placement = cur.fetchall()
    compliance = gov.compliance_report([account_id], account_id)
    try:
        days = max(1, min(365, (today - start.date()).days + 1))
        trend = [p for p in gov.compliance_trend([account_id], days, account_id)
                 if start.date() <= datetime.strptime(p["date"][:10], "%Y-%m-%d").date() <= end.date()]
        trend_available = True
    except Exception as e:
        if getattr(e, "errno", None) != 1146:
            logger.warning("[environment-report] trend unavailable", exc_info=True)
        trend, trend_available = [], False
    gaps, truncated = gov.tagging_gap_rows([account_id], account_id, None, True)
    return {"account": account, "placement": placement, "compliance": compliance, "trend": trend, "trend_available": trend_available,
            "gaps": gaps, "gaps_truncated": truncated, "classification_on": ec.flag_enabled(),
            "period_start": start, "period_end": end}


# ── numbers (pure, tested) ───────────────────────────────────────────

def _pct(n, total):
    return round(100.0 * n / total, 1) if total else 0.0


def summarize(data):
    by_env, by_source = Counter(), Counter()
    for r in data["placement"]:
        by_env[r["env"]] += int(r["n"])
        by_source[r["source"]] += int(r["n"])
    total = sum(by_env.values())
    acct = (data["compliance"]["accounts"] or [None])[0]
    block = acct or data["compliance"]["totals"]
    tags = block["by_tag_state"]
    needs_tagging = tags["nonstandard"]["n"] + tags["invalid"]["n"] + tags["missing"]["n"]
    types = []
    for t in (acct["types"] if acct else []):
        tt = t["by_tag_state"]
        types.append({"type": t["resource_type"], "total": t["total"], "ok": tt["ok"]["n"],
                      "needs": tt["nonstandard"]["n"] + tt["invalid"]["n"] + tt["missing"]["n"],
                      "not_checked": tt["not_checked"]["n"], "pending": tt["pending"]["n"], "ok_pct": t["ok_of_checkable_pct"],
                      "checkable": t["checkable"]})
    missing_tag = Counter()
    gap_types = Counter()
    for row in data["gaps"]:
        gap_types[row[2]] += 1
        for tag in str(row[9]).split("; "):
            if tag:
                missing_tag[tag] += 1
    needs_placement = by_source.get("unknown", 0) + by_source.get("none", 0)
    trend = data["trend"]
    first, last = (trend[0], trend[-1]) if trend else (None, None)
    return {
        "total": total, "prod": by_env.get("prod", 0), "nonprod": by_env.get("nonprod", 0), "unplaced": by_env.get("unclassified", 0),
        "by_source": by_source, "needs_placement": needs_placement,
        "ok_pct": block["ok_of_checkable_pct"], "checkable": block["checkable"], "tags": tags, "needs_tagging": needs_tagging,
        "warnings": block["warnings"], "types": sorted(types, key=lambda t: (-t["needs"], -t["total"], t["type"])),
        "missing_tag": missing_tag.most_common(), "gap_types": gap_types.most_common(), "gap_count": len(data["gaps"]),
        "trend": trend, "trend_change": (round(last["ok_of_checkable_pct"] - first["ok_of_checkable_pct"], 1)
                                         if first and last and first is not last else None),
        "default_env": (data.get("account") or {}).get("default_env"),
    }


def trend_points(trend):
    """(labels, values) of ok-of-checkable % per snapshot day; the last snapshot of each week once there are many."""
    pts = [(datetime.strptime(p["date"][:10], "%Y-%m-%d").date(), p["ok_of_checkable_pct"]) for p in trend if p["checkable"]]
    if len(pts) > WEEKLY_AFTER_POINTS:
        weekly = {}
        for d, v in pts:
            weekly[d - timedelta(days=d.weekday())] = v      # later days overwrite earlier ones in the same week
        pts = sorted(weekly.items())
    return [d.strftime("%d %b") for d, _ in pts], [v for _, v in pts]


def summary_paragraphs(summ, data):
    if not summ["total"]:
        return [("Resources", "No resources are currently tracked in this account, so there is nothing to place or tag.")]
    out = [("Placement", f"{summ['total']:,} resources: {summ['prod']:,} PROD, {summ['nonprod']:,} NON-PROD"
                         + (f", {summ['unplaced']:,} not placed yet" if summ["unplaced"] else "") + ". "
                         + (f"{summ['needs_placement']:,} could not be decided automatically and wait in the Needs classification queue."
                            if summ["needs_placement"] else "Every resource has a placement."))]
    if summ["checkable"]:
        out.append(("Tags", f"{summ['ok_pct']:.1f}% of the {summ['checkable']:,} resources whose own tags CloudOps can read follow the tagging "
                            f"standard; {summ['needs_tagging']:,} {c.plural(summ['needs_tagging'], 'resource')} need tagging."))
    else:
        out.append(("Tags", "CloudOps cannot read the tags of any resource type here yet, so tag compliance is not measured."))
    nc = summ["tags"]["not_checked"]["n"]
    if nc:
        out.append(("Not checked", f"{nc:,} resources are of types whose own tags CloudOps does not read. They are not counted as untagged."))
    if summ["warnings"]:
        out.append(("Mismatches", f"{summ['warnings']:,} resources have a placement that disagrees with their tag or name; review them on the Environments page."))
    if summ["trend_change"] is not None:
        t = summ["trend"]
        out.append(("Trend", f"Tag compliance moved from {t[0]['ok_of_checkable_pct']:.1f}% on {t[0]['date'][:10]} to "
                             f"{t[-1]['ok_of_checkable_pct']:.1f}% on {t[-1]['date'][:10]} ({'+' if summ['trend_change'] > 0 else ''}{summ['trend_change']} points)."))
    if not data["classification_on"]:
        out.append(("Classification", "Environment classification is switched off on this installation, so placements are not being "
                                      "evaluated and alert strength does not depend on environment."))
    return out


def recommendations(summ):
    recs = []
    if summ["needs_placement"]:
        recs.append(f"Place the {summ['needs_placement']:,} resources in the Needs classification queue (Environments page). "
                    "Until then they are treated as PROD, so they are never alerted more weakly.")
    if summ["needs_tagging"] and summ["missing_tag"]:
        top = ", ".join(f"{t} ({n:,})" for t, n in summ["missing_tag"][:3])
        recs.append(f"Add the missing tags, most often: {top}. The list below names the resources.")
    worst = [t for t in summ["types"] if t["needs"] and t["checkable"]][:2]
    for t in worst:
        recs.append(f"Start with {t['type']}: {t['needs']:,} of {t['checkable']:,} checkable resources need tagging ({t['ok_pct']:.0f}% compliant).")
    if summ["warnings"]:
        recs.append(f"Review the {summ['warnings']:,} placement mismatches: a manual choice or a name disagrees with the Environment tag.")
    if summ["checkable"] and summ["ok_pct"] < TARGET_OK_PCT and not summ["needs_tagging"]:
        recs.append("Some resources have no readable tags yet; run discovery again and re-check.")
    return recs


# ── PDF ──────────────────────────────────────────────────────────────

def render_environment_pdf(*, report_type, scope_id, scope_label, period_start, period_end, data, generated_by):
    summ = summarize(data)
    title = "Environment & Tag Compliance Report"
    if not summ["total"]:
        status = ("NO RESOURCES", kit.MUTED)
    elif summ["checkable"] and summ["ok_pct"] >= TARGET_OK_PCT and not summ["needs_placement"]:
        status = ("ON TRACK", kit.STATUS_COLORS["RESOLVED"])
    else:
        status = ("NEEDS WORK", kit.SEVERITY_COLORS["WARNING"])
    kpis = [
        {"label": "RESOURCES", "value": f"{summ['total']:,}", "note": "tracked now", "tone": "ink"},
        {"label": "PROD", "value": f"{summ['prod']:,}", "note": f"{_pct(summ['prod'], summ['total'])}% of resources", "tone": "ink"},
        {"label": "NON-PROD", "value": f"{summ['nonprod']:,}", "note": f"{_pct(summ['nonprod'], summ['total'])}% of resources", "tone": "ink"},
        {"label": "WAITING FOR PLACEMENT", "value": f"{summ['needs_placement']:,}", "note": "treated as PROD" if summ["needs_placement"] else "none",
         "tone": "warn" if summ["needs_placement"] else "ok"},
        {"label": "TAGS OK", "value": f"{summ['ok_pct']:.1f}%" if summ["checkable"] else "n/a", "note": f"of {summ['checkable']:,} checkable",
         "tone": "ok" if summ["checkable"] and summ["ok_pct"] >= TARGET_OK_PCT else "warn"},
    ]
    account = data.get("account")
    rows = []
    if account:
        rows += [("Account", f"{account['account_name']} ({account['account_id']})"), ("Provider", (account.get("provider") or "aws").upper()),
                 ("Account default", ENV_LABELS.get(account.get("default_env") or "", "not set"))]
    pdf, now = c.open_report(title=title, label=scope_label, report_type=report_type, period_start=period_start, period_end=period_end,
                             generated_by=generated_by, status=status, kpis=kpis, keywords="CloudOps, environments, tagging, compliance",
                             detail_rows=rows + [("Trend range", f"{c.fmt_day(period_start)} to {c.fmt_day(period_end)} (UTC)")],
                             as_of_only=True)
    kit.section_header(pdf, "Executive Summary")
    for lead, text in summary_paragraphs(summ, data):
        kit.paragraph(pdf, lead, text)

    if summ["total"]:
        kit.section_header(pdf, "How Resources Were Placed")
        kit.data_table(pdf, [("Decided by", 90, "L"), ("Resources", 40, "R"), ("Share", 40, "R")],
                       [(SOURCE_LABELS.get(k, k), f"{n:,}", c.pct_text(_pct(n, summ["total"])))
                        for k, n in sorted(summ["by_source"].items(), key=lambda kv: -kv[1])])
        c.note(pdf, "Resources CloudOps could not place, and those not evaluated yet, are treated as PROD until someone places them.")

    if summ["types"]:
        kit.section_header(pdf, "Tag Compliance by Resource Type")
        shown = summ["types"][:_MAX_TYPE_ROWS]
        kit.data_table(pdf, [("Resource type", 44, "L"), ("Total", 20, "R"), ("Tags OK", 22, "R"), ("Need tagging", 28, "R"),
                             ("Not checked", 26, "R"), ("OK of checkable", 40, "R")],
                       [(t["type"], f"{t['total']:,}", f"{t['ok']:,}", f"{t['needs']:,}", f"{t['not_checked']:,}",
                         c.pct_text(t["ok_pct"]) if t["checkable"] else "-") for t in shown])
        if len(summ["types"]) > len(shown):
            c.note(pdf, f"{len(summ['types']) - len(shown):,} more resource types are on the Environments page.")
        c.note(pdf, "Not checked means CloudOps does not read that type's own tags; those resources are never counted as untagged.")

    labels, values = trend_points(summ["trend"])
    if len(values) >= 2:
        kit.ensure_room(pdf, 72)
        kit.section_header(pdf, "Tag Compliance Over the Period")
        kit.bar_chart(pdf, labels, values, value_fmt=lambda v: f"{v:.0f}%",
                      caption="Share of checkable resources that follow the tagging standard, from the daily snapshot (last snapshot of each week for long periods).")
    elif not data["trend_available"]:
        c.note(pdf, "No trend: the daily compliance snapshot is not available on this installation yet (migration 099).")
    elif summ["trend"]:
        c.note(pdf, "Only one daily snapshot falls inside this period, so no trend can be drawn yet.")

    if summ["gap_count"]:
        kit.section_header(pdf, f"Resources Missing Essential Tags ({summ['gap_count']:,})")
        if summ["missing_tag"]:
            kit.data_table(pdf, [("Missing tag", 70, "L"), ("Resources", 40, "R")], [(t, f"{n:,}") for t, n in summ["missing_tag"]])
        shown = data["gaps"][:_MAX_GAP_ROWS]
        kit.data_table(pdf, [("Resource", 60, "L"), ("Type", 22, "L"), ("Environment", 26, "L"), ("Missing tags", 72, "L")],
                       [(row[4] or row[3], row[2], ENV_LABELS.get(row[6] or "unclassified", str(row[6])), row[9]) for row in shown])
        if summ["gap_count"] > len(shown):
            c.note(pdf, f"{summ['gap_count'] - len(shown):,} more resources are in the tagging-gaps export on the Environments page."
                        + (" The list itself was capped." if data["gaps_truncated"] else ""))
        c.note(pdf, "Essential tags are the four CloudOps reads: Environment, Criticality, Owner and Application. The wider standard "
                    "(CostCenter, DataClassification, ManagedBy) is not checked here.")

    recs = recommendations(summ)
    kit.section_header(pdf, "Recommendations")
    if recs:
        for r in recs:
            kit.bullet(pdf, r)
    else:
        kit.bullet(pdf, "Nothing needs attention. Every resource is placed and the resources CloudOps can check follow the tagging standard.")
    return c.finish(pdf, scope_label, now, generated_by)


def build_pdf(job):
    """Entry point used by app/reports/registry.py. -> (pdf_bytes, scope_label)."""
    acct_id = parse_scope(job["scope_id"])
    if job["account_id"] is not None and int(job["account_id"]) != acct_id:
        raise ValueError("environment report scope does not match its account")
    data = gather_environment_data(acct_id, job["period_start"], job["period_end"])
    label = (data.get("account") or {}).get("account_name") or job["scope_id"]
    pdf = render_environment_pdf(report_type=job["report_type"], scope_id=job["scope_id"], scope_label=label,
                                 period_start=job["period_start"], period_end=job["period_end"], data=data,
                                 generated_by=job["requested_by"])
    return pdf, label
