# app/reports/synthetic_report.py
"""
Synthetic monitoring report: availability, response time, outages and TLS certificates of an account's synthetic
checks (or of one check), as a PDF in the same design and the same S3 pipeline as every other CloudOps report.

scope_type SYNTHETIC, scope_id "<account id>" (every check in the account) or "<account id>:<check id>" (one check).

Data sources:
  * synthetic_daily_stats      one row per check per UTC day (see app/collector/synthetic.py rollup_daily_stats). Raw probe
                               results are pruned after 30 days; these roll-ups are kept 400 days, so a quarterly report is
                               complete. Days the scheduled roll-up has not covered yet are rolled up first (best effort).
  * synthetic_check_results    today's probes (the current UTC day is not rolled up yet) and each check's latest TLS facts.
  * alerts                     confirmed outages ('synthetic_uptime') and certificate alerts ('synthetic_cert_expiry').

Figures are whole UTC days. Contacts are counted, never printed: reports are forwarded outside the company.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from app import pdf_kit as kit
from app.db import get_db_cursor

logger = logging.getLogger(__name__)

_SCOPE_RE = re.compile(r"^(\d{1,18})(?::(\d{1,18}))?$")
_MAX_OUTAGE_ROWS = 40
# availability bands used for the status chip; reference values, not a contractual SLA
_HEALTHY_PCT, _DEGRADED_PCT = 99.9, 99.0


def parse_scope(scope_id):
    """'12' -> (12, None); '12:34' -> (12, 34); anything else -> ValueError."""
    m = _SCOPE_RE.match(str(scope_id or ""))
    if not m:
        raise ValueError("synthetic scope must be '<account id>' or '<account id>:<check id>'")
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)


def _naive(dt):
    return dt.replace(tzinfo=None) if isinstance(dt, datetime) and dt.tzinfo else dt


def _fmt_stamp(dt):
    return _naive(dt).strftime("%d %b %Y %H:%M") if isinstance(dt, datetime) else str(dt or "-")


def _fmt_day(dt):
    return _naive(dt).strftime("%d %b %Y") if isinstance(dt, datetime) else str(dt or "-")


def _duration(minutes):
    if minutes is None:
        return "-"
    m = int(minutes)
    if m < 60:
        return f"{m} min"
    h, rem = divmod(m, 60)
    if h < 48:
        return f"{h} h {rem:02d} min"
    return f"{h // 24} d {h % 24} h"


# ── data ─────────────────────────────────────────────────────────────

def gather_synthetic_data(account_id, check_id, period_start, period_end):
    start, end = _naive(period_start), _naive(period_end)
    ensure_rollup = None
    try:
        from app.collector.synthetic import rollup_daily_stats as ensure_rollup
    except Exception:
        logger.warning("[synthetic-report] roll-up unavailable; using what is stored", exc_info=True)

    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        cur.execute("SELECT id, account_id, account_name, default_region, provider FROM aws_accounts WHERE id = %s",
                    (account_id,))
        account = cur.fetchone()
        sql = """SELECT c.id, c.name, c.check_type, c.target, c.environment, c.enabled, c.current_status,
                        c.interval_seconds, c.cert_warn_days, c.cert_crit_days, c.created_at,
                        (SELECT COUNT(*) FROM synthetic_check_contacts k WHERE k.check_id = c.id) AS contact_count
                 FROM synthetic_checks c WHERE c.aws_account_id = %s"""
        params = [account_id]
        if check_id:
            sql += " AND c.id = %s"
            params.append(check_id)
        cur.execute(sql + " ORDER BY c.name", params)
        checks = cur.fetchall()
    ids = [c["id"] for c in checks]
    if not ids:
        return {"account": account, "checks": [], "daily": {}, "outages": [], "certs": {}, "cert_alerts": {},
                "today": {}, "period_start": start, "period_end": end}

    if ensure_rollup:
        try:
            ensure_rollup(days_back=40, check_ids=ids)
        except Exception:
            logger.warning("[synthetic-report] roll-up before report failed; continuing", exc_info=True)

    marks = ", ".join(["%s"] * len(ids))
    rids = [f"synthetic-{i}" for i in ids]
    rmarks = ", ".join(["%s"] * len(rids))
    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        cur.execute(f"""SELECT check_id, day, probes, ok, avg_ms, p95_ms, max_ms, min_cert_days
                        FROM synthetic_daily_stats
                        WHERE check_id IN ({marks}) AND day >= DATE(%s) AND day <= DATE(%s) ORDER BY day""",
                    [*ids, start, end])
        daily = {}
        for r in cur.fetchall():
            daily.setdefault(r["check_id"], []).append(r)
        # the current UTC day is not rolled up yet: read it live
        cur.execute(f"""SELECT check_id, DATE(MIN(checked_at)) AS day, COUNT(*) AS probes, SUM(success) AS ok,
                               ROUND(AVG(CASE WHEN success = 1 THEN response_time_ms END)) AS avg_ms,
                               MAX(response_time_ms) AS max_ms, MIN(cert_days_left) AS min_cert_days
                        FROM synthetic_check_results
                        WHERE check_id IN ({marks}) AND checked_at >= CURDATE() AND checked_at <= %s
                        GROUP BY check_id""", [*ids, end])
        today = {}
        for r in cur.fetchall():
            r.update(p95_ms=None)
            today[r["check_id"]] = r
        cur.execute(f"""SELECT resource_id, triggered_at, resolved_at, status FROM alerts
                        WHERE aws_account_id = %s AND metric_name = 'synthetic_uptime' AND resource_id IN ({rmarks})
                          AND triggered_at <= %s AND (resolved_at IS NULL OR resolved_at >= %s)
                        ORDER BY triggered_at""", [account_id, *rids, end, start])
        outages = cur.fetchall()
        cur.execute(f"""SELECT resource_id, COUNT(*) AS n FROM alerts
                        WHERE aws_account_id = %s AND metric_name = 'synthetic_cert_expiry' AND resource_id IN ({rmarks})
                          AND triggered_at <= %s AND (resolved_at IS NULL OR resolved_at >= %s)
                        GROUP BY resource_id""", [account_id, *rids, end, start])
        cert_alerts = {int(r["resource_id"].split("-", 1)[1]): int(r["n"]) for r in cur.fetchall()}
        cur.execute(f"""SELECT r.check_id, r.cert_days_left, r.cert_not_after, r.cert_issuer, r.cert_subject, r.tls_version
                        FROM synthetic_check_results r
                        JOIN (SELECT check_id, MAX(id) AS mid FROM synthetic_check_results
                              WHERE check_id IN ({marks}) AND cert_not_after IS NOT NULL GROUP BY check_id) m
                          ON m.mid = r.id""", ids)
        certs = {r["check_id"]: r for r in cur.fetchall()}
    return {"account": account, "checks": checks, "daily": daily, "today": today, "outages": outages,
            "certs": certs, "cert_alerts": cert_alerts, "period_start": start, "period_end": end}


# ── numbers (pure, tested) ───────────────────────────────────────────

def _status(avail):
    if avail is None:
        return "NO DATA"
    return "HEALTHY" if avail >= _HEALTHY_PCT else ("DEGRADED" if avail >= _DEGRADED_PCT else "POOR")


def summarize(data):
    start, end = data["period_start"], data["period_end"]
    cur_outages = {}
    for o in data["outages"]:
        cid = int(str(o["resource_id"]).split("-", 1)[1])
        s = max(_naive(o["triggered_at"]), start)
        e = min(_naive(o["resolved_at"]) or end, end)
        minutes = max(0, int((e - s).total_seconds() // 60))
        cur_outages.setdefault(cid, []).append({"start": _naive(o["triggered_at"]), "end": _naive(o["resolved_at"]),
                                                "minutes": minutes, "open": o["status"] != "resolved"})
    per, day_failed, day_ms = [], {}, {}
    for c in data["checks"]:
        rows = list(data["daily"].get(c["id"], []))
        t = data["today"].get(c["id"])
        if t:
            rows.append(t)
        probes = sum(int(r["probes"] or 0) for r in rows)
        ok = sum(int(r["ok"] or 0) for r in rows)
        weighted = [(int(r["avg_ms"]), int(r["ok"] or 0)) for r in rows if r.get("avg_ms") is not None and r["ok"]]
        avg_ms = round(sum(a * w for a, w in weighted) / sum(w for _, w in weighted)) if weighted else None
        p95s = [int(r["p95_ms"]) for r in rows if r.get("p95_ms") is not None]
        maxes = [int(r["max_ms"]) for r in rows if r.get("max_ms") is not None]
        avail = round(100 * ok / probes, 2) if probes else None
        outs = cur_outages.get(c["id"], [])
        per.append({"check": c, "probes": probes, "ok": ok, "failed": probes - ok, "availability": avail,
                    "avg_ms": avg_ms, "worst_p95": max(p95s) if p95s else None, "max_ms": max(maxes) if maxes else None,
                    "outages": outs, "downtime_min": sum(o["minutes"] for o in outs), "status": _status(avail),
                    "cert": data["certs"].get(c["id"])})
        for r in rows:
            d = r["day"]
            day_failed[d] = day_failed.get(d, 0) + int(r["probes"] or 0) - int(r["ok"] or 0)
            if r.get("avg_ms") is not None and r["ok"]:
                a, w = day_ms.get(d, (0, 0))
                day_ms[d] = (a + int(r["avg_ms"]) * int(r["ok"]), w + int(r["ok"]))
    avails = [p["availability"] for p in per if p["availability"] is not None]
    seen_days = sorted({r["day"] for rows in data["daily"].values() for r in rows} | {t["day"] for t in data["today"].values()})
    all_outages = sorted((dict(o, check=p["check"]) for p in per for o in p["outages"]), key=lambda o: o["start"])
    certs_due = []
    for p in per:
        c, cert = p["check"], p["cert"]
        if cert and cert.get("cert_days_left") is not None:
            warn = c.get("cert_warn_days") or 30
            if cert["cert_days_left"] <= warn:
                certs_due.append(p)
    return {
        "period_start": start, "period_end": end, "per_check": per, "checks": len(per),
        "first_day": seen_days[0] if seen_days else None, "last_day": seen_days[-1] if seen_days else None,
        "avg_availability": round(sum(avails) / len(avails), 2) if avails else None,
        "probes": sum(p["probes"] for p in per), "failed": sum(p["failed"] for p in per),
        "outage_count": len(all_outages), "downtime_min": sum(p["downtime_min"] for p in per),
        "outages": all_outages,
        "longest": max(all_outages, key=lambda o: o["minutes"]) if all_outages else None,
        "certs_due": certs_due,
        "no_contacts": [p["check"] for p in per if not p["check"].get("contact_count")],
        "disabled": [p["check"] for p in per if not p["check"].get("enabled")],
        "day_failed": dict(sorted(day_failed.items())),
        "day_ms": {d: round(a / w) for d, (a, w) in sorted(day_ms.items()) if w},
        "best": max((p for p in per if p["availability"] is not None), key=lambda p: p["availability"], default=None),
        "worst": min((p for p in per if p["availability"] is not None), key=lambda p: p["availability"], default=None),
    }


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


def _coverage_text(summ, period_start=None, period_end=None):
    """Says so when the figures cover less than the requested period (a check added part-way through, or probes lost)."""
    first = summ.get("first_day")
    start = summ.get("period_start")
    if not first or not start:
        return None
    gap = (datetime.combine(first, datetime.min.time()) - _naive(start).replace(hour=0, minute=0, second=0, microsecond=0)).days \
        if not isinstance(first, datetime) else (_naive(first) - _naive(start)).days
    if gap < 2:
        return None
    return (f"Monitoring data starts on {_fmt_day(datetime.combine(first, datetime.min.time()) if not isinstance(first, datetime) else first)}, "
            f"so the figures below cover {gap_days_text(summ)} of the requested period, not all of it.")


def gap_days_text(summ):
    first, last, start, end = summ["first_day"], summ.get("last_day"), summ["period_start"], summ["period_end"]
    total = max(1, (_naive(end) - _naive(start)).days)
    have = max(1, (last - first).days + 1) if last and first else 0
    return f"{have} of {total} days"


def summary_paragraphs(summ):
    out = []
    if summ["avg_availability"] is None:
        return [("Availability", "No probe results were recorded for these checks in this period.")]
    out.append(("Availability",
                f"Across {summ['checks']} {_plural(summ['checks'], 'check')}, average availability was "
                f"{summ['avg_availability']:.2f}% over {summ['probes']:,} probes ({summ['failed']:,} failed)."))
    cov = _coverage_text(summ)
    if cov:
        out.append(("Data", cov))
    b, w = summ["best"], summ["worst"]
    if b and w and b is not w:
        out.append(("Range", f"Best: {b['check']['name']} at {b['availability']:.2f}%. "
                             f"Lowest: {w['check']['name']} at {w['availability']:.2f}%."))
    if summ["outage_count"]:
        lg = summ["longest"]
        out.append(("Outages", f"{summ['outage_count']} confirmed {_plural(summ['outage_count'], 'outage')}, about "
                               f"{_duration(summ['downtime_min'])} of downtime in total. Longest: {lg['check']['name']}, "
                               f"{_duration(lg['minutes'])}, starting {_fmt_stamp(lg['start'])} UTC."))
    else:
        out.append(("Outages", "No confirmed outage in this period."))
    if summ["certs_due"]:
        names = ", ".join(f"{p['check']['name']} ({max(p['cert']['cert_days_left'], 0)} d)" for p in summ["certs_due"][:5])
        out.append(("Certificates", f"{len(summ['certs_due'])} {_plural(len(summ['certs_due']), 'certificate')} "
                                    f"{'is' if len(summ['certs_due']) == 1 else 'are'} inside the renewal window: {names}."))
    gaps = []
    if summ["no_contacts"]:
        gaps.append(f"{len(summ['no_contacts'])} {_plural(len(summ['no_contacts']), 'check')} "
                    f"{'has' if len(summ['no_contacts']) == 1 else 'have'} no alert contacts")
    if summ["disabled"]:
        gaps.append(f"{len(summ['disabled'])} {_plural(len(summ['disabled']), 'check')} "
                    f"{'is' if len(summ['disabled']) == 1 else 'are'} paused")
    if gaps:
        out.append(("Coverage", "; ".join(gaps) + ". See the recommendations below."))
    return out


def recommendations(summ):
    recs = []
    for p in summ["certs_due"]:
        d = p["cert"]["cert_days_left"]
        recs.append(f"Renew the certificate for {p['check']['name']}: "
                    + ("it has expired." if d < 0 else f"it expires in {d} {_plural(d, 'day')}."))
    for p in summ["per_check"]:
        if p["availability"] is not None and p["availability"] < _DEGRADED_PCT:
            recs.append(f"Investigate {p['check']['name']}: availability {p['availability']:.2f}% is below {_DEGRADED_PCT}%.")
    if summ["no_contacts"]:
        recs.append("Add alert contacts to: " + ", ".join(c["name"] for c in summ["no_contacts"][:6])
                    + (" and others." if len(summ["no_contacts"]) > 6 else ".")
                    + " Without a contact, only the general notification channels hear about a problem.")
    if summ["disabled"]:
        recs.append("Resume or remove paused checks: " + ", ".join(c["name"] for c in summ["disabled"][:6]) + ".")
    return recs


# ── PDF ──────────────────────────────────────────────────────────────

def render_synthetic_pdf(*, report_type, scope_id, scope_label, period_start, period_end, data, generated_by):
    summ = summarize(data)
    label = scope_label or str(scope_id)
    title = "Synthetic Monitoring Report"
    now = datetime.now(timezone.utc)
    pdf = kit.new_document(
        band_subtitle=title, band_right=label, footer_ref=label, title=f"{title} - {label}",
        subject=f"{title}, {_fmt_day(period_start)} to {_fmt_day(period_end)}",
        keywords="CloudOps, synthetic monitoring, uptime, certificates", cover=True)
    kit.cover_page(pdf, title=title, subtitle=label,
                   meta_lines=[f"Period: {_fmt_stamp(period_start)} to {_fmt_stamp(period_end)} (UTC)",
                               f"Generated: {_fmt_stamp(now)} by {generated_by}"])
    avail = summ["avg_availability"]
    chips = [((report_type or "REPORT").upper(), kit.TEAL_DARK),
             (_status(avail), kit.STATUS_COLORS["RESOLVED"] if avail is not None and avail >= _HEALTHY_PCT
              else (kit.SEVERITY_COLORS["WARNING"] if avail is not None and avail >= _DEGRADED_PCT else kit.SEVERITY_COLORS["CRITICAL"]))]
    kit.title_block(pdf, title, chips, f"{label}  ·  {_fmt_day(period_start)} to {_fmt_day(period_end)} (UTC)")
    kit.kpi_strip(pdf, [
        {"label": "AVAILABILITY", "value": f"{avail:.2f}%" if avail is not None else "n/a", "note": "average of all checks",
         "tone": "ok" if avail is not None and avail >= _HEALTHY_PCT else ("warn" if avail is not None and avail >= _DEGRADED_PCT else "crit")},
        {"label": "CHECKS", "value": f"{summ['checks']:,}", "note": f"{len(summ['disabled'])} paused" if summ["disabled"] else "all running", "tone": "ink"},
        {"label": "OUTAGES", "value": f"{summ['outage_count']:,}", "note": f"{_duration(summ['downtime_min'])} down" if summ["outage_count"] else "none",
         "tone": "crit" if summ["outage_count"] else "ok"},
        {"label": "FAILED PROBES", "value": f"{summ['failed']:,}", "note": f"of {summ['probes']:,}", "tone": "warn" if summ["failed"] else "ok"},
        {"label": "CERTS DUE", "value": f"{len(summ['certs_due']):,}", "note": "inside renewal window" if summ["certs_due"] else "none",
         "tone": "crit" if summ["certs_due"] else "ok"},
    ])
    account = data.get("account")
    rows = []
    if account:
        rows += [("Account", f"{account['account_name']} ({account['account_id']})"),
                 ("Provider", (account.get("provider") or "aws").upper())]
    rows += [("Period", f"{_fmt_stamp(period_start)}  to  {_fmt_stamp(period_end)}"),
             ("Generated", f"{_fmt_stamp(now)} by {generated_by}")]
    kit.details_table(pdf, rows)

    kit.section_header(pdf, "Executive Summary")
    for lead, text in summary_paragraphs(summ):
        kit.paragraph(pdf, lead, text)

    if summ["per_check"]:
        kit.section_header(pdf, "Availability by Check")
        kit.data_table(pdf, [("Check", 46, "L"), ("Type", 16, "L"), ("Availability", 24, "R"), ("Probes", 20, "R"),
                             ("Failed", 17, "R"), ("Avg ms", 17, "R"), ("Worst p95", 20, "R"), ("Status", 20, "C")],
                       [(p["check"]["name"], p["check"]["check_type"].upper(),
                         f"{p['availability']:.2f}%" if p["availability"] is not None else "-",
                         f"{p['probes']:,}", f"{p['failed']:,}",
                         f"{p['avg_ms']:,}" if p["avg_ms"] is not None else "-",
                         f"{p['worst_p95']:,}" if p["worst_p95"] is not None else "-",
                         ("chip", p["status"], kit.STATUS_COLORS["RESOLVED"] if p["status"] == "HEALTHY"
                          else (kit.SEVERITY_COLORS["WARNING"] if p["status"] == "DEGRADED"
                                else (kit.SEVERITY_COLORS["CRITICAL"] if p["status"] == "POOR" else kit.MUTED))))
                        for p in summ["per_check"]])
        kit.plain_paragraph(pdf, f"Status bands are reference values: healthy {_HEALTHY_PCT}% or more, degraded "
                                 f"{_DEGRADED_PCT}% or more, poor below that. Worst p95 is the slowest day's 95th percentile "
                                 "response time. Figures are grouped by UTC day; the latest day may be partial.", size=8.5, color=kit.MUTED, italic=True)

    if summ["day_failed"]:
        kit.section_header(pdf, "Failed Probes per Day")
        kit.bar_chart(pdf, [d.strftime("%d %b") for d in summ["day_failed"]], list(summ["day_failed"].values()),
                      caption="Probes that failed on each UTC day, all checks together.")
    if summ["day_ms"]:
        kit.section_header(pdf, "Average Response Time per Day")
        kit.bar_chart(pdf, [d.strftime("%d %b") for d in summ["day_ms"]], list(summ["day_ms"].values()),
                      caption="Average response time in milliseconds of successful probes, per UTC day.")

    kit.section_header(pdf, f"Outages ({summ['outage_count']:,} in this period)")
    if summ["outages"]:
        shown = summ["outages"][:_MAX_OUTAGE_ROWS]
        kit.data_table(pdf, [("Check", 56, "L"), ("Started (UTC)", 34, "L"), ("Ended (UTC)", 34, "L"),
                             ("Duration", 24, "R"), ("State", 24, "C")],
                       [(o["check"]["name"], _fmt_stamp(o["start"]), _fmt_stamp(o["end"]) if o["end"] else "-",
                         _duration(o["minutes"]),
                         ("chip", "OPEN" if o["open"] else "RESOLVED", kit.STATUS_COLORS["OPEN" if o["open"] else "RESOLVED"]))
                        for o in shown])
        if len(summ["outages"]) > len(shown):
            kit.plain_paragraph(pdf, f"{len(summ['outages']) - len(shown):,} more outages are in CloudOps.",
                                size=8.5, color=kit.MUTED, italic=True)
    else:
        kit.plain_paragraph(pdf, "No confirmed outage in this period. A check is confirmed down only after its configured "
                                 "number of consecutive failed probes.")

    certs = [p for p in summ["per_check"] if p["cert"] and p["cert"].get("cert_not_after")]
    if certs:
        kit.section_header(pdf, "TLS Certificates")
        kit.data_table(pdf, [("Check", 38, "L"), ("Expires (UTC)", 28, "L"), ("Days left", 18, "R"),
                             ("Issuer", 70, "L"), ("Status", 26, "C")],
                       [(p["check"]["name"], _fmt_day(p["cert"]["cert_not_after"]),
                         str(p["cert"]["cert_days_left"]) if p["cert"]["cert_days_left"] is not None else "-",
                         p["cert"].get("cert_issuer") or "-",
                         _cert_chip(p["cert"]["cert_days_left"], p["check"])) for p in sorted(
                             certs, key=lambda p: p["cert"]["cert_days_left"] if p["cert"]["cert_days_left"] is not None else 10**6)])
        kit.plain_paragraph(pdf, "Days left is as last seen by CloudOps, not at the end of the period.", size=8.5,
                            color=kit.MUTED, italic=True)

    recs = recommendations(summ)
    kit.section_header(pdf, "Recommendations")
    if recs:
        for r in recs:
            kit.bullet(pdf, r)
    else:
        kit.bullet(pdf, "Nothing needs attention. Checks are healthy, certificates are outside their renewal window and every check has contacts.")
    kit.footnote(pdf, f"Generated automatically by Aurionpro CloudOps for {label} on {_fmt_stamp(now)} (requested by "
                      f"{generated_by}). Verify before external distribution.")
    return bytes(pdf.output())


def _cert_chip(days, check):
    if days is None:
        return ("chip", "UNKNOWN", kit.MUTED)
    warn, crit = check.get("cert_warn_days") or 30, check.get("cert_crit_days") or 7
    if days < 0:
        return ("chip", "EXPIRED", kit.SEVERITY_COLORS["CRITICAL"])
    if days <= crit:
        return ("chip", "CRITICAL", kit.SEVERITY_COLORS["CRITICAL"])
    if days <= warn:
        return ("chip", "RENEW SOON", kit.SEVERITY_COLORS["WARNING"])
    return ("chip", "OK", kit.STATUS_COLORS["RESOLVED"])
