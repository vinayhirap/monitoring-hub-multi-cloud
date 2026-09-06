#!/usr/bin/env python3
"""
apply_console_url_consolidation_frontend.py

Frontend half of console-url consolidation (backend half already applied:
GET /admin/accounts/{account_id}/console-url in app/api/admin/accounts.py).

WHAT THIS CHANGES
    frontend/src/pages/AccountDetail.jsx
        - account-level "AWS Console" link (was a hardcoded
          console.aws.amazon.com href) -> button that calls the backend
          endpoint and opens the returned federated URL.
        - instance-level "Open in AWS" link -> same pattern, with
          resource_id set to the selected instance.
    frontend/src/pages/ServiceDetail.jsx
        - awsConsoleLink()/awsDeepLink() client-side URL builders are
          removed. All three call sites (service-level header link,
          NotImplState fallback link, per-resource detail-panel link)
          now call the backend endpoint instead.
        - Alerts.jsx is NOT touched — it already calls
          GET /alerts/{id}/console-url (done in the backend step).

WHY
    Matches the design in multi-cloud-architecture-assessment.md section
    4 ("Console URLs: collapse the three duplicated frontend
    implementations + the one backend implementation into a single
    backend get_resource_console_url dispatcher"). Also fixes the
    underlying account-mismatch bug the old hrefs had: a plain
    console.aws.amazon.com link has no account context and opens
    whichever account the browser is already signed into.

SAFETY
    Every edit is guarded by an exact occurrence-count check on its
    anchor text before writing. If your local file has already
    diverged from what this script expects, it aborts that file
    untouched and tells you which anchor didn't match, instead of
    guessing. Backups: *.bak.pre-console-consolidation-frontend.

Run from the project root:
    python apply_console_url_consolidation_frontend.py --dry-run
    python apply_console_url_consolidation_frontend.py
"""

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()

ACCOUNT_DETAIL = ROOT / "frontend/src/pages/AccountDetail.jsx"
SERVICE_DETAIL = ROOT / "frontend/src/pages/ServiceDetail.jsx"

BACKUP_SUFFIX = ".bak.pre-console-consolidation-frontend"


def backup(path: Path, dry_run: bool):
    dest = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    if dry_run:
        print(f"  [dry-run] would back up {path.name} -> {dest.name}")
        return
    shutil.copy2(path, dest)
    print(f"  backed up {path.name} -> {dest.name}")


def guarded_replace(text: str, old: str, new: str, label: str, path: Path) -> str:
    count = text.count(old)
    if count != 1:
        print(f"  ABORT [{path.name}]: anchor '{label}' found {count} times, expected 1.")
        print("  Local file has diverged from what this script expects — no changes written.")
        sys.exit(1)
    return text.replace(old, new)


# ── AccountDetail.jsx edits ─────────────────────────────────────────

ACCOUNT_DETAIL_HELPER_ANCHOR = 'const STATE_COLOR = {'
ACCOUNT_DETAIL_HELPER_NEW = '''async function openAccountConsole(accountId, service, resourceId) {
  try {
    const params = new URLSearchParams({ service });
    if (resourceId) params.set("resource_id", resourceId);
    const res = await fetch(`/api/admin/accounts/${accountId}/console-url?${params}`);
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    window.open(data.url, "_blank", "noopener,noreferrer");
  } catch (e) {
    console.error("Console link failed:", e);
    alert("Could not open AWS console link.");
  }
}

const STATE_COLOR = {'''

ACCOUNT_DETAIL_ACCOUNT_LINK_OLD = '''          {/* FIX 7 — account-level AWS Console deep-link using dynamic region */}
          <a
            href={`https://${account.region ?? "ap-south-2"}.console.aws.amazon.com/ec2/home?region=${account.region ?? "ap-south-2"}#Instances:`}
            target="_blank"
            rel="noopener noreferrer"
            className="btn-aws"
          >
            ☁ AWS Console ↗
          </a>'''

ACCOUNT_DETAIL_ACCOUNT_LINK_NEW = '''          {/* Console link now backend-generated — federated, correct account */}
          <button className="btn-aws" onClick={() => openAccountConsole(id, "ec2")}>
            ☁ AWS Console ↗
          </button>'''

ACCOUNT_DETAIL_INSTANCE_LINK_OLD = '''            {/* FIX 7 — instance-level deep-link with dynamic region + sort param */}
            <a
              href={`https://${selected.region}.console.aws.amazon.com/ec2/home?region=${selected.region}#Instances:instanceId=${selected.instance_id};sort=desc:launchTime`}
              target="_blank"
              rel="noopener noreferrer"
              className="btn-open-aws"
            >
              ☁ Open in AWS ↗
            </a>'''

ACCOUNT_DETAIL_INSTANCE_LINK_NEW = '''            {/* Console link now backend-generated — federated, correct account */}
            <button className="btn-open-aws" onClick={() => openAccountConsole(id, "ec2", selected.instance_id)}>
              ☁ Open in AWS ↗
            </button>'''


def patch_account_detail(dry_run: bool):
    print("AccountDetail.jsx")
    text = ACCOUNT_DETAIL.read_text(encoding="utf-8")

    text = guarded_replace(text, ACCOUNT_DETAIL_HELPER_ANCHOR, ACCOUNT_DETAIL_HELPER_NEW,
                            "STATE_COLOR const (helper insertion point)", ACCOUNT_DETAIL)
    text = guarded_replace(text, ACCOUNT_DETAIL_ACCOUNT_LINK_OLD, ACCOUNT_DETAIL_ACCOUNT_LINK_NEW,
                            "account-level AWS Console link", ACCOUNT_DETAIL)
    text = guarded_replace(text, ACCOUNT_DETAIL_INSTANCE_LINK_OLD, ACCOUNT_DETAIL_INSTANCE_LINK_NEW,
                            "instance-level Open in AWS link", ACCOUNT_DETAIL)

    if dry_run:
        print("  [dry-run] 3 anchors matched, would write changes.")
        return
    backup(ACCOUNT_DETAIL, dry_run)
    ACCOUNT_DETAIL.write_text(text, encoding="utf-8")
    print("  written.")


# ── ServiceDetail.jsx edits ─────────────────────────────────────────

SERVICE_DETAIL_OLD_HELPERS = '''function awsConsoleLink(service, region) {
  const base = `https://${region}.console.aws.amazon.com`;
  const map = {
    EC2:    `${base}/ec2/home?region=${region}#Instances:`,
    EBS:    `${base}/ec2/home?region=${region}#Volumes:`,
    RDS:    `${base}/rds/home?region=${region}#databases:`,
    Lambda: `${base}/lambda/home?region=${region}#/functions`,
    S3:     `https://s3.console.aws.amazon.com/s3/buckets`,
    ELB:    `${base}/ec2/home?region=${region}#LoadBalancers:`,
    ECS:    `${base}/ecs/home?region=${region}`,
  };
  return map[service] || base;
}

function awsDeepLink(service, row, region) {
  const base = `https://${region}.console.aws.amazon.com`;
  switch (service) {
    case "EC2":    return `${base}/ec2/home?region=${region}#Instances:instanceId=${row.instance_id}`;
    case "EBS":    return `${base}/ec2/home?region=${region}#Volumes:volumeId=${row.volume_id}`;
    case "RDS":    return `${base}/rds/home?region=${region}#database:id=${row.db_instance_id}`;
    case "Lambda": return `${base}/lambda/home?region=${region}#/functions/${row.function_name}`;
    case "S3":     return `https://s3.console.aws.amazon.com/s3/buckets/${row.bucket_name || row.name}`;
    case "ELB":    return `${base}/ec2/home?region=${region}#LoadBalancers:search=${row.name}`;
    case "ECS":    return `${base}/ecs/home?region=${region}#/clusters/${row.cluster_name}/services/${row.service_name}`;
    default:       return base;
  }
}'''

SERVICE_DETAIL_NEW_HELPERS = '''async function openAccountConsole(accountId, service, params = {}) {
  try {
    const qs = new URLSearchParams({ service: service.toLowerCase(), ...params });
    const res = await fetch(`/api/admin/accounts/${accountId}/console-url?${qs}`);
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    window.open(data.url, "_blank", "noopener,noreferrer");
  } catch (e) {
    console.error("Console link failed:", e);
    alert("Could not open AWS console link.");
  }
}

// Resource-id/name params for the backend console-url dispatcher, one
// row shape per service — mirrors the old awsDeepLink() switch.
function consoleParamsFor(service, row) {
  switch (service) {
    case "EC2":    return { resource_id: row.instance_id };
    case "EBS":    return { resource_id: row.volume_id };
    case "RDS":    return { resource_id: row.db_instance_id };
    case "Lambda": return { resource_id: row.function_name };
    case "S3":     return { resource_id: row.bucket_name || row.name };
    case "ELB":    return { resource_id: row.name, resource_name: row.name };
    case "ECS":    return {
      resource_id: row.cluster_name,
      resource_name: row.cluster_name,
      ecs_service_name: row.service_name,
    };
    default:       return {};
  }
}'''

SERVICE_DETAIL_HEADER_LINK_OLD = '''          <a href={awsConsoleLink(service, region)} target="_blank" rel="noopener noreferrer" className="btn-aws">
            <CloudIcon size={13} /> AWS Console <ExternalLinkIcon size={12} />
          </a>'''

SERVICE_DETAIL_HEADER_LINK_NEW = '''          <button className="btn-aws" onClick={() => openAccountConsole(id, service, { region })}>
            <CloudIcon size={13} /> AWS Console <ExternalLinkIcon size={12} />
          </button>'''

SERVICE_DETAIL_NOTIMPL_LINK_OLD = '''      <a href={awsConsoleLink(service, region)} target="_blank" rel="noopener noreferrer" className="btn-aws">
        <CloudIcon size={13} /> View in AWS Console <ExternalLinkIcon size={12} />
      </a>'''

SERVICE_DETAIL_NOTIMPL_LINK_NEW = '''      <button className="btn-aws" onClick={() => openAccountConsole(accountId, service, { region })}>
        <CloudIcon size={13} /> View in AWS Console <ExternalLinkIcon size={12} />
      </button>'''

SERVICE_DETAIL_NOTIMPL_SIG_OLD = 'function NotImplState({ service, meta, region }) {'
SERVICE_DETAIL_NOTIMPL_SIG_NEW = 'function NotImplState({ service, meta, region, accountId }) {'

SERVICE_DETAIL_NOTIMPL_CALLSITE_OLD = '<NotImplState service={service} meta={meta} region={region} />'
SERVICE_DETAIL_NOTIMPL_CALLSITE_NEW = '<NotImplState service={service} meta={meta} region={region} accountId={id} />'

SERVICE_DETAIL_PANEL_SIG_OLD = (
    'function ServiceDetailPanel({ service, row, metrics, mLoading, region, timeRange, '
    'onTimeRangeChange, allRows, onClose, onSelectRelated }) {'
)
SERVICE_DETAIL_PANEL_SIG_NEW = (
    'function ServiceDetailPanel({ service, row, metrics, mLoading, region, timeRange, '
    'onTimeRangeChange, allRows, onClose, onSelectRelated, accountId }) {'
)

SERVICE_DETAIL_PANEL_CALLSITE_OLD = '''            <ServiceDetailPanel
              service={service}
              row={selected}
              metrics={metrics}
              mLoading={mLoading}
              region={region}
              timeRange={timeRange}
              onTimeRangeChange={setTimeRange}
              allRows={rows}
              onClose={() => { selectedRef.current = null; setSelected(null); setMetrics(null); }}
              onSelectRelated={(row) => selectRow(row)}
            />'''

SERVICE_DETAIL_PANEL_CALLSITE_NEW = '''            <ServiceDetailPanel
              service={service}
              row={selected}
              metrics={metrics}
              mLoading={mLoading}
              region={region}
              timeRange={timeRange}
              onTimeRangeChange={setTimeRange}
              allRows={rows}
              onClose={() => { selectedRef.current = null; setSelected(null); setMetrics(null); }}
              onSelectRelated={(row) => selectRow(row)}
              accountId={id}
            />'''

SERVICE_DETAIL_ROW_LINK_OLD = '''      <a href={awsDeepLink(service, row, region)} target="_blank" rel="noopener noreferrer" className="btn-open-aws">
        <CloudIcon size={13} /> Open in AWS <ExternalLinkIcon size={12} />
      </a>'''

SERVICE_DETAIL_ROW_LINK_NEW = '''      <button
        className="btn-open-aws"
        onClick={() => openAccountConsole(accountId, service, { region, ...consoleParamsFor(service, row) })}
      >
        <CloudIcon size={13} /> Open in AWS <ExternalLinkIcon size={12} />
      </button>'''


def patch_service_detail(dry_run: bool):
    print("ServiceDetail.jsx")
    text = SERVICE_DETAIL.read_text(encoding="utf-8")

    edits = [
        (SERVICE_DETAIL_HEADER_LINK_OLD, SERVICE_DETAIL_HEADER_LINK_NEW, "header AWS Console link"),
        (SERVICE_DETAIL_NOTIMPL_LINK_OLD, SERVICE_DETAIL_NOTIMPL_LINK_NEW, "NotImplState AWS Console link"),
        (SERVICE_DETAIL_NOTIMPL_SIG_OLD, SERVICE_DETAIL_NOTIMPL_SIG_NEW, "NotImplState signature"),
        (SERVICE_DETAIL_NOTIMPL_CALLSITE_OLD, SERVICE_DETAIL_NOTIMPL_CALLSITE_NEW, "NotImplState call site"),
        (SERVICE_DETAIL_PANEL_SIG_OLD, SERVICE_DETAIL_PANEL_SIG_NEW, "ServiceDetailPanel signature"),
        (SERVICE_DETAIL_PANEL_CALLSITE_OLD, SERVICE_DETAIL_PANEL_CALLSITE_NEW, "ServiceDetailPanel call site"),
        (SERVICE_DETAIL_ROW_LINK_OLD, SERVICE_DETAIL_ROW_LINK_NEW, "per-resource Open in AWS link"),
        (SERVICE_DETAIL_OLD_HELPERS, SERVICE_DETAIL_NEW_HELPERS, "awsConsoleLink/awsDeepLink helper block"),
    ]
    for old, new, label in edits:
        text = guarded_replace(text, old, new, label, SERVICE_DETAIL)

    if dry_run:
        print(f"  [dry-run] {len(edits)} anchors matched, would write changes.")
        return
    backup(SERVICE_DETAIL, dry_run)
    SERVICE_DETAIL.write_text(text, encoding="utf-8")
    print("  written.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    for path in (ACCOUNT_DETAIL, SERVICE_DETAIL):
        if not path.exists():
            print(f"ABORT: {path} not found. Run this from the project root.")
            sys.exit(1)

    patch_account_detail(args.dry_run)
    patch_service_detail(args.dry_run)

    if args.dry_run:
        print("\nDry run complete. No files written. Re-run without --dry-run to apply.")
    else:
        print("\nDone. Restart the frontend dev server and click through EC2/EBS/RDS console links to verify.")


if __name__ == "__main__":
    main()
