// monitoring-hub/frontend/src/pages/ServiceList.jsx
import { useEffect, useState, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { getAlertSummary, getAccountMetrics, getResourceCounts } from "../api/api";
import { CloudServiceIcon, AzureBrandLogo, officialPerService } from "../components/cloud-icons";
import { LinkIcon } from "../components/icons";
import { sectionMeta } from "../components/MetricSelector";
import "../components/MetricSelector.css";

// Short blurbs for the services we know about. Anything not listed here
// (e.g. a directory-tier service the account onboarded via live discovery)
// falls back to its category ("core service" / "extended service") rather
// than a made-up description.
const DESC_OVERRIDES = {
  ec2: "Compute instances", ebs: "Block storage volumes", rds: "Managed databases",
  s3: "Object storage buckets", ecs: "Container services", elb: "Load balancers", alb: "Load balancers", nlb: "Load balancers", lambda: "Serverless functions",
  compute_instance: "Virtual machines", gcs_bucket: "Object storage buckets", cloudsql_instance: "Managed databases",
  cloud_run_service: "Serverless containers", gke_cluster: "Kubernetes clusters", gke_node: "Kubernetes nodes",
  cloudfunctions_function: "Serverless functions", pubsub_topic: "Pub/Sub topics", pubsub_subscription: "Pub/Sub subscriptions",
  cloud_lb: "Load balancers", redis_instance: "Managed Redis", bigquery_project: "Data warehouse",
  spanner_instance: "Globally distributed SQL", firestore_database: "Document database", nat_gateway: "Outbound NAT",
  gce_persistent_disk: "Block storage volumes",
  vm: "Virtual machines", vmss: "VM scale sets", storage_account: "Blob/file storage", sql_database: "Managed databases",
  app_service: "Web apps", aks_cluster: "Kubernetes clusters", function_app: "Serverless functions",
  cosmosdb_account: "Multi-model database", redis_cache: "Managed Redis", service_bus_namespace: "Message queues",
  eventhub_namespace: "Event streaming", load_balancer: "Load balancers", application_gateway: "Layer-7 gateway",
  key_vault: "Secrets & keys", container_instance: "Serverless containers", cdn_profile: "CDN / Front Door",
  vpn_gateway: "VPN gateways", data_factory: "Data pipelines", managed_disk: "Block storage volumes",
};

const PALETTE = ["#2bb3ac", "#38bdf8", "#7c6ee0", "#fbbf24", "#34d399", "#f472b6", "#22c55e", "#f59e0b", "#a78bfa", "#e879f9"];

// Every tile now navigates to /accounts/:id/<service> regardless of
// whether that service has a bespoke chart page or not — the decision
// between the two detail components (ServiceDetail.jsx vs the generic
// GenericServiceDetail.jsx) happens one layer down, in
// ServiceDetailRouter.jsx (see hasCoreDetailPage() there). This file
// only needs to know whether to show a tile at all — never which kind
// of page it opens into — so no service allowlist lives here anymore.

// NOTE (2026-09-20): tile alert badges used to be attributed IN THE BROWSER by
// guessing a service from resource-id substrings (`startsWith("i-")`,
// `includes("s3")`...) over EVERY account's alerts. That mis-attributed alerts
// across accounts, missed any bucket whose name lacked "s3", and had no rule
// at all for extended/directory services. The counts now come from the
// server's canonical rollup (GET /api/alerts/summary?account_id=), keyed by
// the alert's real resource_type -- identical to the Overview banner and the
// Alerts tabs.

export default function ServiceList() {
  const { id }    = useParams();
  const navigate  = useNavigate();
  const [account, setAccount] = useState(null);
  const [groups,  setGroups]  = useState([]);
  const [alertSummary, setAlertSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  // Real per-service resource counts, from the shared `resources` table
  // (see GET /api/live/resource-counts/{id} and the backend comment on
  // live_resource_counts) — populated by every provider's discovery
  // pipeline, not just AWS. null = not loaded yet (used only to avoid a
  // flash of every tile before the first fetch resolves). Once loaded,
  // a service with no rows for this account genuinely has zero
  // resources right now — there's no separate "unknown/failed" state
  // to worry about anymore, since this reads one DB table instead of
  // making 41 independent live API calls that could each fail on their
  // own. See the note on the fetch effect below for the accuracy
  // trade-off this brings (freshness = last discovery cycle, not
  // live-to-the-second; and stale rows for since-deleted cloud
  // resources aren't pruned yet).
  const [resourceCounts, setResourceCounts] = useState(null);
  // Which Core/Extended/Directory sections are collapsed -- mirrors
  // MetricSelector's own default (directory starts collapsed, since it's
  // the "discover more services live" tier and typically the longest
  // list once an account has been running a while). See the render
  // block below for why this page groups tiles into sections at all:
  // previously every enabled service (any provider, any tier) rendered
  // as one flat, unsorted grid, so a handful of core services (EC2, RDS)
  // could be scattered between dozens of extended/directory tiles with
  // no visual grouping to tell them apart -- the same core/extended/
  // directory distinction Settings -> Metrics already uses everywhere
  // else in this app.
  const [collapsedSection, setCollapsedSection] = useState(() => new Set(["directory"]));

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(`/api/admin/accounts/${id}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d && !cancelled) setAccount(d); })
      .catch(console.error);
    const loadAlertSummary = () => getAlertSummary(id)
      .then(d => { if (!cancelled) setAlertSummary(d?.accounts?.[String(id)] ?? null); })
      .catch(() => {});
    loadAlertSummary();
    const alertTimer = setInterval(loadAlertSummary, 30000);
    getAccountMetrics(id)
      .then(g => { if (!cancelled) setGroups(Array.isArray(g) ? g : []); })
      .catch(console.error)
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; clearInterval(alertTimer); };
  }, [id]);

  const provider = account?.provider || "aws";

  // Fetch real resource counts for this account. This now reads from
  // the shared, cross-provider `resources` table on the backend (see
  // live_resource_counts's comment) rather than making live AWS-only
  // API calls, so it applies the same way to AWS, GCP, and Azure
  // accounts — the old "only for AWS accounts" gate has been removed.
  // Runs independently of the main load above so a slow fetch never
  // blocks the page from rendering.
  useEffect(() => {
    if (!account) return;
    let cancelled = false;
    getResourceCounts(id).then(c => { if (!cancelled) setResourceCounts(c ?? {}); }).catch(() => {});
    return () => { cancelled = true; };
  }, [account, id]);

  const hasAnyMetricsEnabled = groups.some(g => (g.metrics || []).some(m => m.enabled));

  // Dynamic, aligned with the metric selector, for EVERY provider and
  // EVERY tier (core + extended) — no per-service allowlist decides
  // whether a tile can exist. A tile shows if:
  //   1. It has at least one metric enabled for THIS account (the same
  //      selection made in Settings -> Metrics), AND
  //   2. Either resourceCounts hasn't loaded yet (avoid a flash of
  //      nothing before the first fetch resolves), OR the account has
  //      a confirmed positive resource count for it.
  //
  // getResourceCounts() now reads from the shared `resources` table
  // (populated by AWS core discovery, AWS extended discovery, GCP
  // discovery, and Azure discovery alike — see the backend comment on
  // live_resource_counts) instead of live-calling 41 separate AWS APIs.
  // That removes the old null-vs-zero ambiguity entirely: a service
  // with no rows in `resources` for this account IS a confirmed zero,
  // not an unknown/failed check — there's no more silent-failure case
  // where a broken collector looked identical to "nothing here".
  const activeServices = useMemo(() => {
    return groups
      .filter(g => (g.metrics || []).some(m => m.enabled))
      .filter(g => {
        if (!resourceCounts) return true; // still loading — avoid a flash of nothing
        const count = resourceCounts[g.service];
        return typeof count === "number" && count > 0;
      })
      .map((g, i) => {
        const resourceCount = resourceCounts
          ? (resourceCounts[g.service] ?? 0)
          : null;
        return {
          id: g.service,
          label: g.display_service || g.service,
          desc: DESC_OVERRIDES[g.service] || (g.category === "core" ? "Core service" : "Extended service"),
          color: PALETTE[i % PALETTE.length],
          enabledCount: g.metrics.filter(m => m.enabled).length,
          resourceCount,
          category: g.category || "core",
        };
      });
  }, [groups, resourceCounts]);

  // Per-service counts of FIRING alerts (server rollup). Falls back to zero
  // until the first response so tiles never flash a wrong number.
  function alertsForService(svcId) {
    const v = alertSummary?.services?.[svcId];
    return {
      critical: v?.critical ?? 0, warning: v?.warning ?? 0, info: v?.info ?? 0,
      stale: v?.stale ?? 0,
    };
  }

  // Group into the same core/extended/directory sections MetricSelector
  // uses in Settings -> Metrics -- see sectionMeta()'s shared docstring
  // for why the labels/hints come from that one function instead of a
  // second copy here. A service missing a recognized category (shouldn't
  // happen -- metric_catalog.category is NOT NULL -- but this list is
  // rendered off live data, not a compile-time contract) falls back to
  // "core" rather than silently vanishing from every section.
  const SECTION_ORDER = ["core", "extended", "directory"];
  const SECTION_META = useMemo(() => sectionMeta(provider), [provider]);
  const sectionedServices = useMemo(() => {
    const bySection = { core: [], extended: [], directory: [] };
    activeServices.forEach(svc => {
      (bySection[svc.category] || bySection.core).push(svc);
    });
    return bySection;
  }, [activeServices]);

  function toggleSection(section) {
    setCollapsedSection(prev => {
      const next = new Set(prev);
      next.has(section) ? next.delete(section) : next.add(section);
      return next;
    });
  }

  return (
    <div style={{ maxWidth: 1100 }}>
      <div className="breadcrumb">
        <span className="bc-link" onClick={() => navigate("/overview")}>ALL ACCOUNTS</span>
        <span className="bc-sep">›</span>
        <span className="bc-link" onClick={() => navigate(`/accounts/${id}`)}>{account?.account_name ?? `Account ${id}`}</span>
        <span className="bc-sep">›</span>
        <span className="bc-current">SERVICES</span>
      </div>

      <div style={{ marginBottom:32, display:"flex", justifyContent:"space-between", alignItems:"flex-start" }}>
        <div>
          <h1 style={{ fontSize:24, fontWeight:700, marginBottom:5, letterSpacing:"-0.01em", display:"flex", alignItems:"center", gap:10 }}>
            {provider === "azure" && <AzureBrandLogo size={22} />}
            {account?.account_name ?? "Account"}
            <span style={{ color:"var(--accent)", marginLeft:8 }}>/ Services</span>
          </h1>
          <p style={{ color:"var(--text-muted)", fontSize:12 }}>
            {account?.account_id} · {account?.default_region} · {activeServices.length} service{activeServices.length === 1 ? "" : "s"} selected for monitoring
          </p>
        </div>
        <div style={{ display:"flex", gap:10, flexShrink:0 }}>
          <button
            onClick={() => navigate(`/accounts/${id}/topology`)}
            style={{
              display:"flex", alignItems:"center", gap:6, background:"var(--accent-dim)",
              border:"1px solid rgba(43,179,172,.3)", color:"var(--accent)", padding:"8px 16px",
              borderRadius:"var(--radius)", fontSize:13, fontWeight:600, cursor:"pointer",
            }}
          >
            <LinkIcon size={14} /> Resource Topology
          </button>
          {/* AIOps Incidents button hidden from the UI (2026-09-14) --
              this product's end users aren't ops/dev people with code
              access and don't need internal correlation/RCA surfaced
              to them; product focus stays on cloud/metrics/resource
              monitoring, matching the same "hidden but not deleted"
              treatment already applied to Operational Events in
              Layout.jsx (route/page/API/backend collector jobs all
              stay fully intact at /accounts/:id/incidents, just not
              discoverable from here). See Layout.jsx's own comment
              for the established convention this follows. */}
        </div>
      </div>

      {loading ? (
        <div style={{ color:"var(--text-muted)", fontSize:13, padding:"40px 0", textAlign:"center" }}>Loading services…</div>
      ) : activeServices.length === 0 ? (
        <div style={{
          border:"1px dashed var(--border)", borderRadius:"var(--radius-lg)", padding:"40px 24px",
          textAlign:"center", color:"var(--text-muted)", fontSize:13,
        }}>
          {hasAnyMetricsEnabled ? (
            <>Metrics are enabled for this account, but no matching resources were found
            right now — this list updates automatically once resources appear.</>
          ) : (
            <>No services are enabled for this account yet. Go to <b style={{color:"var(--text-secondary)"}}>Settings → Metrics</b> to select
            which services and metrics to monitor — this page always mirrors that selection.</>
          )}
        </div>
      ) : (
        <div style={{ display:"flex", flexDirection:"column", gap:20 }}>
          {SECTION_ORDER.map(sectionKey => {
            const list = sectionedServices[sectionKey];
            if (!list || list.length === 0) return null;
            const meta = SECTION_META[sectionKey];
            const isCollapsed = collapsedSection.has(sectionKey);
            return (
              <div key={sectionKey} className="ms-section">
                <button type="button" className="ms-section-header" onClick={() => toggleSection(sectionKey)}>
                  <span className={`ms-section-chevron ${isCollapsed ? "" : "ms-section-chevron-open"}`}>▸</span>
                  <span className={`ms-section-dot ms-section-dot-${sectionKey}`} />
                  <span className="ms-section-label">{meta.label}</span>
                  <span className="ms-section-hint">{meta.hint}</span>
                  <span className="ms-section-spacer" />
                  <span className="ms-section-count">{list.length} service{list.length === 1 ? "" : "s"}</span>
                </button>
                {!isCollapsed && (
                  <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fill, minmax(320px, 1fr))", gap:10 }}>
                    {list.map(svc => {
                      const svcAlerts = alertsForService(svc.id);
                      return (
                        <ServiceCard key={svc.id} svc={svc} provider={provider}
                          criticalCount={svcAlerts.critical}
                          warningCount={svcAlerts.warning}
                          onClick={() => navigate(`/accounts/${id}/${svc.id}`)} />
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ServiceCard({ svc, provider, onClick, criticalCount, warningCount }) {
  const [hovered, setHovered] = useState(false);
  const alertCount = criticalCount + warningCount;
  const hasCritical = criticalCount > 0;
  const alertColor = hasCritical ? "var(--red)" : "var(--yellow)";
  // Was a single combined count labeled with just one severity word (e.g.
  // "4 critical" when only 1 of those 4 was actually critical and the
  // other 3 were warnings) -- see 2026-09-15 fix. Now shows both counts
  // whenever both are present, matching the "N CRITICAL · M WARNING"
  // pattern the Overview banner and account card already use, so this
  // badge can never overstate (or understate) either severity.
  const badgeLabel = criticalCount > 0 && warningCount > 0
    ? `${criticalCount} critical · ${warningCount} warning`
    : criticalCount > 0
      ? `${criticalCount} critical`
      : `${warningCount} warning`;
  return (
    <div onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        background:   hovered ? "var(--bg-card-hover)" : "var(--bg-card)",
        border:       `1px solid ${alertCount > 0 ? alertColor : hovered ? "var(--border-bright)" : "var(--border)"}`,
        borderLeft:   `3px solid ${alertCount > 0 ? alertColor : svc.color}`,
        borderRadius: "var(--radius)", padding: "18px 18px 16px",
        cursor: "pointer", transition: "background .15s, border-color .15s",
        display: "flex", alignItems: "center", gap: 14,
      }}>
      <div style={{
        width:40, height:40, borderRadius:"var(--radius)", flexShrink: 0,
        background: svc.color+"14", border: `1px solid ${svc.color}30`,
        display:"flex", alignItems:"center", justifyContent:"center",
      }}>
        <CloudServiceIcon provider={provider} service={svc.id} size={20} />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 2 }}>
          <span style={{ fontWeight:600, fontSize:14, color:"var(--text-primary)" }}>{svc.label}</span>
          {alertCount > 0 && (
            <span style={{
              fontSize:10, fontWeight:700, borderRadius:4, padding:"1px 6px",
              fontFamily:"var(--font-mono)", color: alertColor, background: alertColor+"1a",
            }}>
              {badgeLabel}
            </span>
          )}
        </div>
        <div style={{ fontSize:12, color:"var(--text-muted)", marginBottom:4 }}>{svc.desc}</div>
        <div style={{ fontSize:11, color:"var(--text-muted)", fontFamily:"var(--font-mono)" }}>
          {svc.resourceCount != null && (
            <>{svc.resourceCount} resource{svc.resourceCount === 1 ? "" : "s"} · </>
          )}
          {svc.enabledCount} metric{svc.enabledCount === 1 ? "" : "s"} enabled
        </div>
      </div>
      <div style={{
        fontSize:11, fontFamily:"var(--font-mono)", fontWeight:600,
        color: hovered ? svc.color : "var(--text-muted)", letterSpacing:"0.02em",
        whiteSpace: "nowrap", flexShrink: 0,
      }}>
        Open →
      </div>
    </div>
  );
}
