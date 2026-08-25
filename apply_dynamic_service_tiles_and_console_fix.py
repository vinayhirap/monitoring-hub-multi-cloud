#!/usr/bin/env python3
"""
apply_dynamic_service_tiles_and_console_fix.py

Fixes two Services-page problems:

  1. Some service tiles redirected to /overview instead of a resource
     page. Root cause: ServiceList.jsx builds a tile for every service
     that has a metric enabled (34 possible services), but App.jsx /
     ServiceDetail.jsx only ever had real routes + backend endpoints for
     7 "core" AWS services (ec2, ebs, rds, s3, ecs, elb, lambda). Every
     other tile navigated to a URL with no matching route, which fell
     through to the app's catch-all route ("*") straight to /overview.

     Fix: tiles for the 7 core services still open the internal detail
     page as before. Tiles for every other ("extended") service now open
     the AWS Console directly in a new tab instead, via the SAME
     federated console-link endpoint the Alerts page already uses
     (/api/admin/accounts/{id}/console-url) — nothing internal to
     navigate to for those 34 services yet, so this replaces a broken
     link with a working one rather than pretending they're internal
     pages.

  2. Tiles were shown/hidden based on whether a metric was *selected*
     for that service (a static, manually-picked setting), not whether
     the account actually *has* any resources of that type right now.
     Fix: adds a small backend endpoint
     (GET /api/live/resource-counts/{account_id}) that calls the same
     live AWS collectors the 7 core detail pages already use and
     returns a real resource count per service. The Services page now
     hides a core-service tile only when that count comes back as a
     confirmed zero — an extended service (no collector exists for it
     yet), a fetch error, or a non-AWS account all fail OPEN (tile
     stays visible) rather than guessing.

NOTE on the separate "console link should use the signed-in user's own
AWS credentials, not the monitoring-hub server's role" request: that's
an access-model decision (AWS IAM Identity Center federation vs.
per-app-user IAM role mapping vs. something tied into the access-scopes
work you already have in progress) that needs a decision from you
before it can be safely implemented — this script does NOT touch that;
see the chat message for the question.

ALSO NOTE: the new /resource-counts endpoint (like the existing 7 core
live/* endpoints it reuses) collects using the monitoring-hub server's
own AWS session, not a per-account assumed role, even when an account
has a role_arn configured for a different AWS account. That's an
existing limitation of app/aws/collector_direct.py, not something this
script introduces — worth knowing if you have accounts configured with
a role_arn pointing at a different AWS account than the server itself
runs in, since resource counts (like the existing detail pages) may
reflect the wrong account until that's addressed separately.

Usage:
    python apply_dynamic_service_tiles_and_console_fix.py [path-to-repo-root]
"""

import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(".").resolve()

live_data_py    = repo_root / "app" / "api" / "live_data.py"
api_js          = repo_root / "frontend" / "src" / "api" / "api.js"
servicelist_jsx = repo_root / "frontend" / "src" / "pages" / "ServiceList.jsx"

results = []


def report(label, status, detail=""):
    results.append((label, status, detail))
    tag = {"ok": "\u2705", "skip": "\u23ed ", "FAIL": "\u274c"}[status]
    print(f"{tag} {label}" + (f" \u2014 {detail}" if detail else ""))


def replace_once(path: Path, old: str, new: str, label: str, already_marker: str = None):
    if not path.exists():
        report(label, "FAIL", f"file not found: {path}")
        return
    text = path.read_text(encoding="utf-8")
    if already_marker and already_marker in text:
        report(label, "skip", "already applied")
        return
    count = text.count(old)
    if count == 0:
        report(label, "FAIL",
               f"expected text not found in {path.name} \u2014 your local file has "
               f"diverged here. Apply this change manually (see script source "
               f"under label '{label}').")
        return
    if count > 1:
        report(label, "FAIL",
               f"expected text found {count} times (expected exactly once) in "
               f"{path.name} \u2014 skipping to avoid ambiguous edit.")
        return
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    report(label, "ok")


# ═════════════════════════════════════════════════════════════════
# STEP 1 — backend: GET /api/live/resource-counts/{account_db_id}
# ═════════════════════════════════════════════════════════════════

replace_once(
    live_data_py,
    old='''@router.get("/ecs/{account_db_id}")
def live_ecs(account_db_id: int):
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_ecs_clusters(region))


# ── CloudWatch metric series endpoints ───────────────────────''',
    new='''@router.get("/ecs/{account_db_id}")
def live_ecs(account_db_id: int):
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_ecs_clusters(region))


# ── Real-time per-service resource counts ─────────────────────
# Used by the Services page to decide whether a tile should be shown
# at all — dynamically, based on whether the account actually HAS any
# resources of that type right now, instead of only whether a metric
# is selected for it. Only covers the 7 services with a live collector
# (same ones the /ec2, /ebs, /rds, /lambda, /s3, /elb, /ecs endpoints
# above use) — there's no resource-level collector yet for the
# extended (metric-catalog-only) services, so those aren't included
# here; the frontend treats a missing key as "unknown" and keeps that
# tile visible rather than hiding it on a guess.
_CORE_RESOURCE_COLLECTORS = {
    "ec2":    collect_ec2_instances,
    "ebs":    collect_ebs_volumes,
    "rds":    collect_rds_instances,
    "lambda": collect_lambda_functions,
    "s3":     collect_s3_buckets,
    "elb":    collect_elb,
    "ecs":    collect_ecs_clusters,
}


@router.get("/resource-counts/{account_db_id}")
def live_resource_counts(account_db_id: int):
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region")
    counts = {}
    for svc, collector in _CORE_RESOURCE_COLLECTORS.items():
        try:
            counts[svc] = len(collector(region))
        except Exception as e:
            # Unknown, not zero — a transient AWS/permissions error
            # shouldn't hide a tile that may well have real resources.
            logger.warning(f"resource-counts: {svc} failed for account {account_db_id}: {e}")
            counts[svc] = None
    return counts


# ── CloudWatch metric series endpoints ───────────────────────''',
    label="live_data.py: add /resource-counts/{account_db_id} endpoint",
    already_marker="_CORE_RESOURCE_COLLECTORS = {",
)


# ═════════════════════════════════════════════════════════════════
# STEP 2 — frontend api.js: helpers for the new endpoint + the
#          existing generic console-url endpoint
# ═════════════════════════════════════════════════════════════════

replace_once(
    api_js,
    old='''export const applyDefaultTemplate     = (accountId) =>
  apiFetch(`/api/account-metrics/${accountId}/apply-default`, { method: "POST" });''',
    new='''export const applyDefaultTemplate     = (accountId) =>
  apiFetch(`/api/account-metrics/${accountId}/apply-default`, { method: "POST" });

// ── Live resource counts (dynamic Services-page tile visibility) ────
export const getResourceCounts = (accountId) => apiFetch(`/api/live/resource-counts/${accountId}`);

// ── Federated AWS Console deep link (same endpoint the Alerts page uses) ──
export const getConsoleUrl = (accountId, service) =>
  apiFetch(`/api/admin/accounts/${accountId}/console-url?service=${encodeURIComponent(service)}`);''',
    label="api.js: add getResourceCounts + getConsoleUrl helpers",
    already_marker="export const getResourceCounts = (accountId)",
)


# ═════════════════════════════════════════════════════════════════
# STEP 3 — ServiceList.jsx: dynamic visibility + fix broken links
# ═════════════════════════════════════════════════════════════════

replace_once(
    servicelist_jsx,
    old='import { getAlerts, getAccountMetrics } from "../api/api";',
    new='import { getAlerts, getAccountMetrics, getResourceCounts, getConsoleUrl } from "../api/api";',
    label="ServiceList.jsx: import new api helpers",
    already_marker="getAlerts, getAccountMetrics, getResourceCounts, getConsoleUrl",
)

replace_once(
    servicelist_jsx,
    old='const PALETTE = ["#2bb3ac", "#38bdf8", "#7c6ee0", "#fbbf24", "#34d399", "#f472b6", "#22c55e", "#f59e0b", "#a78bfa", "#e879f9"];',
    new='''const PALETTE = ["#2bb3ac", "#38bdf8", "#7c6ee0", "#fbbf24", "#34d399", "#f472b6", "#22c55e", "#f59e0b", "#a78bfa", "#e879f9"];

// Services with a real backend resource-list + detail page (see
// app/api/live_data.py + the /accounts/:id/<service> routes in
// App.jsx). A tile for any other service opens the AWS Console
// directly instead of navigating internally, since there's no
// detail page for it yet.
const CORE_AWS_SERVICES = new Set(["ec2", "ebs", "rds", "lambda", "s3", "elb", "ecs"]);''',
    label="ServiceList.jsx: add CORE_AWS_SERVICES constant",
    already_marker="const CORE_AWS_SERVICES = new Set(",
)

replace_once(
    servicelist_jsx,
    old='''  const [alerts,  setAlerts]  = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(`/api/admin/accounts/${id}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d && !cancelled) setAccount(d); })
      .catch(console.error);
    getAlerts().then(a => { if (!cancelled) setAlerts(Array.isArray(a) ? a : []); }).catch(() => {});
    getAccountMetrics(id)
      .then(g => { if (!cancelled) setGroups(Array.isArray(g) ? g : []); })
      .catch(console.error)
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [id]);

  const provider = account?.provider || "aws";''',
    new='''  const [alerts,  setAlerts]  = useState([]);
  const [loading, setLoading] = useState(true);
  // Real per-service resource counts from AWS (core services only).
  // null = not loaded yet (or not applicable) — tiles fail OPEN until
  // we actually know a count is zero.
  const [resourceCounts, setResourceCounts] = useState(null);
  const [consoleLoading, setConsoleLoading] = useState(null); // svc.id currently opening

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(`/api/admin/accounts/${id}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d && !cancelled) setAccount(d); })
      .catch(console.error);
    getAlerts().then(a => { if (!cancelled) setAlerts(Array.isArray(a) ? a : []); }).catch(() => {});
    getAccountMetrics(id)
      .then(g => { if (!cancelled) setGroups(Array.isArray(g) ? g : []); })
      .catch(console.error)
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [id]);

  const provider = account?.provider || "aws";

  // Fetch real resource counts once we know this is an AWS account —
  // the collectors behind this endpoint are AWS-only.
  useEffect(() => {
    if (!account || (account.provider || "aws") !== "aws") return;
    let cancelled = false;
    getResourceCounts(id).then(c => { if (!cancelled) setResourceCounts(c); }).catch(() => {});
    return () => { cancelled = true; };
  }, [account, id]);

  function openInConsole(serviceId) {
    if (provider !== "aws") return;
    setConsoleLoading(serviceId);
    getConsoleUrl(id, serviceId)
      .then(r => { if (r?.url) window.open(r.url, "_blank", "noopener,noreferrer"); })
      .catch(err => {
        console.error(err);
        window.alert(
          "Couldn't open the AWS Console for this service. Check that an IAM " +
          "role is configured for this account in Settings."
        );
      })
      .finally(() => setConsoleLoading(null));
  }''',
    label="ServiceList.jsx: add resourceCounts state + openInConsole()",
    already_marker="function openInConsole(serviceId) {",
)

replace_once(
    servicelist_jsx,
    old='''  const activeServices = useMemo(() => {
    return groups
      .filter(g => (g.metrics || []).some(m => m.enabled))
      .map((g, i) => ({
        id: g.service,
        label: g.display_service || g.service,
        desc: DESC_OVERRIDES[g.service] || (g.category === "core" ? "Core service" : "Extended service"),
        color: PALETTE[i % PALETTE.length],
        enabledCount: g.metrics.filter(m => m.enabled).length,
      }));
  }, [groups]);''',
    new='''  const hasAnyMetricsEnabled = groups.some(g => (g.metrics || []).some(m => m.enabled));

  const activeServices = useMemo(() => {
    return groups
      .filter(g => (g.metrics || []).some(m => m.enabled))
      // Dynamic visibility: hide a tile ONLY when we have a real,
      // confirmed-zero resource count for it. Extended services (no
      // live collector yet), non-AWS accounts, and counts we haven't
      // loaded/couldn't fetch all stay visible rather than being
      // hidden on a guess.
      .filter(g => {
        if (!resourceCounts) return true;
        if (!CORE_AWS_SERVICES.has(g.service)) return true;
        const count = resourceCounts[g.service];
        return count === undefined || count === null || count > 0;
      })
      .map((g, i) => ({
        id: g.service,
        label: g.display_service || g.service,
        desc: DESC_OVERRIDES[g.service] || (g.category === "core" ? "Core service" : "Extended service"),
        color: PALETTE[i % PALETTE.length],
        enabledCount: g.metrics.filter(m => m.enabled).length,
      }));
  }, [groups, resourceCounts]);''',
    label="ServiceList.jsx: filter tiles by real resource count",
    already_marker="const hasAnyMetricsEnabled = groups.some",
)

replace_once(
    servicelist_jsx,
    old='''      ) : activeServices.length === 0 ? (
        <div style={{
          border:"1px dashed var(--border)", borderRadius:"var(--radius-lg)", padding:"40px 24px",
          textAlign:"center", color:"var(--text-muted)", fontSize:13,
        }}>
          No services are enabled for this account yet. Go to <b style={{color:"var(--text-secondary)"}}>Settings → Metrics</b> to select
          which services and metrics to monitor — this page always mirrors that selection.
        </div>
      ) : (
        <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fill, minmax(220px, 1fr))", gap:16 }}>
          {activeServices.map(svc => (
            <ServiceCard key={svc.id} svc={svc} provider={provider}
              alertCount={alertsForService(svc.id).length}
              hasCritical={alertsForService(svc.id).some(a => a.severity?.toUpperCase() === "CRITICAL")}
              onClick={() => navigate(`/accounts/${id}/${svc.id}`)} />
          ))}
        </div>
      )}''',
    new='''      ) : activeServices.length === 0 ? (
        <div style={{
          border:"1px dashed var(--border)", borderRadius:"var(--radius-lg)", padding:"40px 24px",
          textAlign:"center", color:"var(--text-muted)", fontSize:13,
        }}>
          {hasAnyMetricsEnabled ? (
            <>Metrics are enabled for this account, but no matching resources were found in AWS
            right now — this list updates automatically once resources appear.</>
          ) : (
            <>No services are enabled for this account yet. Go to <b style={{color:"var(--text-secondary)"}}>Settings → Metrics</b> to select
            which services and metrics to monitor — this page always mirrors that selection.</>
          )}
        </div>
      ) : (
        <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fill, minmax(220px, 1fr))", gap:16 }}>
          {activeServices.map(svc => {
            const routable = CORE_AWS_SERVICES.has(svc.id);
            return (
              <ServiceCard key={svc.id} svc={svc} provider={provider}
                alertCount={alertsForService(svc.id).length}
                hasCritical={alertsForService(svc.id).some(a => a.severity?.toUpperCase() === "CRITICAL")}
                routable={routable}
                isConsoleLoading={consoleLoading === svc.id}
                onClick={() => routable ? navigate(`/accounts/${id}/${svc.id}`) : openInConsole(svc.id)} />
            );
          })}
        </div>
      )}''',
    label="ServiceList.jsx: route core tiles internally, extended tiles to AWS Console",
    already_marker="const routable = CORE_AWS_SERVICES.has(svc.id);",
)

replace_once(
    servicelist_jsx,
    old='function ServiceCard({ svc, provider, onClick, alertCount, hasCritical }) {',
    new='function ServiceCard({ svc, provider, onClick, alertCount, hasCritical, routable = true, isConsoleLoading = false }) {',
    label="ServiceCard: accept routable / isConsoleLoading props",
    already_marker="routable = true, isConsoleLoading = false",
)

replace_once(
    servicelist_jsx,
    old='''        opacity: hovered ? 1 : 0.6, transition:"all .18s",
      }}>OPEN →</div>''',
    new='''        opacity: hovered ? 1 : 0.6, transition:"all .18s",
      }}>
        {routable ? "OPEN →" : (isConsoleLoading ? "OPENING…" : "VIEW IN CONSOLE ↗")}
      </div>''',
    label="ServiceCard: label reflects internal vs. console-link behavior",
    already_marker="VIEW IN CONSOLE ↗",
)


# ═════════════════════════════════════════════════════════════════
print()
n_fail = sum(1 for _, s, _ in results if s == "FAIL")
n_ok   = sum(1 for _, s, _ in results if s == "ok")
n_skip = sum(1 for _, s, _ in results if s == "skip")
print(f"Done. {n_ok} applied, {n_skip} already applied, {n_fail} failed.")
if n_fail:
    print("\nFor any FAILED step above, your local file differs from what this "
          "script expects. Open the file, find the described location, and "
          "apply the change by hand (or paste me the current surrounding "
          "code and I'll adjust).")
    sys.exit(1)
