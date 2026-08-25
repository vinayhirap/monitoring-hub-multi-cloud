#!/usr/bin/env python3
"""
apply_core_only_tiles.py

Idempotent patch for monitoring-hub-multi-cloud. Run from the repo root:

    python apply_core_only_tiles.py .

What it does
------------
frontend/src/pages/ServiceList.jsx
    For AWS accounts, the Services page now shows ONLY tiles that are
    both (a) a core AWS service with a real internal detail page
    (CORE_AWS_SERVICES: ec2/ebs/rds/lambda/s3/elb/alb/ecs) and
    (b) have a confirmed positive resource count. Every "extended"
    service — the ones that only ever opened a "VIEW IN CONSOLE"
    link out to the AWS Console instead of a real internal page — is
    now dropped from this page entirely, regardless of its resource
    count. No more console-link placeholder tiles.

    Non-AWS (GCP/Azure) accounts are unaffected — they have no
    resource-count data source yet, so they continue to show every
    enabled service as before.

Safe to re-run: the edit is guarded, so running this twice on an
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

    p = root / "frontend/src/pages/ServiceList.jsx"
    patch(p, [
        (
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
            '  // Dynamic, aligned with the metric selector: a service tile only shows up\n'
            '  // here if it has at least one metric enabled for THIS account — the same\n'
            '  // selection made during onboarding or later edited in Settings -> Metrics\n'
            '  // — AND (for AWS accounts) BOTH a confirmed positive resource count AND a\n'
            '  // real internal detail page (CORE_AWS_SERVICES) it can open into. AWS\n'
            '  // services with no detail page only ever had a "VIEW IN CONSOLE" tile\n'
            '  // that sends you off to the AWS Console — that\'s been dropped entirely,\n'
            '  // on purpose: this page now only shows tiles you can click straight into\n'
            '  // with real data behind them, never a console-link placeholder. Non-AWS\n'
            '  // accounts have no resource-count data source yet, so they always show.\n'
            '  const activeServices = useMemo(() => {\n'
            '    const isAws = provider === "aws";\n'
            '    return groups\n'
            '      .filter(g => (g.metrics || []).some(m => m.enabled))\n'
            '      .filter(g => {\n'
            '        if (!isAws) return true;                    // no resource-count data for GCP/Azure\n'
            '        if (!CORE_AWS_SERVICES.has(g.service)) return false; // no console-link tiles, ever\n'
            '        if (!resourceCounts) return true;            // still loading — avoid a flash of nothing\n'
            '        const count = resourceCounts[g.service];\n'
            '        return typeof count === "number" && count > 0;\n'
            '      })',
            'no console-link tiles, ever',
        ),
    ], "drop all VIEW IN CONSOLE tiles for AWS accounts")

    print("\nDone.")


if __name__ == "__main__":
    main()
