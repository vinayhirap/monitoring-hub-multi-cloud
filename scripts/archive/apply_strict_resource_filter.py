#!/usr/bin/env python3
"""
apply_strict_resource_filter.py

Idempotent patch for monitoring-hub-multi-cloud. Run from the repo root
(after applying apply_alb_key_and_service_tile_fix.py):

    python apply_strict_resource_filter.py .

What it does
------------
frontend/src/pages/ServiceList.jsx
    Changes the Services page from "fail OPEN" to "fail HIDDEN" for
    resource counts. Previously a service tile stayed visible whenever
    its resource count was unknown (undefined/null) — e.g. a collector
    that threw an AccessDenied for that one service — on the theory
    that "unknown" shouldn't be treated the same as "confirmed zero".
    That meant tiles for services with genuinely zero resources could
    still show up whenever their specific collector failed instead of
    returning a clean 0.

    This patch makes the tile require a confirmed, positive
    (typeof count === "number" && count > 0) resource count to render
    at all, for AWS accounts. Non-AWS accounts (no resource-count data
    source yet) are unaffected and continue to show all enabled
    services. This is a deliberate trade-off: a flaky or
    under-permissioned collector for a specific AWS service will now
    hide that service's tile even if it secretly has resources —
    check the backend logs for a "resource-counts: <svc> failed"
    warning if an expected tile goes missing.

app/api/live_data.py
    Updates the comment above _RESOURCE_COLLECTORS to describe the new
    "hide unless confirmed" semantics (no functional change to the
    collector registry itself).

Safe to re-run: every edit is guarded, so running this twice on an
already-patched tree is a no-op.
"""
import sys
from pathlib import Path


def patch(path: Path, replacements, label):
    if not path.exists():
        print(f"  SKIP  {path} (not found)")
        return False
    text = path.read_text(encoding="utf-8")
    original = text
    for old, new, guard in replacements:
        if guard in text:
            continue  # already applied
        if old not in text:
            print(f"  WARN  {path}: expected snippet not found, skipping one edit")
            continue
        text = text.replace(old, new, 1)
    if text != original:
        path.write_text(text, encoding="utf-8")
        print(f"  OK    {path} ({label})")
        return True
    else:
        print(f"  SKIP  {path} (already patched or nothing to do)")
        return False


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    print(f"Applying patch to {root}\n")

    # 1. app/api/live_data.py — comment only
    p = root / "app/api/live_data.py"
    patch(p, [
        (
            '# Used by the Services page to decide whether a tile should be shown\n'
            '# at all — dynamically, based on whether the account actually HAS any\n'
            '# resources of that type right now, instead of only whether a metric\n'
            '# is selected for it. Covers every service with a live collector,\n'
            '# core (ec2/ebs/rds/lambda/s3/alb/ecs) and extended\n'
            '# (nlb/acm/backup/dms/directconnect/states) alike; the frontend\n'
            '# treats a still-missing key (a service with no collector at all)\n'
            '# as "unknown" and keeps that tile visible rather than hiding it on\n'
            '# a guess, but every service listed here gets a real 0/N.',
            '# Used by the Services page to decide whether a tile should be shown\n'
            '# at all — dynamically, based on whether the account actually HAS any\n'
            '# resources of that type right now, instead of only whether a metric\n'
            '# is selected for it. All 41 curated services (core + extended) have\n'
            '# a collector here. The frontend now treats anything OTHER than a\n'
            '# confirmed positive count — a missing key, a null from a failed/\n'
            '# unauthorized collector, or a real zero — as "hide this tile". That\n'
            '# means a collector that throws (e.g. AccessDenied on one service\'s\n'
            '# IAM permissions) will make its tile disappear even if the service\n'
            '# secretly has resources; check the "resource-counts: <svc> failed"\n'
            '# warning below if a tile you expect to see is missing.',
            'All 41 curated services (core + extended) have',
        ),
    ], "comment update")

    # 2. frontend/src/pages/ServiceList.jsx — behavior change
    p = root / "frontend/src/pages/ServiceList.jsx"
    patch(p, [
        (
            '  // Real per-service resource counts from AWS (core services only —\n'
            '  // see GET /api/live/resource-counts/{id}). null = not loaded yet;\n'
            '  // core tiles fail OPEN (stay visible) until we actually know a\n'
            '  // count is a confirmed zero — a slow/failed fetch never hides a\n'
            '  // tile that may well have real resources.',
            '  // Real per-service resource counts from AWS — see GET\n'
            '  // /api/live/resource-counts/{id}. null = not loaded yet (used only to\n'
            '  // avoid a flash of every tile before the first fetch resolves). Once\n'
            '  // loaded, a tile shows ONLY if we have a confirmed count > 0 for it.\n'
            '  // A missing/failed count (undefined, or a collector that threw — e.g.\n'
            '  // an AccessDenied on that one service) is treated the same as zero and\n'
            '  // hidden. This is a deliberate choice: it trades "never hide a tile\n'
            '  // that might have real resources" for "never show a tile that doesn\'t\n'
            '  // have any" — so a flaky/under-permissioned collector for one service\n'
            '  // will make that tile disappear rather than stay visible. If a tile\n'
            '  // you expect to see goes missing, check the backend logs for a\n'
            '  // "resource-counts: <svc> failed" warning — that\'s usually an IAM\n'
            '  // permissions gap on that specific service, not a real zero.',
            'trades "never hide a tile',
        ),
        (
            '    getResourceCounts(id).then(c => { if (!cancelled) setResourceCounts(c); }).catch(() => {});',
            '    getResourceCounts(id).then(c => { if (!cancelled) setResourceCounts(c ?? {}); }).catch(() => {});',
            'setResourceCounts(c ?? {})',
        ),
        (
            '  // Dynamic, aligned with the metric selector: a service tile only shows up\n'
            '  // here if it has at least one metric enabled for THIS account — the same\n'
            '  // selection made during onboarding or later edited in Settings -> Metrics.\n'
            '  // On top of that, ANY service (core or extended) is hidden if we have a\n'
            '  // real, confirmed-zero resource count for it — see GET\n'
            '  // /api/live/resource-counts/{id}, which now covers both tiers. A service\n'
            '  // still missing a collector (key absent from that response) or a\n'
            '  // non-AWS account (resourceCounts never populated) fails OPEN and stays\n'
            '  // visible, since "unknown" is not the same as "confirmed none".\n'
            '  const activeServices = useMemo(() => {\n'
            '    return groups\n'
            '      .filter(g => (g.metrics || []).some(m => m.enabled))\n'
            '      .filter(g => {\n'
            '        if (!resourceCounts) return true;\n'
            '        const count = resourceCounts[g.service];\n'
            '        return count === undefined || count === null || count > 0;\n'
            '      })',
            '  // Dynamic, aligned with the metric selector: a service tile only shows up\n'
            '  // here if it has at least one metric enabled for THIS account — the same\n'
            '  // selection made during onboarding or later edited in Settings -> Metrics\n'
            '  // — AND (for AWS accounts, once resourceCounts has loaded) a confirmed,\n'
            '  // positive resource count. No confirmed count => hidden. Non-AWS accounts\n'
            '  // have no resource-count data source at all, so they always show (nothing\n'
            '  // to check them against).\n'
            '  const activeServices = useMemo(() => {\n'
            '    const isAws = provider === "aws";\n'
            '    return groups\n'
            '      .filter(g => (g.metrics || []).some(m => m.enabled))\n'
            '      .filter(g => {\n'
            '        if (!isAws) return true;          // no resource-count data for GCP/Azure\n'
            '        if (!resourceCounts) return true; // still loading — avoid a flash of nothing\n'
            '        const count = resourceCounts[g.service];\n'
            '        return typeof count === "number" && count > 0;\n'
            '      })',
            'const isAws = provider === "aws";',
        ),
        (
            '  }, [groups, resourceCounts]);',
            '  }, [groups, resourceCounts, provider]);',
            '[groups, resourceCounts, provider]',
        ),
    ], "strict resource-count filtering")

    print("\nDone.")


if __name__ == "__main__":
    main()
