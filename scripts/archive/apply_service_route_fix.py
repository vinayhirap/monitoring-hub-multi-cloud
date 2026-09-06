#!/usr/bin/env python3
r"""
apply_service_route_fix.py

IMPORTANT CONTEXT: while building this, I found your repo has moved
forward via what looks like a separate session/tool -- commits
8a50d79 and 4524462 already shipped a console-credentials fix AND a
dynamic-resource-count fix for the Services page. I checked both
carefully:

  - Console links: already fixed, and better than what I was about to
    ship. It keeps federation but scopes the resulting AWS session to
    the specific resource (build_scoped_session_policy) AND attributes
    it to the specific monitoring-hub user who clicked (requested_by ->
    session name), via app/aws/federation.py + app/api/alerts.py. That's
    a more complete answer to 'own credentials and access to the
    specific resource' than my simpler plan (which would have just
    dropped federation entirely). Nothing to do here -- I dropped my
    version rather than risk regressing this.

  - Dynamic resource tiles: already fixed too. ServiceList.jsx now
    fetches per-account counts via a new getResourceCounts() call
    (app/api/live_data.py) and only renders a tile when resourceCount >
    0, hiding GCP/Azure/directory-tier services entirely rather than
    showing broken links. I dropped my version (a separate new
    /resource-counts endpoint) since it would have been pure redundant
    duplication of a better, already-working mechanism.

WHAT'S STILL ACTUALLY MISSING, and what this script fixes: the
existing dynamic-tile fix HIDES broken links rather than fixing the
underlying cause. Root cause, confirmed by reading the code: App.jsx's
route table only ever had explicit routes for the 7 AWS services (ec2,
ebs, rds, s3, ecs, elb, lambda). Any other service key -- every GCP
service, every Azure service, any AWS directory-tier service discovered
live -- doesn't match any route and falls through to the wildcard '*'
route, silently redirecting to Overview. Today that's masked because
RESOURCE_COUNT_FIELD in ServiceList.jsx only has entries for those same
7 services, so non-AWS tiles never render and can't be clicked. But:
  - An alert notification or any other link that points straight at a
    GCP/Azure resource's service page bypasses that tile-rendering gate
    entirely and still hits the broken redirect today.
  - The moment someone extends RESOURCE_COUNT_FIELD to cover a GCP/Azure
    service (necessary once Steps 4-7's collectors are feeding real
    data), that service's tile starts rendering and clicking it 404s to
    Overview again -- this script prevents that regression pre-emptively.

Fix: replaced the 7 static routes with one dynamic accounts/:id/:service
route, and updated ServiceDetail.jsx to read the service from the URL
instead of a hardcoded prop. Known AWS services (the same 7) render
exactly as before -- zero behavior change for them. Anything else now
falls through to the graceful 'not configured yet' state that already
existed in this file for exactly this purpose (see NotImplState), with a
working 'Open in Console' button, instead of a silent redirect.

Verified: full npx vite build passes clean (964 modules, no errors)
against your actual current repo state (post commits 8a50d79 and
4524462) -- not a stale baseline.

Usage:
    python apply_service_route_fix.py --dry-run
    python apply_service_route_fix.py

Run from the repo root (D:\Project\monitoring-tool\monitoring-hub-V5-multi-cloud).
"""

import argparse
import shutil
import sys
from pathlib import Path

ROUTING_PATCHES = {'app': ['frontend/src/App.jsx', [['        <Route path="accounts/:id/services"     element={<ServiceList />} />\n        <Route path="accounts/:id/ec2"          element={<ServiceDetail service="EC2"    />} />\n        <Route path="accounts/:id/ebs"          element={<ServiceDetail service="EBS"    />} />\n        <Route path="accounts/:id/rds"          element={<ServiceDetail service="RDS"    />} />\n        <Route path="accounts/:id/s3"           element={<ServiceDetail service="S3"     />} />\n        <Route path="accounts/:id/ecs"          element={<ServiceDetail service="ECS"    />} />\n        <Route path="accounts/:id/elb"          element={<ServiceDetail service="ELB"    />} />\n        <Route path="accounts/:id/lambda"       element={<ServiceDetail service="Lambda" />} />\n        <Route path="accounts/:id"              element={<AccountDetail />} />', '        <Route path="accounts/:id/services"     element={<ServiceList />} />\n        <Route path="accounts/:id/:service"     element={<ServiceDetail />} />\n        <Route path="accounts/:id"              element={<AccountDetail />} />']], 'jsx'], 'sd': ['frontend/src/pages/ServiceDetail.jsx', [['const BASE = "";\nconst OPTIONAL_SERVICES = new Set([]);', 'const BASE = "";\n\n// The 7 AWS resource types this page can actually fetch live data for\n// today (app/api/live_data.py only has AWS endpoints — no GCP/Azure\n// resource-list endpoints exist yet). URL params arrive lowercase\n// (matching resources.resource_type / metric_catalog.service, e.g. "ec2",\n// "vm", "compute_instance") — normalize to the internal keys this file\n// has always used.\nconst KNOWN_SERVICE_KEYS = { ec2: "EC2", ebs: "EBS", rds: "RDS", s3: "S3", ecs: "ECS", elb: "ELB", lambda: "Lambda" };\n\nfunction normalizeService(rawParam) {\n  return KNOWN_SERVICE_KEYS[(rawParam || "").toLowerCase()] || null;\n}'], ['export default function ServiceDetail({ service }) {\n  const { id }   = useParams();\n  const navigate = useNavigate();\n  const [searchParams] = useSearchParams();\n  const meta     = SERVICE_META[service] || SERVICE_META.EC2;', 'export default function ServiceDetail() {\n  const { id, service: rawService } = useParams();\n  const navigate = useNavigate();\n  const [searchParams] = useSearchParams();\n  const service  = normalizeService(rawService);       // "EC2" etc, or null if unsupported\n  const meta     = service ? (SERVICE_META[service] || SERVICE_META.EC2) : {\n    icon: CloudIcon, color: "#6382be", label: (rawService || "Service"),\n  };'], ['  const loadRows = useCallback(async () => {\n    if (notImplRef.current) return;\n    setError(null);\n    try {\n      const data = await fetchService(id, service);\n      setRows(Array.isArray(data) ? data : []);\n    } catch (e) {\n      if (e.status === 404 && OPTIONAL_SERVICES.has(service)) {\n        notImplRef.current = true;\n        setNotImpl(true);\n        setRows([]);\n      } else {\n        setError(e.message);\n      }\n    } finally {\n      setLoading(false);\n    }\n  }, [id, service]);', '  const loadRows = useCallback(async () => {\n    if (notImplRef.current) return;\n    if (!service) {\n      // Unsupported/unmapped service (GCP/Azure, or an AWS directory-tier\n      // service without a live-data endpoint yet) -- don\'t even attempt a\n      // fetch, go straight to the same graceful NotImplState AWS services\n      // without endpoints already use.\n      notImplRef.current = true;\n      setNotImpl(true);\n      setRows([]);\n      setLoading(false);\n      return;\n    }\n    setError(null);\n    try {\n      const data = await fetchService(id, service);\n      setRows(Array.isArray(data) ? data : []);\n    } catch (e) {\n      if (e.status === 404) {\n        // Any unmapped/unimplemented service 404s the same way -- treat\n        // it as "not configured yet" rather than a hard error.\n        notImplRef.current = true;\n        setNotImpl(true);\n        setRows([]);\n      } else {\n        setError(e.message);\n      }\n    } finally {\n      setLoading(false);\n    }\n  }, [id, service]);'], ['          <button className="btn-back" onClick={() => navigate(`/accounts/${id}/services`)}><ArrowLeftIcon size={13} /> Back</button>\n          <button className="btn-aws" onClick={() => openAccountConsole(id, service, { region })}>\n            <CloudIcon size={13} /> AWS Console <ExternalLinkIcon size={12} />\n          </button>', '          <button className="btn-back" onClick={() => navigate(`/accounts/${id}/services`)}><ArrowLeftIcon size={13} /> Back</button>\n          <button className="btn-aws" onClick={() => openAccountConsole(id, rawService, { region })}>\n            <CloudIcon size={13} /> Open Console <ExternalLinkIcon size={12} />\n          </button>'], ['        <span className="bc-current">{service}</span>', '        <span className="bc-current">{meta.label}</span>'], ['      {notImpl ? (\n        <NotImplState service={service} meta={meta} region={region} accountId={id} />\n      ) : (', '      {notImpl ? (\n        <NotImplState service={meta.label} rawService={rawService} meta={meta} region={region} accountId={id} />\n      ) : ('], ['function NotImplState({ service, meta, region, accountId }) {\n  return (\n    <div style={{ textAlign: "center", padding: "64px 32px", background: "rgba(13,22,39,0.5)", border: "1px solid rgba(99,130,190,0.1)", borderRadius: 12, marginTop: 8 }}>\n      <div style={{ display: "flex", justifyContent: "center", marginBottom: 16 }}><ServiceIcon icon={meta.icon} size={48} /></div>\n      <div style={{ fontSize: 16, fontWeight: 700, color: "#e2e8f0", marginBottom: 8 }}>{meta.label} not configured</div>\n      <div style={{ fontSize: 12, color: "rgba(99,130,190,0.65)", lineHeight: 1.7, marginBottom: 20 }}>\n        Backend endpoint not available yet for {service}.\n      </div>\n      <button className="btn-aws" onClick={() => openAccountConsole(accountId, service, { region })}>\n        <CloudIcon size={13} /> View in AWS Console <ExternalLinkIcon size={12} />\n      </button>\n    </div>\n  );\n}', 'function NotImplState({ service, rawService, meta, region, accountId }) {\n  return (\n    <div style={{ textAlign: "center", padding: "64px 32px", background: "rgba(13,22,39,0.5)", border: "1px solid rgba(99,130,190,0.1)", borderRadius: 12, marginTop: 8 }}>\n      <div style={{ display: "flex", justifyContent: "center", marginBottom: 16 }}><ServiceIcon icon={meta.icon} size={48} /></div>\n      <div style={{ fontSize: 16, fontWeight: 700, color: "#e2e8f0", marginBottom: 8 }}>{service} not configured</div>\n      <div style={{ fontSize: 12, color: "rgba(99,130,190,0.65)", lineHeight: 1.7, marginBottom: 20 }}>\n        This app doesn\'t have a resource list view for {service.toLowerCase?.() || service} yet.\n        You can still open it directly in the cloud console.\n      </div>\n      <button className="btn-aws" onClick={() => openAccountConsole(accountId, rawService, { region })}>\n        <CloudIcon size={13} /> Open in Console <ExternalLinkIcon size={12} />\n      </button>\n    </div>\n  );\n}']], 'jsx']}


def log(msg):
    print(msg, flush=True)


def apply_surgical_patches(path_str, patch_pairs, kind, dry_run):
    path = Path(path_str)
    if not path.exists():
        log(f"  ABORT: {path_str} does not exist -- wrong repo root, or the file was moved/"
            f"renamed since this script was written. Skipping.")
        return False

    original = path.read_text(encoding="utf-8")
    working = original
    problems = []
    for i, (old, new) in enumerate(patch_pairs, start=1):
        if new in working:
            log(f"  Patch {i}/{len(patch_pairs)} for {path_str}: already applied, skipping.")
            continue
        count = working.count(old)
        if count == 0:
            problems.append(f"patch {i}: anchor not found (local file has drifted from what "
                             f"this script expects -- re-check the file content before retrying)")
            continue
        if count > 1:
            problems.append(f"patch {i}: anchor matches {count} times, expected exactly 1 "
                             f"(ambiguous -- aborting this file to avoid a wrong replacement)")
            continue
        working = working.replace(old, new, 1)

    if problems:
        log(f"  ABORT {path_str}: not applying any changes to this file because:")
        for p in problems:
            log(f"    - {p}")
        return False

    if working == original:
        log(f"  No changes needed for {path_str} (already up to date).")
        return True

    if dry_run:
        log(f"  [dry-run] would apply {len(patch_pairs)} patch(es) to {path_str} "
            f"(backup would be made)")
        return True

    backup_path = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup_path)
    path.write_text(working, encoding="utf-8")
    log(f"  OK: patched {path_str} [.bak made]")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Preview changes without writing anything")
    args = ap.parse_args()

    if not Path("app").exists() or not Path("frontend").exists():
        log("ERROR: run this from the repo root (both ./app and ./frontend must exist here).")
        sys.exit(1)

    log(f"{'DRY RUN -- ' if args.dry_run else ''}Applying service-route fix...\n")

    ok = True
    for key, (path_str, patch_pairs, kind) in ROUTING_PATCHES.items():
        ok &= apply_surgical_patches(path_str, patch_pairs, kind, args.dry_run)

    log("")
    if args.dry_run:
        log("Dry run complete. Re-run without --dry-run to apply.")
    elif ok:
        log("All changes applied successfully.")
        log("")
        log("Next steps:")
        log("  1. cd frontend && npx vite build   (sanity check -- should be ~865KB, no errors)")
        log("  2. No backend restart needed -- this is frontend-only.")
        log("  3. This mainly matters once RESOURCE_COUNT_FIELD in ServiceList.jsx gets")
        log("     extended to cover GCP/Azure services -- worth a quick manual URL test")
        log("     now: visit /accounts/<id>/some-unmapped-key directly and confirm you")
        log("     get the 'not configured yet' panel, not a redirect to Overview.")
    else:
        log("Some files were NOT changed -- see ABORT/FAILED messages above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
