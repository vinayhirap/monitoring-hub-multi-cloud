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
  // Real per-service resource counts from AWS (core services only —
  // see GET /api/live/resource-counts/{id}). null = not loaded yet;
  // core tiles fail OPEN (stay visible) until we actually know a
  // count is a confirmed zero — a slow/failed fetch never hides a
  // tile that may well have real resources.
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
  }

  const hasAnyMetricsEnabled = groups.some(g => (g.metrics || []).some(m => m.enabled));

  // Dynamic, aligned with the metric selector: a service tile only shows up
  // here if it has at least one metric enabled for THIS account — the same
  // selection made during onboarding or later edited in Settings -> Metrics.
  // On top of that, ANY service (core or extended) is hidden if we have a
  // real, confirmed-zero resource count for it — see GET
  // /api/live/resource-counts/{id}, which now covers both tiers. A service
  // still missing a collector (key absent from that response) or a
  // non-AWS account (resourceCounts never populated) fails OPEN and stays
  // visible, since "unknown" is not the same as "confirmed none".
  const activeServices = useMemo(() => {
    return groups
      .filter(g => (g.metrics || []).some(m => m.enabled))
      .filter(g => {
        if (!resourceCounts) return true;
        const count = resourceCounts[g.service];
        return count === undefined || count === null || count > 0;
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
  }, [groups, resourceCounts]);

  const activeAlerts = alerts.filter(a => (a.status || "").toLowerCase() === "active");

  function alertsForService(svcId) {
    const match = alertMatcher(provider, svcId);
    if (!match) return [];
    return activeAlerts.filter(a => match(a.resource));
  }

  return (
    <div style={{ maxWidth: 1100 }}>
      <div style={{ display:"flex", alignItems:"center", gap:6, marginBottom:20, fontSize:11, color:"var(--text-muted)", fontFamily:"var(--font-mono)", letterSpacing:"0.06em" }}>
        <span style={{ cursor:"pointer", color:"var(--accent)" }} onClick={() => navigate("/overview")}>ALL ACCOUNTS</span>
        <span style={{ opacity:.4 }}>›</span>
        <span style={{ color:"var(--text-secondary)" }}>{account?.account_name ?? `Account ${id}`}</span>
        <span style={{ opacity:.4 }}>›</span>
        <span>SERVICES</span>
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
      )}
    </div>
  );
}

function ServiceCard({ svc, provider, onClick, alertCount, hasCritical, routable = true, isConsoleLoading = false }) {
  const [hovered, setHovered] = useState(false);
  const alertColor = hasCritical ? "#ef4444" : "#f59e0b";
  return (
    <div onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        background:   hovered ? "var(--bg-card-hover)" : "var(--bg-card)",
        border:       `1px solid ${alertCount > 0 ? alertColor+"50" : hovered ? svc.color+"60" : "var(--border)"}`,
        borderRadius: "var(--radius-lg)", padding: "28px 20px 22px",
        cursor: "pointer", transition: "all .18s ease", textAlign: "center",
        position: "relative", overflow: "hidden",
        transform: hovered ? "translateY(-4px)" : "translateY(0)",
        boxShadow: hovered ? `0 12px 32px ${svc.color}20` : "none",
      }}>
      {alertCount > 0 && (
        <div style={{
          position:"absolute", top:10, right:10,
          background: alertColor, color:"#fff",
          fontSize:10, fontWeight:700, borderRadius:10, padding:"2px 7px",
          fontFamily:"var(--font-mono)", zIndex:2,
        }}>
          {hasCritical ? "🔴" : "⚠️"} {alertCount}
        </div>
      )}
      <div style={{ position:"absolute", top:0, left:0, right:0, height:3,
        background:`linear-gradient(90deg,${svc.color}00,${svc.color},${svc.color}00)`,
        opacity: hovered ? 1 : 0.35, transition:"opacity .18s" }}/>
      <div style={{
        width:64, height:64, borderRadius:"50%",
        background: hovered ? svc.color+"25" : svc.color+"15",
        borderWidth:1, borderStyle:"solid",
        borderColor: hovered ? svc.color+"50" : svc.color+"25",
        display:"flex", alignItems:"center", justifyContent:"center",
        margin:"0 auto 14px", transition:"background .18s, border-color .18s",
      }}>
        <CloudServiceIcon provider={provider} service={svc.id} size={32} />
      </div>
      <div style={{ fontWeight:700, fontSize:15, color:"var(--text-primary)", marginBottom:6 }}>{svc.label}</div>
      <div style={{ fontSize:12, color:"var(--text-muted)", lineHeight:1.5, marginBottom:6 }}>{svc.desc}</div>
      <div style={{ fontSize:10, color:"var(--text-muted)", opacity:.7, marginBottom:10, fontFamily:"var(--font-mono)" }}>
        {svc.resourceCount != null && (
          <>{svc.resourceCount} resource{svc.resourceCount === 1 ? "" : "s"} · </>
        )}
        {svc.enabledCount} metric{svc.enabledCount === 1 ? "" : "s"} enabled
      </div>
      <div style={{
        display:"inline-flex", alignItems:"center", gap:5,
        fontSize:10, fontFamily:"var(--font-mono)", fontWeight:700,
        color:svc.color, letterSpacing:"0.08em",
        background:svc.color+"12", border:`1px solid ${svc.color}30`,
        borderRadius:20, padding:"4px 12px",
        opacity: hovered ? 1 : 0.6, transition:"all .18s",
      }}>
        {routable ? "OPEN →" : (isConsoleLoading ? "OPENING…" : "VIEW IN CONSOLE ↗")}
      </div>
    </div>
  );
}
