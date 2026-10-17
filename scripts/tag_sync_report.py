#!/usr/bin/env python3
"""
Tag-sync verification report (workstream B). READ-ONLY: it never writes to the database and never writes to AWS.

Run it on PROD BEFORE turning TAG_SYNC_ENABLED on. It does what the sync would do, prints the outcome and writes
nothing. Per resource type it shows how many rows there are, how many matched a tagged ARN, how many would be
called untagged, how many stay unknown, the ARNs the sweep returned that matched no row, the rows no ARN matched,
and the resulting tag_state distribution. A type is safe to rely on when its unmatched rows and unmatched ARNs are
explained (resources that really are not tagged show up as "absent", not as unmatched).

    cd /opt/monitoring-hub/app
    /opt/monitoring-hub/venv/bin/python3 scripts/tag_sync_report.py                  # live: DB read + GetResources
    /opt/monitoring-hub/venv/bin/python3 scripts/tag_sync_report.py --account-id 7
    /opt/monitoring-hub/venv/bin/python3 scripts/tag_sync_report.py --show 50        # list more unmatched items
    /opt/monitoring-hub/venv/bin/python3 scripts/tag_sync_report.py --json

    # No AWS credentials, no database: run the same logic on a recorded sweep (also how it is tested)
    python3 scripts/tag_sync_report.py --fixtures tests/fixtures/tag_sync_report_fixture.json

Live mode does not need migration 095. It uses only columns that already exist.
A fixtures file is JSON: {"account_name", "account_number", "rows": [{id, resource_type, resource_id, region, tags}],
"sweeps": [{"region", "ok", "error", "items": [{"arn", "tags": {}}]}]}.
"""
import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from app import environment_classifier as ec  # noqa: E402
from app.aws import arn_match  # noqa: E402

STATES = ("ok", "nonstandard", "invalid", "missing", "not_checked")


def build_report(rows, sweeps, account_number, absence_types=None):
    """Pure: rows + sweeps -> report dict (what the sync would decide, nothing applied)."""
    plan = arn_match.plan_account(rows, sweeps, account_number,
                                  arn_match.ABSENCE_VERIFIED_TYPES if absence_types is None else absence_types)
    by_type = {}
    for r in rows:
        t = str(r.get("resource_type") or "").lower()
        d = plan["decisions"].get(r["id"])
        state = ec.tag_state(t, r.get("tags"), d.tags if d is not None else None)
        slot = by_type.setdefault(t, {s: 0 for s in STATES})
        slot[state] = slot.get(state, 0) + 1
    types = {}
    for t, stats in plan["types"].items():
        types[t] = dict(stats, tag_state=by_type.get(t, {}), may_claim_untagged=t in arn_match.ABSENCE_VERIFIED_TYPES)
    return {
        "sweeps": [{"region": s["region"], "scope": s.get("scope") or "full", "ok": bool(s.get("ok")), "resources": len(s.get("items") or []),
                    "pages": s.get("pages"), "error": str(s["error"]) if s.get("error") else None} for s in sweeps],
        "types": types,
        "other_types_tag_state": {t: v for t, v in by_type.items() if t not in plan["types"]},
        "foreign_arns": plan["foreign_arns"], "unsupported_arns": plan["unsupported_arns"],
    }


def render(account_name, rep, show=20):
    out = [f"=== {account_name} ==="]
    for s in rep["sweeps"]:
        out.append(f"sweep {s['region']:<14} {s['scope']:<7} {'ok' if s['ok'] else 'FAILED'}  resources={s['resources']}  pages={s['pages']}"
                   + (f"  error={s['error']}" if s["error"] else ""))
    out.append(f"ARNs of types the sync does not handle (ignored): {rep['unsupported_arns']}   "
               f"ARNs of other accounts (ignored): {len(rep['foreign_arns'])}")
    out.append("")
    out.append(f"{'type':<20}{'rows':>5}{'matched':>8}{'absent':>7}{'unknown':>8}{'badrows':>8}{'badarns':>8}   tag_state   (claims untagged?)")
    for t in sorted(rep["types"]):
        x = rep["types"][t]
        dist = " ".join(f"{k}={v}" for k, v in x["tag_state"].items() if v)
        out.append(f"{t:<20}{x['rows']:>5}{x['matched']:>8}{x['absent']:>7}{x['unknown']:>8}"
                   f"{len(x['unmatched_rows']):>8}{len(x['unmatched_arns']):>8}   {dist or '-'}   "
                   f"({'yes' if x['may_claim_untagged'] else 'never, type not verified'})")
    for t in sorted(rep["types"]):
        x = rep["types"][t]
        if not (x["unmatched_rows"] or x["unmatched_arns"] or x["ambiguous"] or x["absence_blocked"]):
            continue
        out.append("")
        out.append(f"-- {t}")
        for reason in x["absence_blocked"]:
            out.append(f"   not called untagged because: {reason}")
        for a in x["ambiguous"][:show]:
            out.append(f"   ambiguous: {a}")
        for pk, rid in x["unmatched_rows"][:show]:
            out.append(f"   row not matched: id={pk} resource_id={rid}")
        if len(x["unmatched_rows"]) > show:
            out.append(f"   ... {len(x['unmatched_rows']) - show} more rows")
        for a in x["unmatched_arns"][:show]:
            out.append(f"   ARN not matched to a row: {a}")
        if len(x["unmatched_arns"]) > show:
            out.append(f"   ... {len(x['unmatched_arns']) - show} more ARNs")
    if rep["other_types_tag_state"]:
        out.append("")
        out.append("other types (tag_state from the tags discovery already stores, unchanged by the sync): "
                   + "; ".join(f"{t}: " + " ".join(f"{k}={v}" for k, v in d.items() if v)
                               for t, d in sorted(rep["other_types_tag_state"].items())))
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------
def _live(account_id):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
    from app.db import get_connection
    from app.collector import tag_sync
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        sql = ("SELECT id, account_name, account_id, role_arn, auth_mode, external_id, default_region "
               "FROM aws_accounts WHERE status = 'active' AND provider = 'aws'")
        params = []
        if account_id is not None:
            sql += " AND id = %s"
            params.append(account_id)
        cur.execute(sql, params)
        accounts = cur.fetchall()
        result = []
        types = sorted(arn_match.SYNC_TYPES)
        for a in accounts:
            # the sync's own row filter, without the migration-095 columns
            cur.execute("SELECT r.id, r.resource_type, r.resource_id, r.region, r.tags FROM resources r "
                        "WHERE r.aws_account_id = %s AND (r.instance_state IS NULL OR r.instance_state != 'terminated') "
                        "AND (r.last_seen_at IS NULL OR r.last_seen_at >= DATE_SUB(NOW(), INTERVAL %s HOUR)) "
                        "AND r.resource_type IN (" + ", ".join(["%s"] * len(types)) + ")",
                        [a["id"], tag_sync.STALE_RESOURCE_HOURS, *types])
            result.append((a, cur.fetchall()))
        conn.commit()
    finally:
        conn.close()
    for a, rows in result:
        number = str(a.get("account_id") or "").strip()
        if not number:
            print(f"{a['account_name']}: no AWS account number on the account row, skipped")
            continue
        session = tag_sync._get_session(a)
        if session is None:
            print(f"{a['account_name']}: no AWS session, skipped")
            continue
        sweeps = [tag_sync.sweep_region(session, sp["region"], sp["filters"], sp["scope"])
                  for sp in tag_sync.sweep_plan(a, rows)]
        yield a["account_name"], rows, sweeps, number


def _fixtures(path):
    with open(path) as f:
        data = json.load(f)
    yield data.get("account_name", "fixture"), data["rows"], data["sweeps"], str(data["account_number"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixtures", metavar="FILE", help="run on a recorded sweep: no AWS, no database")
    ap.add_argument("--account-id", type=int, default=None, help="limit to one aws_accounts.id (live mode)")
    ap.add_argument("--show", type=int, default=20, help="how many unmatched items to list per type (default 20)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    source = _fixtures(args.fixtures) if args.fixtures else _live(args.account_id)
    out = {}
    first = True
    for name, rows, sweeps, number in source:
        rep = build_report(rows, sweeps, number)
        out[name] = rep
        if not args.json:
            if not first:
                print("")
            print(render(name, rep, args.show))
            first = False
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print("\nREAD-ONLY: nothing was written to the database or to AWS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
