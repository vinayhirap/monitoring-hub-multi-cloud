# app/reports/host_report.py
"""
Host agents report: the servers that run the CloudOps host agent in one account (or one host): how long each reported,
heartbeat gaps, current state, resource use over the period, watched services that are not running, and alerts.

scope_type HOST, scope_id "<account id>" (every host in the account) or "<account id>:<host agent id>" (one host).
Needs hostagents.view and HOST_AGENT_ENABLED; account-scoped like the Host Agents page.

Data sources (all existing tables, nothing new is collected):
  host_agents                  identity, OS, agent version, status, last_seen_at, enrolled_at
  host_agent_watchdog_state    current online / stale / offline and since when (migration 121; optional)
  host_agent_services          watched services and their latest state (migration 123; optional)
  alerts                       'host_agent_heartbeat' alerts = the gaps; other alerts on agent-fed metrics
  metric_history               CPU, memory, swap and disk use. Kept 30 days (app/metric_display.py), so a longer period
                               is reported over the last 30 days and says so.

"Reporting" is the share of the period with no open heartbeat alert. The watchdog only alerts after a host has been silent
past its threshold, so a gap shorter than that is not counted. Hosts enrolled part-way through are measured from enrolment.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from app import pdf_kit as kit
from app.db import get_db_cursor
from app.reports import common as c

logger = logging.getLogger(__name__)

HEARTBEAT_NAME = "host_agent_heartbeat"
HISTORY_DAYS = 30                 # app/metric_display.py METRIC_HISTORY_RETENTION_DAYS
DISK_WARN_PCT = 85.0              # reference values for the recommendations, not alert thresholds
MEM_WARN_PCT = 85.0
CPU_WARN_PCT = 80.0
_MAX_GAP_ROWS = 40
_MAX_SERVICE_ROWS = 40
_USE_CONTRACTS = ("cpu_percent", "mem_used_percent", "swap_used_percent", "disk_used_percent")


def parse_scope(scope_id):
    from app.reports.common import parse_account_scope
    return parse_account_scope(scope_id)


def derive_state(status, last_seen_at, now, stale_seconds):
    """Same rule as app/api/host_agents.py::_derive_state: online within stale seconds, stale up to 5x, offline beyond."""
    if status == "revoked":
        return "revoked"
    if last_seen_at is None:
        return "offline"
    age = (now - last_seen_at).total_seconds()
    if age <= stale_seconds:
        return "online"
    return "stale" if age <= stale_seconds * 5 else "offline"


def _version_key(v):
    parts = re.findall(r"\d+", str(v or ""))
    return tuple(int(p) for p in parts) if parts else ()


# ── data ─────────────────────────────────────────────────────────────

def _optional(cur, sql, params, what):
    """A table from a migration that may not be applied yet: empty result, never a failed report."""
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    except Exception as e:
        if getattr(e, "errno", None) == 1146 or "doesn't exist" in str(e):
            logger.info("[host-report] %s not available (migration pending)", what)
            return []
        raise


def gather_host_data(account_id, host_id, period_start, period_end):
    from app.hostagents import settings
    start, end = c.naive(period_start), c.naive(period_end)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        cur.execute("SELECT id, account_id, account_name, default_region, provider FROM aws_accounts WHERE id = %s", (account_id,))
        account = cur.fetchone()
        sql = ("SELECT id, hostname, instance_id, resource_id, os, arch, agent_version, status, last_seen_at, enrolled_at, config_json "
               "FROM host_agents WHERE aws_account_id = %s")
        params = [account_id]
        if host_id:
            sql += " AND id = %s"
            params.append(host_id)
        cur.execute(sql + " ORDER BY hostname, id", params)
        hosts = cur.fetchall()
        if not hosts:
            return {"account": account, "hosts": [], "now": now, "period_start": start, "period_end": end,
                    "stale_seconds": settings.stale_seconds(), "watchdog": {}, "services": {}, "gaps": [], "alerts": [],
                    "usage": {}, "usage_start": None}
        ids = [h["id"] for h in hosts]
        marks = ", ".join(["%s"] * len(ids))
        watchdog = {r["host_agent_id"]: r for r in _optional(
            cur, f"SELECT host_agent_id, state, state_since, offline_reason FROM host_agent_watchdog_state WHERE host_agent_id IN ({marks})",
            ids, "host_agent_watchdog_state")}
        services = {}
        for r in _optional(cur, f"SELECT host_agent_id, service_name, state, state_since, observed_at FROM host_agent_services "
                                f"WHERE host_agent_id IN ({marks}) ORDER BY service_name", ids, "host_agent_services"):
            services.setdefault(r["host_agent_id"], []).append(r)

        # alerts hang on whichever resource the host maps to: its own resource_id, its EC2 instance, or a host_agent row
        rid_to_host = {}
        for h in hosts:
            for rid in (h.get("resource_id"), h.get("instance_id"), f"host-agent-{h['id']}"):
                if rid:
                    rid_to_host.setdefault(rid, h["id"])
        rids = list(rid_to_host)
        rmarks = ", ".join(["%s"] * len(rids))
        cur.execute(f"""SELECT id, resource_id, metric_name, severity, status, triggered_at, resolved_at FROM alerts
                        WHERE aws_account_id = %s AND resource_id IN ({rmarks}) AND triggered_at <= %s
                          AND (resolved_at IS NULL OR resolved_at >= %s) ORDER BY triggered_at LIMIT 5000""",
                    [account_id, *rids, end, start])
        alerts = []
        from app.hostagents import metric_map
        for a in cur.fetchall():
            name = a["metric_name"] or ""
            if name == HEARTBEAT_NAME or metric_map.describe_stored_name(name) is not None:
                a["host_id"] = rid_to_host[a["resource_id"]]
                a["heartbeat"] = name == HEARTBEAT_NAME
                alerts.append(a)

        # utilisation from metric_history (30 days retention)
        usage_start = max(start, now - timedelta(days=HISTORY_DAYS))
        res_by_host = {}
        wanted = [(h["resource_id"], "host" if str(h["resource_id"]).startswith("host:") else "ec2", h["id"])
                  for h in hosts if h.get("resource_id")]
        if wanted:
            wm = ", ".join(["%s"] * len(wanted))
            cur.execute(f"SELECT id, resource_id, resource_type FROM resources WHERE aws_account_id = %s AND resource_id IN ({wm}) "
                        "AND resource_type IN ('host', 'ec2')", [account_id, *[w[0] for w in wanted]])
            found = {(r["resource_id"], r["resource_type"]): r["id"] for r in cur.fetchall()}
            for rid, rtype, hid in wanted:
                if (rid, rtype) in found:
                    res_by_host[found[(rid, rtype)]] = hid
        usage = {}
        if res_by_host and usage_start < end:
            pk = list(res_by_host)
            pm = ", ".join(["%s"] * len(pk))
            cur.execute(f"""SELECT resource_id, metric_name, AVG(metric_value) AS avg_v, MAX(metric_value) AS max_v, COUNT(*) AS n
                            FROM metric_history
                            WHERE resource_id IN ({pm}) AND metric_timestamp BETWEEN %s AND %s
                              AND (metric_name IN ('host_cpu_percent', 'mem_used_percent', 'host_swap_used_percent', 'disk_used_percent')
                                   OR metric_name LIKE %s)
                            GROUP BY resource_id, metric_name""",
                        [*pk, usage_start, end, r"disk\_used\_percent\_\_%"])
            from app.hostagents import metric_map as mm
            for r in cur.fetchall():
                info = mm.describe_stored_name(r["metric_name"])
                if not info or info["contract"] not in _USE_CONTRACTS or r["avg_v"] is None:
                    continue
                usage.setdefault(res_by_host[r["resource_id"]], []).append(
                    {"contract": info["contract"], "label": info.get("label"), "avg": float(r["avg_v"]),
                     "max": float(r["max_v"]), "n": int(r["n"])})
    return {"account": account, "hosts": hosts, "now": now, "period_start": start, "period_end": end,
            "stale_seconds": settings.stale_seconds(), "watchdog": watchdog, "services": services, "alerts": alerts,
            "usage": usage, "usage_start": usage_start}


# ── numbers (pure, tested) ───────────────────────────────────────────

def _merge(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def summarize(data):
    start, end, now = data["period_start"], data["period_end"], data["now"]
    hosts = data["hosts"]
    live_versions = [h["agent_version"] for h in hosts if h.get("status") != "revoked" and h.get("agent_version")]
    newest = max(live_versions, key=_version_key) if live_versions else None
    per, all_gaps = [], []
    for h in hosts:
        eff_start = max(start, c.naive(h["enrolled_at"])) if h.get("enrolled_at") else start
        period_min = max(1.0, (end - eff_start).total_seconds() / 60)
        mine = [a for a in data["alerts"] if a["host_id"] == h["id"]]
        gaps, spans = [], []
        for a in (x for x in mine if x["heartbeat"]):
            s = max(c.naive(a["triggered_at"]), eff_start)
            e = min(c.naive(a["resolved_at"]) or end, end)
            if e <= s:
                continue
            spans.append((s, e))
            gaps.append({"host": h, "start": c.naive(a["triggered_at"]), "end": c.naive(a["resolved_at"]),
                         "minutes": int((e - s).total_seconds() // 60), "open": a["status"] != "resolved"})
        down = sum((e - s).total_seconds() / 60 for s, e in _merge(spans))
        state = derive_state(h["status"], h.get("last_seen_at"), now, data["stale_seconds"])
        wd = data["watchdog"].get(h["id"])
        svcs = data["services"].get(h["id"], [])
        bad_svcs = [s for s in svcs if s["state"] != "running"]
        use = {}
        for m in data["usage"].get(h["id"], []):
            if m["contract"] == "disk_used_percent":       # one row per mount: keep the fullest
                if "disk" not in use or m["max"] > use["disk"]["max"]:
                    use["disk"] = dict(m)
            else:
                use[m["contract"]] = dict(m)
        crit = sum(1 for a in mine if not a["heartbeat"] and str(a["severity"]).upper() == "CRITICAL")
        warn = sum(1 for a in mine if not a["heartbeat"] and str(a["severity"]).upper() == "WARNING")
        age = (now - h["last_seen_at"]).total_seconds() if h.get("last_seen_at") else None
        row = {"host": h, "state": state, "since": wd["state_since"] if wd else None, "offline_reason": wd.get("offline_reason") if wd else None,
               "last_seen_age": age, "reporting": round(100 * (1 - min(down, period_min) / period_min), 2),
               "gaps": gaps, "downtime_min": int(down), "services": svcs, "bad_services": bad_svcs, "use": use,
               "alerts_critical": crit, "alerts_warning": warn,
               "outdated": bool(newest and h.get("agent_version") and h["status"] != "revoked"
                                and _version_key(h["agent_version"]) < _version_key(newest))}
        per.append(row)
        all_gaps.extend(gaps)
    active = [p for p in per if p["state"] != "revoked"]
    all_gaps.sort(key=lambda g: g["start"])
    return {
        "per_host": per, "hosts": len(per), "active": len(active), "revoked": len(per) - len(active),
        "online": sum(1 for p in active if p["state"] == "online"), "stale": [p for p in active if p["state"] == "stale"],
        "offline": [p for p in active if p["state"] == "offline"], "gap_count": len(all_gaps), "gaps": all_gaps,
        "downtime_min": sum(p["downtime_min"] for p in per),
        "bad_services": [(p, s) for p in active for s in p["bad_services"]],
        "alerts_critical": sum(p["alerts_critical"] for p in per), "alerts_warning": sum(p["alerts_warning"] for p in per),
        "outdated": [p for p in active if p["outdated"]], "newest_version": newest,
        "avg_reporting": round(sum(p["reporting"] for p in active) / len(active), 2) if active else None,
        "disk_high": [p for p in active if p["use"].get("disk") and p["use"]["disk"]["max"] >= DISK_WARN_PCT],
        "mem_high": [p for p in active if p["use"].get("mem_used_percent") and p["use"]["mem_used_percent"]["avg"] >= MEM_WARN_PCT],
        "cpu_high": [p for p in active if p["use"].get("cpu_percent") and p["use"]["cpu_percent"]["avg"] >= CPU_WARN_PCT],
        "never_seen": [p for p in active if p["host"].get("last_seen_at") is None],
        "usage_start": data.get("usage_start"),
    }


def _name(p):
    return p["host"]["hostname"]


def summary_paragraphs(summ, data):
    if not summ["hosts"]:
        return [("Hosts", "No host agents are enrolled in this account.")]
    out = [("Fleet", f"{summ['active']} active {c.plural(summ['active'], 'host')}"
                     + (f" and {summ['revoked']} revoked" if summ["revoked"] else "")
                     + f": {summ['online']} online now, {len(summ['stale'])} stale, {len(summ['offline'])} offline.")]
    if summ["avg_reporting"] is not None:
        out.append(("Reporting", f"Hosts reported without a heartbeat alert for {summ['avg_reporting']:.2f}% of the period on average "
                                 f"({summ['gap_count']} {c.plural(summ['gap_count'], 'gap')}, about {c.duration(summ['downtime_min'])} in total)."))
    if summ["bad_services"]:
        out.append(("Services", f"{len(summ['bad_services'])} watched {c.plural(len(summ['bad_services']), 'service')} "
                                f"{'is' if len(summ['bad_services']) == 1 else 'are'} not running right now."))
    if summ["alerts_critical"] or summ["alerts_warning"]:
        out.append(("Alerts", f"{summ['alerts_critical']} critical and {summ['alerts_warning']} warning alerts on host metrics in the period."))
    use = []
    if summ["disk_high"]:
        use.append(f"{len(summ['disk_high'])} with a disk above {DISK_WARN_PCT:.0f}% full")
    if summ["mem_high"]:
        use.append(f"{len(summ['mem_high'])} with average memory use above {MEM_WARN_PCT:.0f}%")
    if summ["cpu_high"]:
        use.append(f"{len(summ['cpu_high'])} with average CPU above {CPU_WARN_PCT:.0f}%")
    if use:
        out.append(("Capacity", "; ".join(use) + "."))
    if summ["usage_start"] and summ["usage_start"] > data["period_start"] + timedelta(days=1):
        out.append(("Data", f"Resource use is shown from {c.fmt_day(summ['usage_start'])}: metric history is kept for {HISTORY_DAYS} days."))
    return out


def recommendations(summ):
    recs = []
    for p in summ["offline"]:
        if p["host"].get("last_seen_at") is None:
            recs.append(f"{_name(p)} has enrolled but never checked in. Confirm the agent is running and can reach CloudOps.")
        else:
            recs.append(f"Investigate {_name(p)}: no check-in for {c.age_text(p['last_seen_age'])}. If the server was retired, revoke it on the Host Agents page.")
    for p in summ["stale"]:
        recs.append(f"{_name(p)} is late checking in ({c.age_text(p['last_seen_age'])}). Watch it; it becomes offline if it stays silent.")
    for p, s in summ["bad_services"][:10]:
        recs.append(f"Restart or fix the service {s['service_name']} on {_name(p)}: it is {s['state']}.")
    for p in summ["disk_high"]:
        d = p["use"]["disk"]
        recs.append(f"Free space or extend the disk on {_name(p)} ({d.get('label') or '/'}): it peaked at {d['max']:.0f}% full.")
    for p in summ["mem_high"]:
        recs.append(f"{_name(p)} averaged {p['use']['mem_used_percent']['avg']:.0f}% memory use. Check for a leak or size the server up.")
    for p in summ["cpu_high"]:
        recs.append(f"{_name(p)} averaged {p['use']['cpu_percent']['avg']:.0f}% CPU. Look at what is running or size the server up.")
    if summ["outdated"]:
        recs.append(f"Upgrade the agent to {summ['newest_version']} on: " + ", ".join(_name(p) for p in summ["outdated"][:8])
                    + (" and others." if len(summ["outdated"]) > 8 else "."))
    return recs


# ── PDF ──────────────────────────────────────────────────────────────

def _state_chip(state):
    return {"online": c.chip("ONLINE", kit.STATUS_COLORS["RESOLVED"]), "stale": c.chip("STALE", kit.SEVERITY_COLORS["WARNING"]),
            "offline": c.chip("OFFLINE", kit.SEVERITY_COLORS["CRITICAL"]), "revoked": c.chip("REVOKED", kit.MUTED)}[state]


def _pct(m):
    return f"{m:.0f}%" if m is not None else "-"


def render_host_pdf(*, report_type, scope_id, scope_label, period_start, period_end, data, generated_by):
    summ = summarize(data)
    title = "Host Agents Report"
    if not summ["hosts"]:
        status = ("NO HOSTS", kit.MUTED)
    elif summ["offline"]:
        status = ("OFFLINE HOSTS", kit.SEVERITY_COLORS["CRITICAL"])
    elif summ["stale"] or summ["bad_services"] or summ["gap_count"]:
        status = ("ATTENTION", kit.SEVERITY_COLORS["WARNING"])
    else:
        status = ("HEALTHY", kit.STATUS_COLORS["RESOLVED"])
    kpis = [
        {"label": "HOSTS", "value": f"{summ['active']}", "note": f"{summ['revoked']} revoked" if summ["revoked"] else "all active", "tone": "ink"},
        {"label": "ONLINE NOW", "value": f"{summ['online']} of {summ['active']}", "note": f"{len(summ['offline'])} offline" if summ["offline"] else "none offline",
         "tone": "crit" if summ["offline"] else ("warn" if summ["stale"] else "ok")},
        {"label": "HEARTBEAT GAPS", "value": f"{summ['gap_count']}", "note": f"{c.duration(summ['downtime_min'])} silent" if summ["gap_count"] else "none",
         "tone": "warn" if summ["gap_count"] else "ok"},
        {"label": "SERVICES DOWN", "value": f"{len(summ['bad_services'])}", "note": "watched, not running" if summ["bad_services"] else "none", "tone": "crit" if summ["bad_services"] else "ok"},
        {"label": "HOST ALERTS", "value": f"{summ['alerts_critical'] + summ['alerts_warning']}", "note": f"{summ['alerts_critical']} critical",
         "tone": "crit" if summ["alerts_critical"] else ("warn" if summ["alerts_warning"] else "ok")},
    ]
    account = data.get("account")
    rows = []
    if account:
        rows += [("Account", f"{account['account_name']} ({account['account_id']})"), ("Provider", (account.get("provider") or "aws").upper())]
    pdf, now = c.open_report(title=title, label=scope_label, report_type=report_type, period_start=period_start, period_end=period_end,
                             generated_by=generated_by, status=status, kpis=kpis, keywords="CloudOps, host agents, servers",
                             detail_rows=rows)
    kit.section_header(pdf, "Executive Summary")
    for lead, text in summary_paragraphs(summ, data):
        kit.paragraph(pdf, lead, text)

    if summ["per_host"]:
        kit.section_header(pdf, "Hosts")
        kit.data_table(pdf, [("Host", 40, "L"), ("OS", 20, "L"), ("Agent", 18, "L"), ("State", 24, "C"), ("Last seen", 22, "R"),
                             ("Reporting", 20, "R"), ("Gaps", 14, "R"), ("Alerts C/W", 22, "R")],
                       [(p["host"]["hostname"], p["host"].get("os") or "-", p["host"].get("agent_version") or "-", _state_chip(p["state"]),
                         c.age_text(p["last_seen_age"]) + (" ago" if p["last_seen_age"] is not None else ""),
                         c.pct_text(p["reporting"], 2), str(len(p["gaps"])), f"{p['alerts_critical']} / {p['alerts_warning']}") for p in summ["per_host"]])
        c.note(pdf, "Reporting is the share of the period with no open heartbeat alert; hosts enrolled during the period are measured from "
                    "enrolment. Alerts C/W counts critical and warning alerts on agent metrics (not heartbeat gaps).")

    with_use = [p for p in summ["per_host"] if p["use"]]
    if with_use:
        kit.section_header(pdf, "Resource Use")
        kit.data_table(pdf, [("Host", 40, "L"), ("CPU avg", 20, "R"), ("CPU peak", 20, "R"), ("Memory avg", 22, "R"),
                             ("Memory peak", 22, "R"), ("Fullest disk", 36, "R"), ("Swap peak", 20, "R")],
                       [(p["host"]["hostname"],
                         _pct(p["use"].get("cpu_percent", {}).get("avg")), _pct(p["use"].get("cpu_percent", {}).get("max")),
                         _pct(p["use"].get("mem_used_percent", {}).get("avg")), _pct(p["use"].get("mem_used_percent", {}).get("max")),
                         (f"{p['use']['disk']['max']:.0f}% ({p['use']['disk'].get('label') or '/'})" if p["use"].get("disk") else "-"),
                         _pct(p["use"].get("swap_used_percent", {}).get("max"))) for p in with_use])
        c.note(pdf, f"Averages and peaks of the readings stored from {c.fmt_day(summ['usage_start'])} to {c.fmt_day(data['period_end'])}. "
                    "On a host that is also an EC2 instance, memory and disk series may combine the agent and the CloudWatch agent.")

    kit.section_header(pdf, f"Heartbeat Gaps ({summ['gap_count']:,} in this period)")
    if summ["gaps"]:
        shown = summ["gaps"][:_MAX_GAP_ROWS]
        kit.data_table(pdf, [("Host", 52, "L"), ("Started (UTC)", 34, "L"), ("Ended (UTC)", 34, "L"), ("Duration", 28, "R"), ("State", 24, "C")],
                       [(g["host"]["hostname"], c.fmt_stamp(g["start"]), c.fmt_stamp(g["end"]) if g["end"] else "-", c.duration(g["minutes"]),
                         c.chip("OPEN" if g["open"] else "RESOLVED", kit.STATUS_COLORS["OPEN" if g["open"] else "RESOLVED"])) for g in shown])
        if summ["gap_count"] > len(shown):
            c.note(pdf, f"{summ['gap_count'] - len(shown):,} more gaps are in CloudOps.")
    else:
        kit.plain_paragraph(pdf, "No heartbeat gap in this period. A gap is recorded when a host stays silent past the watchdog's threshold.")

    if summ["bad_services"]:
        kit.section_header(pdf, f"Services Not Running ({len(summ['bad_services']):,})")
        kit.data_table(pdf, [("Host", 52, "L"), ("Service", 62, "L"), ("State", 24, "C"), ("Since (UTC)", 38, "L")],
                       [(p["host"]["hostname"], s["service_name"],
                         c.chip(str(s["state"]).upper(), kit.SEVERITY_COLORS["CRITICAL"] if s["state"] in ("failed", "stopped") else kit.MUTED),
                         c.fmt_stamp(s["state_since"])) for p, s in summ["bad_services"][:_MAX_SERVICE_ROWS]])
        c.note(pdf, "Only the services chosen for each host on the Host Agents page are watched; this is their latest state, not their history.")

    recs = recommendations(summ)
    kit.section_header(pdf, "Recommendations")
    if recs:
        for r in recs:
            kit.bullet(pdf, r)
    else:
        kit.bullet(pdf, "Nothing needs attention. Hosts are reporting, watched services are running and capacity is within reference limits.")
    c.note(pdf, f"Capacity limits used above (disk {DISK_WARN_PCT:.0f}%, memory {MEM_WARN_PCT:.0f}%, CPU {CPU_WARN_PCT:.0f}%) are reference values, "
                "not the alert thresholds configured in CloudOps.")
    return c.finish(pdf, scope_label, now, generated_by)


def build_pdf(job):
    """Entry point used by app/reports/registry.py. -> (pdf_bytes, scope_label)."""
    acct_id, host_id = parse_scope(job["scope_id"])
    if job["account_id"] is not None and int(job["account_id"]) != acct_id:
        raise ValueError("host report scope does not match its account")
    data = gather_host_data(acct_id, host_id, job["period_start"], job["period_end"])
    label = (data.get("account") or {}).get("account_name") or job["scope_id"]
    if host_id and data["hosts"]:
        label = f"{label} / {data['hosts'][0]['hostname']}"
    pdf = render_host_pdf(report_type=job["report_type"], scope_id=job["scope_id"], scope_label=label,
                          period_start=job["period_start"], period_end=job["period_end"], data=data, generated_by=job["requested_by"])
    return pdf, label
