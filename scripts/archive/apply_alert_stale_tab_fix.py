#!/usr/bin/env python3
"""
apply_alert_stale_tab_fix.py

Follow-up to apply_alert_evaluation_hardening_code_fix.py (run that one
first — this expects its edits, e.g. the `a.stale` field and the
"stale — no data" badge, to already be in frontend/src/pages/Alerts.jsx).

What changed: a stale flag next to an alert still sitting in the Active
tab is easy to miss / still clutters the view you actually watch. This
moves stale alerts (status='active' but a.stale=true) OUT of the Active
tab entirely and into a new dedicated "Stale" tab, so:

  - Active tab  = confirmed-live breaches only (fresh data, currently
    breaching)
  - Stale tab   = status is still 'active' in the DB (nothing has been
    auto-resolved — see 008_revert_falsely_resolved_alerts.sql for why
    that's deliberately avoided), but no fresh metric has arrived
    recently, so it needs a human to check whether the resource still
    exists / the collector is still working for it.

Nothing server-side changes here — this is a pure frontend filter/tab
change, `a.stale` is already computed by the API.

Usage:
    python apply_alert_stale_tab_fix.py [repo_root]
"""
import shutil
import sys
from pathlib import Path

BAK_SUFFIX = ".bak.pre-stale-tab-fix"
MARKER = 'tab === "stale"'


class PatchError(Exception):
    pass


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def backup(path: Path):
    bak = path.with_name(path.name + BAK_SUFFIX)
    if not bak.exists():
        shutil.copy2(path, bak)


def apply_replacements(path: Path, replacements, already_applied_marker=None):
    text = read(path)
    if already_applied_marker and already_applied_marker in text:
        print(f"  SKIP  {path} (already patched)")
        return False

    backup(path)
    for old, new, label in replacements:
        count = text.count(old)
        if count == 0:
            raise PatchError(f"{path}: pattern not found for '{label}'")
        if count > 1:
            raise PatchError(f"{path}: pattern for '{label}' matches {count} times, expected 1")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    print(f"  OK    {path} ({len(replacements)} edit{'s' if len(replacements) != 1 else ''})")
    return True


def main():
    repo_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    print(f"Repo root: {repo_root}")

    p = repo_root / "frontend" / "src" / "pages" / "Alerts.jsx"
    if not p.exists():
        print(f"ABORTED: {p} not found", file=sys.stderr)
        sys.exit(1)

    replacements = [
        (
            '  const filtered = alerts.filter(a => {\n'
            '    const s = (a.status || "").toLowerCase();\n'
            '    if (tab === "active"       && s !== "active")       return false;\n'
            '    if (tab === "critical"     && (a.severity || "").toUpperCase() !== "CRITICAL") return false;\n'
            '    if (tab === "acknowledged" && s !== "acknowledged") return false;\n'
            '    if (tab === "resolved"     && s !== "resolved")     return false;\n'
            '    if (search) {\n'
            '      const q = search.toLowerCase();\n'
            '      return (\n'
            '        (a.metric_name || "").toLowerCase().includes(q) ||\n'
            '        (a.resource    || "").toLowerCase().includes(q) ||\n'
            '        (a.severity    || "").toLowerCase().includes(q)\n'
            '      );\n'
            '    }\n'
            '    return true;\n'
            '  });\n'
            '\n'
            '  const counts = {\n'
            '    all:          alerts.length,\n'
            '    active:       alerts.filter(a => (a.status || "").toLowerCase() === "active").length,\n'
            '    critical:     alerts.filter(a => (a.severity || "").toUpperCase() === "CRITICAL").length,\n'
            '    acknowledged: alerts.filter(a => (a.status || "").toLowerCase() === "acknowledged").length,\n'
            '    resolved:     alerts.filter(a => (a.status || "").toLowerCase() === "resolved").length,\n'
            '  };',

            '  const filtered = alerts.filter(a => {\n'
            '    const s = (a.status || "").toLowerCase();\n'
            '    // "Active" means confirmed live — a resource still sending fresh data\n'
            '    // that\'s breaching right now. Stale ones (no fresh data in 20+ min)\n'
            '    // move to their own tab so they don\'t clutter the feed you actually\n'
            '    // watch, without silently resolving/hiding them.\n'
            '    if (tab === "active"       && (s !== "active" || a.stale)) return false;\n'
            '    if (tab === "stale"        && (s !== "active" || !a.stale)) return false;\n'
            '    if (tab === "critical"     && (a.severity || "").toUpperCase() !== "CRITICAL") return false;\n'
            '    if (tab === "acknowledged" && s !== "acknowledged") return false;\n'
            '    if (tab === "resolved"     && s !== "resolved")     return false;\n'
            '    if (search) {\n'
            '      const q = search.toLowerCase();\n'
            '      return (\n'
            '        (a.metric_name || "").toLowerCase().includes(q) ||\n'
            '        (a.resource    || "").toLowerCase().includes(q) ||\n'
            '        (a.severity    || "").toLowerCase().includes(q)\n'
            '      );\n'
            '    }\n'
            '    return true;\n'
            '  });\n'
            '\n'
            '  const counts = {\n'
            '    all:          alerts.length,\n'
            '    active:       alerts.filter(a => (a.status || "").toLowerCase() === "active" && !a.stale).length,\n'
            '    stale:        alerts.filter(a => (a.status || "").toLowerCase() === "active" && a.stale).length,\n'
            '    critical:     alerts.filter(a => (a.severity || "").toUpperCase() === "CRITICAL").length,\n'
            '    acknowledged: alerts.filter(a => (a.status || "").toLowerCase() === "acknowledged").length,\n'
            '    resolved:     alerts.filter(a => (a.status || "").toLowerCase() === "resolved").length,\n'
            '  };',
            "Alerts.jsx: exclude stale from Active, add stale count",
        ),
        (
            '        {[\n'
            '          ["all",          "All"],\n'
            '          ["active",       "Active"],\n'
            '          ["critical",     "Critical"],\n'
            '          ["acknowledged", "Acknowledged"],\n'
            '          ["resolved",     "Resolved"],\n'
            '        ].map(([key, label]) => (',

            '        {[\n'
            '          ["all",          "All"],\n'
            '          ["active",       "Active"],\n'
            '          ["stale",        "Stale"],\n'
            '          ["critical",     "Critical"],\n'
            '          ["acknowledged", "Acknowledged"],\n'
            '          ["resolved",     "Resolved"],\n'
            '        ].map(([key, label]) => (',
            "Alerts.jsx: add Stale tab",
        ),
    ]

    try:
        changed = apply_replacements(p, replacements, already_applied_marker=MARKER)
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        bak = p.with_name(p.name + BAK_SUFFIX)
        if bak.exists():
            shutil.copy2(bak, p)
            print(f"  reverted {p}")
        sys.exit(1)

    if changed:
        print("\nDone. Your dev server will hot-reload this automatically —")
        print("just refresh the Alerts page. Active now only shows confirmed-live")
        print("breaches; stale ones live under the new Stale tab.")
    else:
        print("Nothing to do — already patched.")


if __name__ == "__main__":
    main()
