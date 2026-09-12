// monitoring-hub/frontend/src/pages/ServiceList.jsx
import { useEffect, useState, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { getAlerts, getAccountMetrics, getResourceCounts, getConsoleUrl } from "../api/api";
import { CloudServiceIcon, AzureBrandLogo, officialPerService } from "../components/cloud-icons";

// Short blurbs for the services we know about. Anything not listed here
// (e.g. a directory-tier service the account onboarded via live discovery)
// falls back to its category ("core service" / "extended service") rather
// than a made-up description.
const DESC_OVERRIDES = {
  ec2: "Compute instances", ebs: "Block storage volumes", rds: "Managed databases",
  s3: "Object storage buckets", ecs: "Container services", elb: "Load balancers", alb: "Load balancers", lambda: "Serverless functions",
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

// Services with a real backend resource-list + detail page (see
// app/api/live_data.py + the /accounts/:id/<service> routes in
// App.jsx). A tile for any other ("extended") service opens the AWS
// Console directly instead of navigating internally, since there's no
// detail page for it yet — see openInConsole() below.
const CORE_AWS_SERVICES = new Set(["ec2", "ebs", "rds", "lambda", "s3", "elb", "alb", "ecs"]);

// Real-shape resource-id/ARN patterns per provider, used to attribute
// active alerts to the right service tile — NOT hardcoded to AWS only.
function alertMatcher(provider, service) {
  if (provider === "aws") {
    return {
      ec2: r => r?.startsWith("i-"), ebs: r => r?.startsWith("vol-"),
      rds: r => r?.includes("rds") || r?.includes("db-") || r?.startsWith("db"),
      lambda: r => r?.includes("lambda") || r?.startsWith("arn:aws:lambda"),
      elb: r => r?.includes("alb") || r?.includes("elb") || r?.includes("loadbalancer"),
      alb: r => r?.includes("alb") || r?.includes("elb") || r?.includes("loadbalancer"),
      s3: r => r?.includes("s3"), ecs: r => r?.includes("ecs"),
    }[service];
  }
  if (provider === "gcp") {
    return {
      compute_instance: r => r?.includes("/zones/") && r?.includes("/instances/"),
      gcs_bucket: r => r?.includes("/buckets/"),
      cloudsql_instance: r => r?.includes("/instances/") && !r?.includes("/zones/"),
      cloud_run_service: r => r?.includes("/services/"),
    }[service];
  }
  if (provider === "azure") {
    return {
      vm: r => r?.includes("Microsoft.Compute/virtualMachines"),
      storage_account: r => r?.includes("Microsoft.Storage/storageAccounts"),
      sql_database: r => r?.includes("Microsoft.Sql/servers"),
      app_service: r => r?.includes("Microsoft.Web/sites"),
      aks_cluster: r => r?.includes("Microsoft.ContainerService"),
    }[service];
  }
  return null;
}

export default function ServiceList() {
  const { id }    = useParams();
  const navigate  = useNavigate();
  const [account, setAccount] = useState(null);
  const [groups,  setGroups]  = useState([]);
  const [alerts,  setAlerts]  = useState([]);
  const [loading, setLoading] = useState(true);
  // Real per-service resource counts from AWS — see GET
  // /api/live/resource-counts/{id}. null = not loaded yet (used only to
  // avoid a flash of every tile before the first fetch resolves). Once
  // loaded, a tile shows ONLY if we have a confirmed count > 0 for it.
  // A missing/failed count (undefined, or a collector that threw — e.g.
  // an AccessDenied on that one service) is treated the same as zero and
  // hidden. This is a deliberate choice: it trades "never hide a tile
  // that might have real resources" for "never show a tile that doesn't
  // have any" — so a flaky/under-permissioned collector for one service
  // will make that tile disappear rather than stay visible. If a tile
  // you expect to see goes missing, check the backend logs for a
  // "resource-counts: <svc> failed" warning — that's usually an IAM
  // permissions gap on that specific service, not a real zero.
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
  // the collectors behind this endpoint are AWS-only. This runs
  // independently of the main load above so a slow AWS call never
  // blocks the page from rendering.
  useEffect(() => {
    if (!account || (account.provider || "aws") !== "aws") return;
    let cancelled = false;
    getResourceCounts(id).then(c => { if (!cancelled) setResourceCounts(c ?? {}); }).catch(() => {});
    return () => { cancelled = true; };
  }, [account, id]);

  function openInConsole(serviceId) {
    // Previously bailed out here for any non-AWS provider. The backend
    // endpoint this calls (getConsoleUrl -> /api/admin/accounts/{id}/
    // console-url) already dispatches through get_provider() and works
    // for Azure/GCP too -- Azure generically, GCP with real per-service
    // deep links for 10 of 16 curated types. Removed the bailout; the
    // existing catch below already surfaces a clear error if a
    // particular service genuinely has no console link available yet.
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
  }

  const hasAnyMetricsEnabled = groups.some(g => (g.metrics || []).some(m => m.enabled));

  // Dynamic, aligned with the metric selector: a service tile only shows up
  // here if it has at least one metric enabled for THIS account — the same
  // selection made during onboarding or later edited in Settings -> Metrics
  // — AND (for AWS accounts) BOTH a confirmed positive resource count AND a
  // real internal detail page (CORE_AWS_SERVICES) it can open into. AWS
  // services with no detail page only ever had a "VIEW IN CONSOLE" tile
  // that sends you off to the AWS Console — that's been dropped entirely,
  // on purpose: this page now only shows tiles you can click straight into
  // with real data behind them, never a console-link placeholder. Non-AWS
  // accounts have no resource-count data source yet, so they always show.
  const activeServices = useMemo(() => {
    const isAws = provider === "aws";
    return groups
      .filter(g => (g.metrics || []).some(m => m.enabled))
      .filter(g => {
        if (!isAws) return true;                    // no resource-count data for GCP/Azure
        if (!CORE_AWS_SERVICES.has(g.service)) return false; // no console-link tiles, ever
        if (!resourceCounts) return true;            // still loading — avoid a flash of nothing
        const count = resourceCounts[g.service];
        return typeof count === "number" && count > 0;
      })
      .map((g, i) => {
        const resourceCount = resourceCounts
          ? (resourceCounts[g.service] ?? null)
          : null;
        return {
          id: g.service,
          label: g.display_service || g.service,
          desc: DESC_OVERRIDES[g.service] || (g.category === "core" ? "Core service" : "Extended service"),
          color: PALETTE[i % PALETTE.length],
          enabledCount: g.metrics.filter(m => m.enabled).length,
          resourceCount,
        };
      });
  }, [groups, resourceCounts, provider]);

  const activeAlerts = alerts.filter(a => (a.status || "").toLowerCase() === "active");

  function alertsForService(svcId) {
    const match = alertMatcher(provider, svcId);
    if (!match) return [];
    return activeAlerts.filter(a => match(a.resource));
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
      </div>

      {loading ? (
        <div style={{ color:"var(--text-muted)", fontSize:13, padding:"40px 0", textAlign:"center" }}>Loading services…</div>
      ) : activeServices.length === 0 ? (
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
        <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fill, minmax(320px, 1fr))", gap:10 }}>
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
      )}
    </div>
  );
}

function ServiceCard({ svc, provider, onClick, alertCount, hasCritical, routable = true, isConsoleLoading = false }) {
  const [hovered, setHovered] = useState(false);
  const alertColor = hasCritical ? "var(--red)" : "var(--yellow)";
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
              {alertCount} {hasCritical ? "critical" : "warning"}
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
        {routable ? "Open →" : (isConsoleLoading ? "Opening…" : "View in console ↗")}
      </div>
    </div>
  );
}
