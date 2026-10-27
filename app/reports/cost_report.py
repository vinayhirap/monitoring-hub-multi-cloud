# app/reports/cost_report.py
"""
Cloud cost report: what the clouds billed for CloudOps's own paid metric calls over the period, per cloud, per account and
per day, plus this month against last month and the list-price estimate by polling tier ("where to cut").

scope_type COST, scope_id "all" (every cloud with an active account) or one of "aws" | "azure" | "gcp". account_id is NULL.
ADMIN ONLY, like the CLIENT report: the figures are organisation-wide billing totals that are not filtered per account,
and a report with no account scope can only be downloaded by an admin (app/api/reports.py::_load_report_or_404).

Nothing is recomputed here. Every figure comes from the same functions the Cloud Cost page uses (app/api/usage.py:
cost_explorer_report, monthly_totals, build_tier_cost), so the PDF cannot disagree with the page. Nothing is forecast.

Honesty rules carried over from the page:
  * billed figures lag real usage by about 48 hours; the last two days are marked provisional;
  * tax is billed separately and is not included;
  * the tier figures are a LIST-PRICE ESTIMATE of CloudOps's reads, not the invoice, and exist only for the last 30 days
    (op_events retention), so that section appears only for periods that end now.
"""
import logging
from datetime import datetime, timedelta, timezone

from app import pdf_kit as kit
from app.reports import common as c

logger = logging.getLogger(__name__)

PROVIDERS = ("aws", "azure", "gcp")
PROVIDER_NAMES = {"aws": "AWS", "azure": "Azure", "gcp": "GCP"}
EXPLORER_MAX_DAYS = 400          # app/api/usage.py EXPLORER_MAX_DAYS
TIER_MAX_DAYS = 30               # op_events retention
WEEKLY_AFTER_DAYS = 62           # longer periods are charted per week so the bars stay readable
STATE_LABELS = {
    "ok": "OK", "disabled": "NOT ENABLED", "not_enabled": "NOT ENABLED", "waiting": "WAITING FOR DATA",
    "permission_missing": "NO PERMISSION", "error": "ERROR", "migration_pending": "MIGRATION PENDING",
    "no_accounts": "NO ACCOUNTS", "unavailable": "UNAVAILABLE",
}
_TOP_ACCOUNTS = 15
_TOP_TIERS = 12


def parse_scope(scope_id):
    """'all' | 'aws' | 'azure' | 'gcp' (any case) -> the lower-case key; anything else -> ValueError."""
    s = str(scope_id or "").strip().lower()
    if s != "all" and s not in PROVIDERS:
        raise ValueError("cost scope must be 'all', 'aws', 'azure' or 'gcp'")
    return s


def scope_label_for(scope):
    return "Cloud cost: all clouds" if scope == "all" else f"Cloud cost: {PROVIDER_NAMES[scope]}"


# ── data ─────────────────────────────────────────────────────────────

def _usage_module():
    from app.api import usage
    return usage


def gather_cost_data(scope, period_start, period_end, requested_by, _usage=None):
    """Reads through app.api.usage (the Cloud Cost page's own code). `_usage` lets a test inject a fake module."""
    usage = _usage or _usage_module()
    from fastapi import HTTPException
    user = {"username": requested_by, "role": "admin"}      # generate_report() already refused non-admins
    now = datetime.now(timezone.utc)
    today = now.date()
    start_d = c.naive(period_start).date()
    end_d = min(c.naive(period_end).date(), today)
    notes = []
    if (end_d - start_d).days + 1 > EXPLORER_MAX_DAYS:
        start_d = end_d - timedelta(days=EXPLORER_MAX_DAYS - 1)
        notes.append(f"The period is longer than the {EXPLORER_MAX_DAYS} days billing data is read for, so it starts on "
                     f"{c.fmt_day(start_d)}.")

    if scope == "all":
        conn = usage.get_connection()
        cur = conn.cursor()
        try:
            providers = list(usage._active_providers(cur))
        finally:
            cur.close()
            conn.close()
    else:
        providers = [scope]

    out = []
    for p in providers:
        entry = {"provider": p, "name": PROVIDER_NAMES.get(p, p), "available": False, "state": "unavailable",
                 "daily": {}, "groups": [], "rep": None}
        try:
            rep = usage.cost_explorer_report(
                start=start_d.isoformat(), end=end_d.isoformat(), granularity="daily", group_by="account",
                accounts="", regions="", categories="", scope="onboarded", cloudops_only=True, provider=p,
                current_user=user)
        except HTTPException as e:
            logger.warning("[cost-report] %s: %s", p, e.detail)
            entry["state"] = "error"
            out.append(entry)
            continue
        except Exception:
            logger.exception("[cost-report] %s failed", p)
            entry["state"] = "error"
            out.append(entry)
            continue
        entry["state"] = rep.get("state") or "unavailable"
        if "periods" in rep:
            entry.update(available=True, rep=rep, daily=usage._daily_totals(rep), groups=rep.get("groups", []))
        out.append(entry)

    monthly = None
    tiers, tier_days = None, None
    ends_now = (today - end_d).days <= usage.PROVISIONAL_DAYS
    if ends_now:
        try:
            monthly = usage.monthly_totals(current_user=user)
        except Exception:
            logger.warning("[cost-report] monthly totals unavailable", exc_info=True)
        try:
            tier_days = max(1, min(TIER_MAX_DAYS, (today - start_d).days + 1))
            conn = usage.get_connection()
            cur = conn.cursor()
            try:
                rows, truncated = usage._fetch(tier_days, cur)
            finally:
                cur.close()
                conn.close()
            try:
                env_state = usage.env_polling_state()
            except Exception:
                env_state = None
            tiers = usage.build_tier_cost(rows, tier_days, env_state)
            if truncated:
                tiers["notes"].append("More usage rows exist than were read, so the estimate is too low.")
        except Exception:
            logger.warning("[cost-report] tier estimate unavailable", exc_info=True)
            tiers = None
    return {"scope": scope, "start": start_d, "end": end_d, "today": today, "providers": out, "monthly": monthly,
            "tiers": tiers, "tier_days": tier_days, "notes": notes, "provisional_days": usage.PROVISIONAL_DAYS,
            "period_start": c.naive(period_start), "period_end": c.naive(period_end)}


# ── numbers (pure, tested) ───────────────────────────────────────────

def _bucket(day_total, start, end):
    """Daily bars for up to WEEKLY_AFTER_DAYS days, otherwise weekly (Monday start). -> (labels, values, per_week)."""
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    if len(days) <= WEEKLY_AFTER_DAYS:
        return [d.strftime("%d %b") for d in days], [round(day_total.get(d, 0.0), 4) for d in days], False
    weeks = {}
    for d in days:
        monday = d - timedelta(days=d.weekday())
        weeks[monday] = weeks.get(monday, 0.0) + day_total.get(d, 0.0)
    keys = sorted(weeks)
    return [k.strftime("%d %b") for k in keys], [round(weeks[k], 4) for k in keys], True


def summarize(data):
    today, start, end = data["today"], data["start"], data["end"]
    last_settled = today - timedelta(days=data["provisional_days"])
    per, day_total = [], {}
    for p in data["providers"]:
        row = {"provider": p["provider"], "name": p["name"], "available": p["available"], "state": p["state"],
               "total": None, "through": None, "accounts": [], "reconciled": True}
        if p["available"]:
            rep = p["rep"]
            row["total"] = float(rep["totals"]["billed_usd"])
            row["through"] = rep.get("through_date")
            row["reconciled"] = bool(rep.get("reconciliation", {}).get("ok", True))
            row["accounts"] = [{"cloud": p["name"], "label": g["label"], "total": float(g["total"]),
                                "share": g.get("share_pct"), "regions": g.get("regions") or []} for g in p["groups"]]
            row["out_of_scope"] = float((rep.get("out_of_scope") or {}).get("usd") or 0)
            for d, v in p["daily"].items():
                if start <= d <= end:
                    day_total[d] = day_total.get(d, 0.0) + float(v)
        per.append(row)
    avail = [r for r in per if r["available"]]
    total = round(sum(r["total"] for r in avail), 4)
    settled = [v for d, v in day_total.items() if d <= last_settled and v > 0]
    peak = max(day_total.items(), key=lambda kv: kv[1]) if day_total and max(day_total.values()) > 0 else None
    labels, values, weekly = _bucket(day_total, start, end)
    accounts = sorted((a for r in avail for a in r["accounts"]), key=lambda a: -a["total"])
    return {
        "per_cloud": per, "total": total, "clouds": len(avail), "unavailable": [r for r in per if not r["available"]],
        "avg_settled_day": round(sum(settled) / len(settled), 4) if settled else None, "settled_days": len(settled),
        "peak": {"day": peak[0], "usd": round(peak[1], 4), "provisional": peak[0] > last_settled} if peak else None,
        "chart": {"labels": labels, "values": values, "weekly": weekly}, "accounts": accounts,
        "provisional_in_period": end > last_settled, "last_settled": last_settled,
        "unreconciled": [r for r in avail if not r["reconciled"]],
    }


def monthly_rows(monthly, scope):
    """Rows for the month-on-month table from usage.monthly_totals(): (name, this, last, same period, change %)."""
    if not monthly:
        return []
    rows = []
    for p in monthly.get("providers", []):
        if scope != "all" and p["provider"] != scope:
            continue
        if not p.get("available"):
            continue
        this, last, same = p["this_month_usd"], p["last_month_usd"], p.get("same_period_last_month_usd")
        rows.append({"name": p["name"], "this": this, "last": last, "same": same, "change": change_pct(this, same),
                     "partial": bool(p.get("last_month_partial"))})
    if scope == "all" and len(rows) > 1:
        this = round(sum(r["this"] for r in rows), 4)
        last = round(sum(r["last"] for r in rows), 4)
        same_vals = [r["same"] for r in rows]
        same = round(sum(same_vals), 4) if all(v is not None for v in same_vals) else None
        rows.append({"name": "All clouds", "this": this, "last": last, "same": same, "change": change_pct(this, same),
                     "partial": any(r["partial"] for r in rows)})
    return rows


def change_pct(now_v, before):
    """Percent change, or None when there is nothing to compare with (never a made-up 0 or infinity)."""
    if before is None or before <= 0:
        return None
    return round(100.0 * (float(now_v) - float(before)) / float(before), 1)


def tier_rows(tiers, scope):
    if not tiers:
        return []
    rows = [t for t in tiers.get("tiers", []) if (scope == "all" or t["provider"] == scope) and (t["usd"] > 0 or t["calls"] > 0)]
    return rows[:_TOP_TIERS]


def summary_paragraphs(summ, data, monthly, tiers):
    out = []
    if not summ["clouds"]:
        return [("Billed cost", "No billing data could be read for this period. See the status of each cloud below.")]
    out.append(("Billed cost", f"CloudOps's own paid metric calls were billed {c.usd(summ['total'])} across {summ['clouds']} "
                               f"{c.plural(summ['clouds'], 'cloud')} from {c.fmt_day(data['start'])} to {c.fmt_day(data['end'])} (UTC)."
                               + (" The most recent days are provisional and can still grow." if summ["provisional_in_period"] else "")))
    if summ["avg_settled_day"] is not None:
        pk = summ["peak"]
        out.append(("Daily rate", f"{c.usd(summ['avg_settled_day'])} a day on average over {summ['settled_days']} settled "
                                  f"{c.plural(summ['settled_days'], 'day')}. Highest day: {c.usd(pk['usd'])} on {c.fmt_day(pk['day'])}"
                                  + (" (provisional)." if pk["provisional"] else ".")))
    top = summ["accounts"][0] if summ["accounts"] else None
    if top and summ["total"] > 0:
        out.append(("Largest account", f"{top['label']} ({top['cloud']}) accounts for {c.usd(top['total'])}, "
                                       f"{c.pct_text(100.0 * top['total'] / summ['total'])} of the total."))
    rows = monthly_rows(monthly, data["scope"])
    tot = rows[-1] if rows else None
    if tot and tot["change"] is not None:
        out.append(("This month", f"{c.usd(tot['this'])} so far this month against {c.usd(tot['same'])} in the same days of last "
                                  f"month ({'+' if tot['change'] > 0 else ''}{tot['change']}%). Nothing is forecast."))
    if tiers and tiers.get("levers"):
        out.append(("Where it comes from", tiers["levers"][0]["text"]))
    if summ["unavailable"]:
        names = ", ".join(f"{r['name']} ({STATE_LABELS.get(r['state'], r['state']).lower()})" for r in summ["unavailable"])
        out.append(("Data", f"No billing figures for: {names}."))
    return out


def recommendations(summ, data, tiers):
    recs = []
    for r in summ["unavailable"]:
        if r["state"] in ("disabled", "not_enabled"):
            recs.append(f"Turn on billed cost for {r['name']} so this report can show its cost; the Cloud Cost page lists the setup steps.")
        elif r["state"] == "permission_missing":
            recs.append(f"{r['name']}: the billing read is refused for lack of permission. Grant the read access described on the Cloud Cost page.")
        elif r["state"] == "migration_pending":
            recs.append("Run the pending database migrations (migrate.py apply --all-pending) to enable billed cost.")
        else:
            recs.append(f"{r['name']}: billing data could not be read ({STATE_LABELS.get(r['state'], r['state']).lower()}). Check the Cloud Cost page.")
    for r in summ["unreconciled"]:
        recs.append(f"{r['name']}: the breakdowns did not add up to the same total. Do not rely on its split until this is checked.")
    if tiers:
        for lv in tiers.get("levers", []):
            recs.append(lv["text"])
    return recs


# ── PDF ──────────────────────────────────────────────────────────────

def render_cost_pdf(*, report_type, scope, scope_label, period_start, period_end, data, generated_by):
    summ = summarize(data)
    monthly, tiers = data.get("monthly"), data.get("tiers")
    title = "Cloud Cost Report"
    if not summ["clouds"]:
        status = ("NO BILLING DATA", kit.SEVERITY_COLORS["CRITICAL"])
    elif summ["unavailable"]:
        status = ("PARTIAL DATA", kit.SEVERITY_COLORS["WARNING"])
    else:
        status = None
    pk = summ["peak"]
    kpis = [
        {"label": "BILLED (CLOUDOPS)", "value": c.usd(summ["total"]), "note": "provisional days included" if summ["provisional_in_period"] else "settled", "tone": "ink"},
        {"label": "DAILY AVERAGE", "value": c.usd(summ["avg_settled_day"]) if summ["avg_settled_day"] is not None else "n/a",
         "note": f"{summ['settled_days']} settled days", "tone": "ink"},
        {"label": "HIGHEST DAY", "value": c.usd(pk["usd"]) if pk else "n/a", "note": c.fmt_day(pk["day"]) if pk else "no cost billed", "tone": "warn" if pk else "ok"},
        {"label": "CLOUDS", "value": f"{summ['clouds']}", "note": f"{len(summ['unavailable'])} without data" if summ["unavailable"] else "all with data",
         "tone": "warn" if summ["unavailable"] else "ok"},
    ]
    pdf, now = c.open_report(title=title, label=scope_label, report_type=report_type, period_start=period_start,
                             period_end=period_end, generated_by=generated_by, status=status, kpis=kpis,
                             keywords="CloudOps, cloud cost, billing",
                             detail_rows=[("Scope", "Every cloud with an active account" if scope == "all" else PROVIDER_NAMES[scope]),
                                          ("Currency", "USD, tax not included")])
    kit.section_header(pdf, "Executive Summary")
    for lead, text in summary_paragraphs(summ, data, monthly, tiers):
        kit.paragraph(pdf, lead, text)

    kit.section_header(pdf, "Cost by Cloud")
    cols = [("Cloud", 34, "L"), ("Billed", 34, "R"), ("Share", 24, "R"), ("Data through", 36, "L"), ("Status", 52, "C")]
    rows = []
    for r in summ["per_cloud"]:
        ok = r["available"]
        rows.append((r["name"], c.usd(r["total"]) if ok else "-",
                     c.pct_text(100.0 * r["total"] / summ["total"]) if ok and summ["total"] else "-",
                     r["through"] or "-",
                     c.chip(STATE_LABELS.get(r["state"], r["state"].upper()),
                            kit.STATUS_COLORS["RESOLVED"] if r["state"] == "ok" else
                            (kit.SEVERITY_COLORS["WARNING"] if ok else kit.SEVERITY_COLORS["CRITICAL"]))))
    kit.data_table(pdf, cols, rows)
    c.note(pdf, "Billed is what the cloud charged for CloudOps's own paid metric calls, in the regions each account is onboarded in. "
                "Billing data lags real usage by up to about 48 hours.")

    ch = summ["chart"]
    if ch["values"] and any(v > 0 for v in ch["values"]):
        kit.ensure_room(pdf, 72)          # heading and chart stay together
        kit.section_header(pdf, "Cost per Week" if ch["weekly"] else "Cost per Day")
        kit.bar_chart(pdf, ch["labels"], ch["values"], value_fmt=lambda v: f"${v:,.2f}",
                      caption=("Billed cost per week (weeks start on Monday, UTC), all clouds together." if ch["weekly"]
                               else "Billed cost per UTC day, all clouds together. The last two days are provisional."))

    if summ["accounts"]:
        kit.section_header(pdf, "Cost by Account")
        shown = summ["accounts"][:_TOP_ACCOUNTS]
        kit.data_table(pdf, [("Account", 62, "L"), ("Cloud", 20, "L"), ("Billed", 28, "R"), ("Share of cloud", 28, "R"), ("Region", 42, "L")],
                       [(a["label"], a["cloud"], c.usd(a["total"]), c.pct_text(a["share"]), ", ".join(a["regions"]) or "-") for a in shown])
        if len(summ["accounts"]) > len(shown):
            c.note(pdf, f"{len(summ['accounts']) - len(shown):,} more accounts are on the Cloud Cost page.")

    mrows = monthly_rows(monthly, data["scope"])
    if mrows:
        kit.section_header(pdf, "This Month Against Last Month")
        kit.data_table(pdf, [("Cloud", 34, "L"), ("This month so far", 36, "R"), ("Same days last month", 38, "R"),
                             ("Change", 24, "R"), ("Last month", 32, "R")],
                       [(r["name"], c.usd(r["this"]), c.usd(r["same"]) if r["same"] is not None else "-",
                         "-" if r["change"] is None else f"{'+' if r['change'] > 0 else ''}{r['change']}%", c.usd(r["last"])) for r in mrows])
        c.note(pdf, "Actual billed cost, no forecast. Months are UTC calendar months. The last two days of this month are provisional."
                    + (" Billing history for at least one cloud starts after the first day of last month, so last month is understated for it."
                       if any(r["partial"] for r in mrows) else ""))

    trows = tier_rows(tiers, data["scope"])
    if trows:
        kit.section_header(pdf, "Where the Cost Comes From (Estimate)")
        kit.data_table(pdf, [("Polling tier", 50, "L"), ("Cloud", 18, "L"), ("Estimate", 26, "R"), ("Share", 18, "R"),
                             ("Calls", 24, "R"), ("If polled half as often", 44, "R")],
                       [(t["label"], PROVIDER_NAMES.get(t["provider"], t["provider"]), c.usd(t["usd"]), c.pct_text(t["share_pct"]), f"{t['calls']:,}",
                         f"saves about {c.usd(t['halve_saving_usd'])}" if t.get("halve_saving_usd") else ("people trigger it" if t.get("user_triggered") else "-")) for t in trows])
        c.note(pdf, f"List-price estimate of CloudOps's own metered reads over the last {data['tier_days']} days, not the invoice. "
                    "Free tiers are ignored, so Azure and GCP figures are a ceiling. The saving is arithmetic (calls scale with "
                    "1 / polling interval), not a forecast.")

    recs = recommendations(summ, data, tiers)
    kit.section_header(pdf, "Recommendations")
    if recs:
        for r in recs:
            kit.bullet(pdf, r)
    else:
        kit.bullet(pdf, "Nothing needs attention. Billing data is complete for every cloud in scope.")
    for n in data.get("notes", []):
        c.note(pdf, n)
    c.note(pdf, "Tax is billed separately and is not included in any figure here.")
    return c.finish(pdf, scope_label, now, generated_by)


def build_pdf(job):
    """Entry point used by app/reports/registry.py. -> (pdf_bytes, scope_label)."""
    scope = parse_scope(job["scope_id"])
    label = scope_label_for(scope)
    data = gather_cost_data(scope, job["period_start"], job["period_end"], job["requested_by"])
    pdf = render_cost_pdf(report_type=job["report_type"], scope=scope, scope_label=label, period_start=job["period_start"],
                          period_end=job["period_end"], data=data, generated_by=job["requested_by"])
    return pdf, label
