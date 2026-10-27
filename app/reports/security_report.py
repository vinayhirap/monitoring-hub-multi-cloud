# app/reports/security_report.py
"""
Security posture report: the open security findings of one account (what is wrong now, how old it is), and what changed
over the period (new findings, findings fixed, how long fixes took).

scope_type SECURITY, scope_id "<account id>". Needs security.view; account-scoped like the Security Findings page, and,
like that page, findings of an account that is no longer active are left out (they can no longer be re-scanned, so they
would stay open forever).

Data: security_findings (migration 035), written by app/collector/cspm.py.
  * "Open now" is the state at generation time, whatever the period.
  * "New" counts findings whose first_seen_at falls inside the period; "fixed" counts resolved_at inside the period.
  * first_seen_at is when CloudOps FIRST saw the finding. A finding that was fixed and came back keeps its first date, so
    its age can overstate how long the current exposure has lasted. The report says so.
Resource names appear in the detail table (an admin needs them to fix anything), so the cover marks the report confidential.
"""
import logging
from datetime import datetime, timedelta, timezone

from app import pdf_kit as kit
from app.db import get_db_cursor
from app.reports import common as c

logger = logging.getLogger(__name__)

CHECK_LABELS = {   # same wording as frontend/src/pages/SecurityFindings.jsx
    "s3_bucket_public": "Public S3 bucket",
    "sg_open_to_world": "Security group open to internet",
    "ebs_unencrypted": "Unencrypted EBS volume",
    "ebs_unattached": "Unattached EBS volume (billed, idle)",
    "iam_user_no_mfa": "IAM user without MFA",
    "iam_stale_access_key": "Stale IAM access key",
    "azure_nsg_open_to_world": "NSG open to internet",
    "azure_storage_public_access": "Storage account allows public blob access",
    "azure_storage_insecure_transport": "Storage account allows unencrypted (HTTP) access",
    "gcp_firewall_open_to_world": "Firewall rule open to internet",
    "gcp_gcs_bucket_public": "Public GCS bucket",
}
SEVERITIES = ("HIGH", "MEDIUM", "LOW")
SEV_COLOR = {"HIGH": kit.SEVERITY_COLORS["CRITICAL"], "MEDIUM": kit.SEVERITY_COLORS["WARNING"], "LOW": kit.SEVERITY_COLORS["INFO"]}
AGE_BUCKETS = (("Under 7 days", 0, 7), ("7 to 30 days", 7, 30), ("30 to 90 days", 30, 90), ("Over 90 days", 90, 10 ** 6))
STALE_SCAN_HOURS = 3             # the scan runs hourly (extended tier); this long without a re-check means it stopped
_MAX_ROWS = 40


def parse_scope(scope_id):
    from app.reports.common import parse_account_scope
    acct, extra = parse_account_scope(scope_id)
    if extra is not None:
        raise ValueError("a security report is for a whole account: scope must be '<account id>'")
    return acct


def label_of(check_id):
    return CHECK_LABELS.get(check_id) or str(check_id)


# ── data ─────────────────────────────────────────────────────────────

def gather_security_data(account_id, period_start, period_end):
    start, end = c.naive(period_start), c.naive(period_end)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        cur.execute("SELECT id, account_id, account_name, provider, status FROM aws_accounts WHERE id = %s", (account_id,))
        account = cur.fetchone()
        findings = []
        if account and account.get("status") == "active":
            cur.execute("""SELECT id, check_id, resource_id, severity, title, status, first_seen_at, last_seen_at, resolved_at
                           FROM security_findings
                           WHERE aws_account_id = %s
                             AND (status = 'open' OR first_seen_at BETWEEN %s AND %s OR resolved_at BETWEEN %s AND %s)
                           ORDER BY FIELD(severity, 'HIGH', 'MEDIUM', 'LOW'), first_seen_at DESC, id DESC
                           LIMIT 20000""", (account_id, start, end, start, end))
            findings = cur.fetchall()
    return {"account": account, "findings": findings, "now": now, "period_start": start, "period_end": end}


# ── numbers (pure, tested) ───────────────────────────────────────────

def _in(dt, start, end):
    dt = c.naive(dt)
    return dt is not None and start <= dt <= end


def summarize(data):
    start, end, now = data["period_start"], data["period_end"], data["now"]
    f = data["findings"]
    open_ = [x for x in f if x["status"] == "open"]
    new = [x for x in f if _in(x["first_seen_at"], start, end)]
    fixed = [x for x in f if x["status"] == "resolved" and _in(x["resolved_at"], start, end)]
    for x in open_:
        x["age_days"] = max(0, (now - c.naive(x["first_seen_at"])).days)
    open_by_sev = {s: sum(1 for x in open_ if x["severity"] == s) for s in SEVERITIES}
    new_by_sev = {s: sum(1 for x in new if x["severity"] == s) for s in SEVERITIES}
    fix_days = [(c.naive(x["resolved_at"]) - c.naive(x["first_seen_at"])).total_seconds() / 86400 for x in fixed
                if x.get("resolved_at") and x.get("first_seen_at")]
    by_check = {}
    for x in open_:
        row = by_check.setdefault(x["check_id"], {"check_id": x["check_id"], "label": label_of(x["check_id"]), "n": 0,
                                                  "worst": "LOW", "oldest": 0})
        row["n"] += 1
        if SEVERITIES.index(x["severity"]) < SEVERITIES.index(row["worst"]):
            row["worst"] = x["severity"]
        row["oldest"] = max(row["oldest"], x["age_days"])
    ages = [{"label": lbl, "n": sum(1 for x in open_ if lo <= x["age_days"] < hi)} for lbl, lo, hi in AGE_BUCKETS]
    seen = [c.naive(x["last_seen_at"]) for x in open_ if x.get("last_seen_at")]
    newest_check = max(seen) if seen else None
    stale_scan = bool(newest_check and (now - newest_check) > timedelta(hours=STALE_SCAN_HOURS))
    return {
        "open": open_, "open_total": len(open_), "open_by_sev": open_by_sev, "new": new, "new_by_sev": new_by_sev,
        "new_total": len(new), "fixed": fixed, "fixed_total": len(fixed),
        "avg_fix_days": round(sum(fix_days) / len(fix_days), 1) if fix_days else None,
        "by_check": sorted(by_check.values(), key=lambda r: (SEVERITIES.index(r["worst"]), -r["n"], r["label"])),
        "ages": ages, "old_high": [x for x in open_ if x["severity"] == "HIGH" and x["age_days"] >= 30],
        "newest_check": newest_check, "stale_scan": stale_scan,
        "net": len(new) - len(fixed),
    }


def summary_paragraphs(summ):
    if not summ["open_total"] and not summ["new_total"] and not summ["fixed_total"]:
        return [("Findings", "No open security findings, and none opened or fixed in this period.")]
    sev = summ["open_by_sev"]
    out = [("Open now", f"{summ['open_total']:,} open {c.plural(summ['open_total'], 'finding')}: {sev['HIGH']:,} high, "
                        f"{sev['MEDIUM']:,} medium, {sev['LOW']:,} low.")]
    out.append(("This period", f"{summ['new_total']:,} {c.plural(summ['new_total'], 'finding')} first seen and {summ['fixed_total']:,} fixed"
                               + (f", so the open count {'rose' if summ['net'] > 0 else 'fell'} by {abs(summ['net']):,} from findings alone." if summ["net"] else ".")))
    if summ["avg_fix_days"] is not None:
        out.append(("Time to fix", f"Findings fixed in this period had been open {summ['avg_fix_days']} days on average "
                                   "(measured from when CloudOps first saw them)."))
    if summ["old_high"]:
        out.append(("Ageing", f"{len(summ['old_high']):,} high-severity {c.plural(len(summ['old_high']), 'finding')} "
                              f"{'has' if len(summ['old_high']) == 1 else 'have'} been open for 30 days or more."))
    if summ["stale_scan"]:
        out.append(("Data", f"The newest re-check of an open finding was {c.fmt_stamp(summ['newest_check'])} (UTC). The hourly scan may have stopped, "
                            "so the list below can be out of date."))
    return out


def recommendations(summ):
    recs = []
    for x in summ["old_high"][:5]:
        recs.append(f"Fix the high-severity finding on {x['resource_id']} ({label_of(x['check_id'])}); it has been open {x['age_days']} days.")
    if len(summ["old_high"]) > 5:
        recs.append(f"{len(summ['old_high']) - 5:,} more high-severity findings have been open 30 days or more.")
    fresh_high = [x for x in summ["open"] if x["severity"] == "HIGH" and x["age_days"] < 30]
    if fresh_high:
        recs.append(f"Review the {len(fresh_high):,} high-severity {c.plural(len(fresh_high), 'finding')} opened in the last 30 days before they age.")
    big = [r for r in summ["by_check"] if r["n"] >= 5][:2]
    for r in big:
        recs.append(f"{r['label']} accounts for {r['n']:,} open findings; fixing the cause once (a template or a policy) clears most of them.")
    if summ["stale_scan"]:
        recs.append("Check that the security scan is running: no open finding has been re-checked in over three hours.")
    return recs


# ── PDF ──────────────────────────────────────────────────────────────

def render_security_pdf(*, report_type, scope_id, scope_label, period_start, period_end, data, generated_by):
    summ = summarize(data)
    title = "Security Posture Report"
    sev = summ["open_by_sev"]
    if sev["HIGH"]:
        status = ("HIGH RISK FINDINGS", kit.SEVERITY_COLORS["CRITICAL"])
    elif summ["open_total"]:
        status = ("OPEN FINDINGS", kit.SEVERITY_COLORS["WARNING"])
    else:
        status = ("NO OPEN FINDINGS", kit.STATUS_COLORS["RESOLVED"])
    kpis = [
        {"label": "OPEN NOW", "value": f"{summ['open_total']:,}", "note": f"{sev['MEDIUM']:,} medium, {sev['LOW']:,} low", "tone": "ink"},
        {"label": "HIGH SEVERITY", "value": f"{sev['HIGH']:,}", "note": f"{len(summ['old_high'])} open 30+ days" if summ["old_high"] else "open now",
         "tone": "crit" if sev["HIGH"] else "ok"},
        {"label": "NEW IN PERIOD", "value": f"{summ['new_total']:,}", "note": f"{summ['new_by_sev']['HIGH']} high", "tone": "warn" if summ["new_total"] else "ok"},
        {"label": "FIXED IN PERIOD", "value": f"{summ['fixed_total']:,}", "note": f"avg {summ['avg_fix_days']} days open" if summ["avg_fix_days"] is not None else "none",
         "tone": "ok"},
    ]
    account = data.get("account")
    rows = []
    if account:
        rows += [("Account", f"{account['account_name']} ({account['account_id']})"), ("Provider", (account.get("provider") or "aws").upper())]
    pdf, now = c.open_report(title=title, label=scope_label, report_type=report_type, period_start=period_start, period_end=period_end,
                             generated_by=generated_by, status=status, kpis=kpis, keywords="CloudOps, security, posture, findings",
                             detail_rows=rows)
    kit.section_header(pdf, "Executive Summary")
    for lead, text in summary_paragraphs(summ):
        kit.paragraph(pdf, lead, text)

    if summ["by_check"]:
        kit.section_header(pdf, "Open Findings by Type")
        kit.data_table(pdf, [("Finding type", 84, "L"), ("Open", 20, "R"), ("Worst", 28, "C"), ("Oldest", 28, "R")],
                       [(r["label"], f"{r['n']:,}", c.chip(r["worst"], SEV_COLOR[r["worst"]]), f"{r['oldest']} d") for r in summ["by_check"]])
    if summ["open_total"]:
        kit.section_header(pdf, "How Long Findings Have Been Open")
        kit.data_table(pdf, [("Age", 60, "L"), ("Open findings", 40, "R"), ("Share", 30, "R")],
                       [(a["label"], f"{a['n']:,}", c.pct_text(100.0 * a["n"] / summ["open_total"])) for a in summ["ages"]])
        c.note(pdf, "Age counts from the day CloudOps first saw the finding. A finding that was fixed and came back keeps its first date.")

        kit.section_header(pdf, f"Open Findings ({summ['open_total']:,})")
        shown = summ["open"][:_MAX_ROWS]       # already ordered high first, then newest
        kit.data_table(pdf, [("Severity", 22, "C"), ("Finding", 52, "L"), ("Resource", 64, "L"), ("First seen", 26, "L"), ("Age", 16, "R")],
                       [(c.chip(x["severity"], SEV_COLOR[x["severity"]]), label_of(x["check_id"]), x["resource_id"], c.fmt_day(x["first_seen_at"]),
                         f"{x['age_days']} d") for x in shown])
        if summ["open_total"] > len(shown):
            c.note(pdf, f"{summ['open_total'] - len(shown):,} more open findings are on the Security Findings page.")
        c.note(pdf, "This table names resources; handle the report as confidential.")

    kit.section_header(pdf, f"Changes in This Period (+{summ['new_total']:,} / -{summ['fixed_total']:,})")
    kit.data_table(pdf, [("", 60, "L"), ("High", 30, "R"), ("Medium", 30, "R"), ("Low", 30, "R")],
                   [("First seen in period", *[f"{summ['new_by_sev'][s]:,}" for s in SEVERITIES]),
                    ("Fixed in period", *[f"{sum(1 for x in summ['fixed'] if x['severity'] == s):,}" for s in SEVERITIES]),
                    ("Open now", *[f"{sev[s]:,}" for s in SEVERITIES])])

    recs = recommendations(summ)
    kit.section_header(pdf, "Recommendations")
    if recs:
        for r in recs:
            kit.bullet(pdf, r)
    else:
        kit.bullet(pdf, "Nothing needs attention. There are no open high-severity findings and the scan is running.")
    c.note(pdf, "The checks are a short, high-signal list (public storage, open firewalls, unencrypted volumes, users without MFA, stale keys), "
                "not a full compliance audit.")
    return c.finish(pdf, scope_label, now, generated_by)


def build_pdf(job):
    """Entry point used by app/reports/registry.py. -> (pdf_bytes, scope_label)."""
    acct_id = parse_scope(job["scope_id"])
    if job["account_id"] is not None and int(job["account_id"]) != acct_id:
        raise ValueError("security report scope does not match its account")
    data = gather_security_data(acct_id, job["period_start"], job["period_end"])
    label = (data.get("account") or {}).get("account_name") or job["scope_id"]
    pdf = render_security_pdf(report_type=job["report_type"], scope_id=job["scope_id"], scope_label=label,
                              period_start=job["period_start"], period_end=job["period_end"], data=data, generated_by=job["requested_by"])
    return pdf, label
