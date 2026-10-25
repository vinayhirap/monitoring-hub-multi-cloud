# app/environment_governance.py
"""
Pure helpers for the environment governance features (workstream C). No imports beyond the stdlib, so the API,
the collector hooks and the unit tests share one definition. The queries that feed these live in
app/env_governance_db.py.

  default_change_summary / alert_impact   what changing an account default does (preview, nothing written)
  tally_compliance                        tag_state / decision-source percentages per account and resource type
  csv_text / csv_cell                     CSV output that spreadsheet programs cannot execute
  iso_week_key / digest_text              the weekly digest of new unknown resources
"""
import csv
import io
from collections import Counter

from app.env_tiers import is_nonprod, to_tier

TAG_BUCKETS = ("ok", "nonstandard", "invalid", "missing", "not_checked", "pending")
SOURCE_BUCKETS = ("manual", "tag", "name", "related", "account", "unknown", "pending")
CHECKABLE = ("ok", "nonstandard", "invalid", "missing")      # not_checked and pending are NOT in here, on purpose
PRIORITY_LABEL = {1: "P1", 2: "P2", 3: "P3", 4: "P4"}

# Same matrix as docs/PRIORITY_AND_ROUTING.md (PROD critical=P1, warning=P2; NON-PROD critical=P3, warning=P4). It
# ignores the Criticality=critical tag bump, which needs the resource's tags; the preview says so.
_PRIORITY = {("prod", True): 1, ("prod", False): 2, ("nonprod", True): 3, ("nonprod", False): 4}


def _prod_like(env):
    return not is_nonprod(env)


def priority_for(env, is_critical):
    return _PRIORITY[(to_tier(env), bool(is_critical))]


def pct(n, total):
    return round(100.0 * n / total, 1) if total else None


# ---- account default preview ---------------------------------------------------------------
def alert_impact(changes, alerts, *, flag_on, priority_on):
    """What the re-labelling does to alerts that are open right now.

    changes   {resource_id: (old_env, new_env)} for the rows whose environment changes (None = not evaluated yet)
    alerts    iterable of dicts with resource_id and severity (open alerts of the account)
    Severity and priority only move when ENV_AWARE_CLASSIFICATION is on (flag_on); with it off nothing about an
    alert changes and the answer says so. CRITICAL never changes severity; a warning-level alert is WARNING on a
    PROD resource and INFO on NON-PROD (environment_classifier.severity_for).
    """
    out = {"flag_on": bool(flag_on), "priority_on": bool(priority_on and flag_on), "open_alerts_affected": 0,
           "warning_to_info": 0, "info_to_warning": 0, "priority_shifts": []}
    if not flag_on:
        return out
    shifts = Counter()
    for a in alerts:
        pair = changes.get(a.get("resource_id"))
        if not pair:
            continue
        old, new = pair
        crit = str(a.get("severity") or "").upper() == "CRITICAL"
        moved = False
        if not crit:
            if _prod_like(old) and not _prod_like(new):
                out["warning_to_info"] += 1; moved = True
            elif not _prod_like(old) and _prod_like(new):
                out["info_to_warning"] += 1; moved = True
        if out["priority_on"]:
            p0, p1 = priority_for(old, crit), priority_for(new, crit)
            if p0 != p1:
                shifts[(p0, p1)] += 1; moved = True
        if moved:
            out["open_alerts_affected"] += 1
    out["priority_shifts"] = [{"from": PRIORITY_LABEL[a], "to": PRIORITY_LABEL[b], "count": n}
                              for (a, b), n in sorted(shifts.items())]
    return out


def default_change_summary(report, changes, current_default, new_default, alerts, *, flag_on, priority_on):
    """Shape the env_sync dry-run report into the preview the confirmation dialog shows.

    report   sync_resource_environments(..., dry_run=True, account_default_override=...) result
    changes  [{resource_id, old_env, old_source, new_env, new_source}] for the rows that would be written
    """
    moves = Counter()
    ids = {}
    env_changes = 0
    for c in changes:
        old_env, new_env = c["old_env"], c["new_env"]
        if old_env != new_env or c["old_source"] != c["new_source"]:
            env_changes += 1
        moves[(old_env or "unclassified", new_env)] += 1
        ids[c["resource_id"]] = (old_env, new_env)
    return {
        "current_default": current_default, "new_default": new_default,
        "total": report["total"],
        # what the PUT will report as "reclassified": rows actually written, identical definition (dry run of the same code)
        "rows_rewritten": report["changed"],
        # the subset a person cares about: the environment or the way it was decided changes
        "environment_changes": env_changes,
        "by_env": dict(report["by_env"]), "by_source": dict(report["by_source"]),
        "moves": [{"from": a, "to": b, "count": n} for (a, b), n in sorted(moves.items())],
        "alerts": alert_impact(ids, alerts, flag_on=flag_on, priority_on=priority_on),
        "ignores_criticality_tag": True,
    }


# ---- compliance ----------------------------------------------------------------------------
def _block(c):
    """Counter-like of tag_<bucket> / src_<bucket> / total / warnings -> the report shape. Percentages are of ALL
    resources in the group, so the tag buckets sum to 100 and not_checked stays visible as its own bucket."""
    total = int(c.get("total", 0))
    tags = {k: {"n": int(c.get(f"tag_{k}", 0)), "pct": pct(int(c.get(f"tag_{k}", 0)), total)} for k in TAG_BUCKETS}
    srcs = {k: {"n": int(c.get(f"src_{k}", 0)), "pct": pct(int(c.get(f"src_{k}", 0)), total)} for k in SOURCE_BUCKETS}
    checkable = sum(tags[k]["n"] for k in CHECKABLE)
    return {"total": total, "by_tag_state": tags, "by_source": srcs, "warnings": int(c.get("warnings", 0)),
            "checkable": checkable, "ok_of_checkable_pct": pct(tags["ok"]["n"], checkable)}


def tally_compliance(rows):
    """rows: dicts with account_id, account_name, provider, resource_type, tag_state, env_source, warning.
    NULL tag_state / env_source mean "not evaluated yet" and go to the `pending` bucket, never to missing/unknown.
    -> {"totals": block, "accounts": [{account_id, account_name, provider, ..block.., "types": [{resource_type, ..block..}]}]}"""
    per = {}
    names = {}
    for r in rows:
        key = (r["account_id"], r["resource_type"])
        names[r["account_id"]] = (r.get("account_name"), r.get("provider") or "aws")
        c = per.setdefault(key, Counter())
        c["total"] += 1
        ts = r.get("tag_state") if r.get("tag_state") in TAG_BUCKETS else "pending"
        src = r.get("env_source") if r.get("env_source") in SOURCE_BUCKETS else "pending"
        c[f"tag_{ts}"] += 1
        c[f"src_{src}"] += 1
        if r.get("warning"):
            c["warnings"] += 1
    return _assemble(per, names)


def _assemble(per, names):
    accounts = {}
    grand = Counter()
    for (aid, rtype), c in per.items():
        grand.update(c)
        acc = accounts.setdefault(aid, {"counter": Counter(), "types": []})
        acc["counter"].update(c)
        acc["types"].append(dict(_block(c), resource_type=rtype))
    out = []
    for aid, acc in accounts.items():
        name, provider = names.get(aid, (None, "aws"))
        out.append(dict(_block(acc["counter"]), account_id=aid, account_name=name, provider=provider,
                        types=sorted(acc["types"], key=lambda t: (-t["total"], t["resource_type"]))))
    out.sort(key=lambda a: (a["account_name"] or "", a["account_id"]))
    return {"totals": _block(grand), "accounts": out}


def compliance_from_snapshot_rows(rows, names):
    """Same shape from stored snapshot rows (columns total, tag_*, src_*, warnings)."""
    per = {}
    for r in rows:
        per[(r["aws_account_id"], r["resource_type"])] = {k: v for k, v in r.items()}
    return _assemble(per, names)


def compliance_csv_rows(report):
    """Flat rows for the CSV export: one per account x resource type, plus a TOTAL row per account."""
    head = ["account", "provider", "resource_type", "total"]
    for k in TAG_BUCKETS:
        head += [f"tag_{k}", f"tag_{k}_pct"]
    for k in SOURCE_BUCKETS:
        head += [f"decided_by_{k}", f"decided_by_{k}_pct"]
    head += ["checkable", "ok_of_checkable_pct", "mismatch_warnings"]

    def line(account, provider, rtype, b):
        row = [account, provider, rtype, b["total"]]
        for k in TAG_BUCKETS:
            row += [b["by_tag_state"][k]["n"], b["by_tag_state"][k]["pct"]]
        for k in SOURCE_BUCKETS:
            row += [b["by_source"][k]["n"], b["by_source"][k]["pct"]]
        return row + [b["checkable"], b["ok_of_checkable_pct"], b["warnings"]]

    body = []
    for a in report["accounts"]:
        for t in a["types"]:
            body.append(line(a["account_name"], a["provider"], t["resource_type"], t))
        body.append(line(a["account_name"], a["provider"], "ALL TYPES", a))
    return head, body


# ---- CSV -----------------------------------------------------------------------------------
def csv_cell(v):
    """Neutralise spreadsheet formulas. A cell that starts with = + - @ (or a tab / CR) is read as a formula by Excel
    and Sheets; resource names and tag values come from AWS and are not trusted, so prefix an apostrophe."""
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def csv_text(head, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(head)
    for r in rows:
        w.writerow([csv_cell(c) for c in r])
    return buf.getvalue()


# ---- weekly digest -------------------------------------------------------------------------
def iso_week_key(dt):
    """datetime -> '2026W42' (8 chars, ISO year and week)."""
    y, w, _ = dt.isocalendar()
    return f"{y}W{w:02d}"


DIGEST_LIST_MAX = 25


def digest_text(account_name, new_items, total_unknown, url):
    """Plain text body. new_items: [{name, resource_type, region}] (new unknown since the last digest, already
    capped by the caller); the line counts say how many were not listed. Names and types only: no tags, no
    account numbers."""
    shown = new_items[:DIGEST_LIST_MAX]
    lines = [f"CloudOps: {len(new_items)} new resource(s) in {account_name} could not be placed in an environment.",
             "They are alerted as if they were Prod until someone places them.", ""]
    for it in shown:
        lines.append(f"  - {it.get('name') or '(no name)'} ({it.get('resource_type')}"
                     + (f", {it['region']}" if it.get("region") else "") + ")")
    if len(new_items) > len(shown):
        lines.append(f"  ... and {len(new_items) - len(shown)} more")
    lines += ["", f"{total_unknown} resource(s) in this account are waiting in total.", f"Place them here: {url}"]
    return "\n".join(lines)
