#!/usr/bin/env python3
"""
apply_alert_deeplink_fix.py

Fixes: clicking "Metrics" / the resource name on the Alerts page dropped you
on the resource LIST page (e.g. all 20 EBS volumes) with no indication of
which one triggered the alert, forcing a manual search.

After this patch: the same click takes you straight to that resource's row
on the ServiceDetail page with its metrics panel already open (like the EBS
detail panel), no manual searching required.

Run from the project root:
    python apply_alert_deeplink_fix.py

What it touches:
    frontend/src/pages/Alerts.jsx        - deep-link route now includes
                                            ?resource=<id> and is built from
                                            the alert's own `service` field
                                            instead of guessing from the id
    frontend/src/pages/ServiceDetail.jsx - reads that ?resource= param,
                                            auto-selects the matching row,
                                            and scrolls it into view

Safe to re-run: if the patch was already applied, it detects that and skips
without error. Original files are backed up to *.bak.pre-deeplink-fix before
any change, and any failure aborts BEFORE touching a file (a failed match on
file A never leaves file B half-patched).
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

ALERTS_PATH = REPO_ROOT / "frontend" / "src" / "pages" / "Alerts.jsx"
SERVICE_DETAIL_PATH = REPO_ROOT / "frontend" / "src" / "pages" / "ServiceDetail.jsx"


def die(msg):
    print(f"\n[ABORTED] {msg}")
    print("No files were modified.")
    sys.exit(1)


def load(path: Path) -> str:
    if not path.exists():
        die(f"Expected file not found: {path}\n"
            f"Run this script from the project root (monitoring-hub-V4-cost-optimized).")
    return path.read_text(encoding="utf-8")


def require_one(text: str, needle: str, filename: str):
    count = text.count(needle)
    if count == 0:
        die(f"Anchor text not found in {filename} — the file has likely drifted "
            f"since this script was written. Aborting without changes.\n"
            f"--- missing anchor ---\n{needle}")
    if count > 1:
        die(f"Anchor text found {count} times in {filename} (expected exactly once) — "
            f"refusing to guess which one to patch.\n"
            f"--- ambiguous anchor ---\n{needle}")


# ── Alerts.jsx edits ─────────────────────────────────────────────────────

ALERTS_OLD_DETAILROUTE = '''// ── Internal resource detail route ─────────────────────────────
function detailRoute(resource, accountId = 3) {
  if (!resource) return null;
  if (resource.startsWith("i-"))   return `/accounts/${accountId}/ec2?resource=${resource}`;
  if (resource.startsWith("vol-")) return `/accounts/${accountId}/ebs?resource=${resource}`;
  if (resource.includes("lambda")) return `/accounts/${accountId}/lambda`;
  return null;
}'''

ALERTS_NEW_DETAILROUTE = '''// ── Internal resource detail route ─────────────────────────────
// Deep-links straight to the resource's row + metrics panel on the
// ServiceDetail page (which reads the `resource` query param and
// auto-selects the matching row instead of making the user search).
const ROUTE_SEGMENT_BY_SERVICE = {
  ec2: "ec2", ebs: "ebs", rds: "rds", lambda: "lambda",
  s3: "s3", elb: "elb", ecs: "ecs",
};

function detailRoute(resource, accountId, service) {
  if (!resource || !accountId) return null;
  const seg = ROUTE_SEGMENT_BY_SERVICE[(service || "").toLowerCase()];
  if (seg) return `/accounts/${accountId}/${seg}?resource=${encodeURIComponent(resource)}`;

  // Fallback if `service` wasn't provided — guess from the resource id shape
  if (resource.startsWith("i-"))   return `/accounts/${accountId}/ec2?resource=${encodeURIComponent(resource)}`;
  if (resource.startsWith("vol-")) return `/accounts/${accountId}/ebs?resource=${encodeURIComponent(resource)}`;
  if (resource.includes("lambda")) return `/accounts/${accountId}/lambda?resource=${encodeURIComponent(resource)}`;
  return null;
}'''

ALERTS_OLD_CALLSITE = "                  const route      = detailRoute(a.resource, a.account_id);"
ALERTS_NEW_CALLSITE = "                  const route      = detailRoute(a.resource, a.account_id, a.service);"

ALERTS_ALREADY_APPLIED_MARKER = "ROUTE_SEGMENT_BY_SERVICE"


# ── ServiceDetail.jsx edits ──────────────────────────────────────────────

SD_OLD_IMPORT = 'import { useParams, useNavigate } from "react-router-dom";'
SD_NEW_IMPORT = 'import { useParams, useNavigate, useSearchParams } from "react-router-dom";'

SD_OLD_EXPORT_LINE = "export default function ServiceDetail({ service }) {"
SD_NEW_HELPER_PLUS_EXPORT = '''// Finds the row that matches a `?resource=` deep-link value coming from
// the Alerts page, so it can be auto-selected instead of making the user
// search for it manually.
function findRowByResource(rows, service, resource) {
  if (!resource || !Array.isArray(rows) || rows.length === 0) return null;

  if (service === "ECS") {
    // rows = cluster objects, each with a nested `.services` array
    for (const cluster of rows) {
      const svc = (cluster.services || []).find(s => s.service_name === resource);
      if (svc) return { ...svc, cluster_name: cluster.cluster_name, region: cluster.region };
    }
    return null;
  }

  return rows.find(r => {
    switch (service) {
      case "EC2":    return r.instance_id === resource;
      case "EBS":    return r.volume_id === resource;
      case "RDS":    return r.db_instance_id === resource || r.identifier === resource;
      case "Lambda": return r.function_name === resource || r.function_arn === resource;
      case "S3":     return (r.bucket_name || r.name) === resource;
      case "ELB":    return r.name === resource || r.load_balancer_arn === resource;
      default:       return false;
    }
  }) || null;
}

export default function ServiceDetail({ service }) {'''

SD_OLD_HEADER = '''  const { id }   = useParams();
  const navigate = useNavigate();
  const meta     = SERVICE_META[service] || SERVICE_META.EC2;'''
SD_NEW_HEADER = '''  const { id }   = useParams();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const meta     = SERVICE_META[service] || SERVICE_META.EC2;'''

SD_OLD_REFS = '''  const notImplRef  = useRef(false);
  const selectedRef = useRef(null);'''
SD_NEW_REFS = '''  const notImplRef  = useRef(false);
  const selectedRef = useRef(null);
  const autoSelectedRef = useRef(null);'''

SD_OLD_TAIL_OF_SELECTROW = '''    } catch (e) {
      console.error("Metrics fetch error:", e);
    } finally {
      setMLoading(false);
    }
  }

  const stateCounts = rows.reduce((acc, r) => {'''
SD_NEW_TAIL_OF_SELECTROW = '''    } catch (e) {
      console.error("Metrics fetch error:", e);
    } finally {
      setMLoading(false);
    }
  }

  // Deep-link support: if we arrived via Alerts' "📊 Metrics" link
  // (?resource=vol-xxx), auto-select that row as soon as it's loaded
  // instead of leaving the user to search for it manually.
  useEffect(() => {
    const resourceParam = searchParams.get("resource");
    if (!resourceParam || rows.length === 0) return;
    if (autoSelectedRef.current === resourceParam) return; // already handled

    const match = findRowByResource(rows, service, resourceParam);
    if (match) {
      autoSelectedRef.current = resourceParam;
      selectRow(match);
    }
  }, [rows, service, searchParams]);

  // Scroll the selected row into view (covers both the deep-link
  // auto-select above and normal manual clicks).
  useEffect(() => {
    if (!selected) return;
    const el = document.querySelector(".inst-row.inst-selected");
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [selected]);

  const stateCounts = rows.reduce((acc, r) => {'''

SD_ALREADY_APPLIED_MARKER = "findRowByResource"


def main():
    print(f"Project root: {REPO_ROOT}\n")

    alerts_text = load(ALERTS_PATH)
    sd_text = load(SERVICE_DETAIL_PATH)

    if ALERTS_ALREADY_APPLIED_MARKER in alerts_text and SD_ALREADY_APPLIED_MARKER in sd_text:
        print("Already applied — both files already contain the fix. Nothing to do.")
        return

    # ---- validate every anchor BEFORE writing anything ----
    require_one(alerts_text, ALERTS_OLD_DETAILROUTE, "Alerts.jsx")
    require_one(alerts_text, ALERTS_OLD_CALLSITE, "Alerts.jsx")

    require_one(sd_text, SD_OLD_IMPORT, "ServiceDetail.jsx")
    require_one(sd_text, SD_OLD_EXPORT_LINE, "ServiceDetail.jsx")
    require_one(sd_text, SD_OLD_HEADER, "ServiceDetail.jsx")
    require_one(sd_text, SD_OLD_REFS, "ServiceDetail.jsx")
    require_one(sd_text, SD_OLD_TAIL_OF_SELECTROW, "ServiceDetail.jsx")

    # ---- all anchors verified — safe to apply ----
    new_alerts_text = alerts_text.replace(ALERTS_OLD_DETAILROUTE, ALERTS_NEW_DETAILROUTE, 1)
    new_alerts_text = new_alerts_text.replace(ALERTS_OLD_CALLSITE, ALERTS_NEW_CALLSITE, 1)

    new_sd_text = sd_text.replace(SD_OLD_IMPORT, SD_NEW_IMPORT, 1)
    new_sd_text = new_sd_text.replace(SD_OLD_EXPORT_LINE, SD_NEW_HELPER_PLUS_EXPORT, 1)
    new_sd_text = new_sd_text.replace(SD_OLD_HEADER, SD_NEW_HEADER, 1)
    new_sd_text = new_sd_text.replace(SD_OLD_REFS, SD_NEW_REFS, 1)
    new_sd_text = new_sd_text.replace(SD_OLD_TAIL_OF_SELECTROW, SD_NEW_TAIL_OF_SELECTROW, 1)

    # ---- backup originals, then write ----
    alerts_backup = ALERTS_PATH.with_suffix(ALERTS_PATH.suffix + ".bak.pre-deeplink-fix")
    sd_backup = SERVICE_DETAIL_PATH.with_suffix(SERVICE_DETAIL_PATH.suffix + ".bak.pre-deeplink-fix")

    if not alerts_backup.exists():
        alerts_backup.write_text(alerts_text, encoding="utf-8")
    if not sd_backup.exists():
        sd_backup.write_text(sd_text, encoding="utf-8")

    ALERTS_PATH.write_text(new_alerts_text, encoding="utf-8")
    SERVICE_DETAIL_PATH.write_text(new_sd_text, encoding="utf-8")

    print("Patched:")
    print(f"  {ALERTS_PATH.relative_to(REPO_ROOT)}")
    print(f"  {SERVICE_DETAIL_PATH.relative_to(REPO_ROOT)}")
    print(f"\nBackups saved as *.bak.pre-deeplink-fix next to each file.")
    print("\nNext steps:")
    print("  1. Restart/rebuild the frontend (npm run dev, or npm run build for prod)")
    print("  2. On the Alerts page, click 'Metrics' or a resource name")
    print("     -> should land on the resource's row with its metrics panel already open")
    print("  3. git add frontend/src/pages/Alerts.jsx frontend/src/pages/ServiceDetail.jsx")
    print("     git commit -m 'fix: deep-link alerts straight to resource metrics panel'")
    print("     git push origin main")


if __name__ == "__main__":
    main()
