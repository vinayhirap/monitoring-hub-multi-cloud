// src/pages/Topology.jsx
//
// Roadmap phase 4/7 (2026-09-13). Rebuilt from the original version,
// which positioned EVERY resource in the account (not just ones with a
// tracked relationship) into a handful of columns -- for an account
// with ~90 resources and only a few real edges, that produced a wall of
// mostly-unrelated boxes nobody could usefully scan, with no visible
// connecting lines in the areas that mattered. This version:
//
//   - Only resources that are actually part of at least one edge are
//     drawn in the graph. Everything else collapses into a single
//     "N other resources" disclosure below it -- still visible, not
//     hidden, just not competing for space with the real topology.
//   - Edges are real curved SVG paths with arrowheads (not straight
//     lines only drawn when both endpoints happen to already be
//     adjacent columns), color/dash-coded by source (auto vs manual),
//     with hover-to-highlight so a busy graph is still readable.
//   - A resource_id with no matching row in `resources` (the RCA
//     tie-in documented in app/api/topology.py) renders as its own
//     dashed "unresolved" node instead of only being listed in a
//     separate text banner, so the gap is visible in the graph itself.
//   - Manual-edge management uses dropdowns populated from this
//     account's actual resources instead of free-text resource-ID
//     inputs, which were easy to typo and gave no feedback until the
//     save actually failed.
//   - ENI resources are excluded entirely (nodes AND any edge touching
//     one) -- they're a supporting/plumbing resource type that adds
//     graph noise without adding useful topology information for this
//     view; every account has one per attached interface and they
//     rarely represent a relationship anyone is trying to understand.
//   - Clicking a node PINS it (persists its connection highlight
//     regardless of where the mouse moves afterward) rather than
//     navigating immediately -- the original hover-only highlight
//     reset the instant the cursor left the node, which made it
//     impossible to trace a connection down a long column: scrolling
//     the page moves the node out from under the (stationary) cursor,
//     clearing the highlight before you could follow it. A dedicated
//     small link icon on each node with a real detail route (see
//     detailRoute() below) is the explicit way to navigate now; the
//     node body itself only pins/unpins.
//   - Node icons and column placement are provider-aware (AWS/Azure/
//     GCP resource-type keys all map to a sensible tier), since an
//     account onboarded as Azure or GCP renders its own topology here
//     too, not just AWS accounts.
//   - "Add dependency" / delete controls only render for a user with
//     topology.manage (see db/migrations/024_topology_manage_permission.sql)
//     -- previously any viewer could mutate manually-declared edges
//     because the write endpoints rode along on the same permission as
//     the read-only view.
//
// 2026-09-17 audit ("I want every resource in the account, not just
// this pipeline, and aligned for GCP/Azure too"): traced this all the
// way back through discovery (app/collector/discovery/runner.py +
// extended.py, app/providers/azure/discovery.py,
// app/providers/gcp/discovery.py) and the icon layer
// (components/cloud-icons.jsx) before touching anything here --
// discovery already writes every one of the ~42 AWS / 19 Azure / 16
// GCP catalogued resource types into `resources` (see each file's own
// docstring), and every one of those already has a real icon with a
// sane fallback. The account in the screenshot that prompted this
// really does have all 81 of its resources in `resources` and visible
// in the "N other resources" disclosure below -- nothing was silently
// dropped there. Three real gaps, fixed here:
//   1. TIERS below only explicitly categorized a subset of AWS types
//      and a much smaller subset of Azure/GCP types -- everything else
//      silently fell into TIER_FALLBACK ("Compute"), so e.g. an SQS
//      queue or a KMS key rendered in the Compute column. Every
//      catalogued type across all three providers is now explicitly
//      placed, plus a new "Security / Identity" tier for
//      kms/certificatemanager/cognito/key_vault, which had no honest
//      home in the previous three tiers.
//   2. detailRoute()/ROUTE_SEGMENT_BY_TYPE assumed only 7 hardcoded AWS
//      service pages existed and returned null (no link) for anything
//      else, including every Azure/GCP resource. That was true when
//      this file was first written, but ServiceDetailRouter.jsx +
//      GenericServiceDetail.jsx (added since) now give EVERY service
//      key, for every provider, a real in-app detail page -- the route
//      segment is just the resource_type itself, matching the exact
//      convention ServiceList.jsx's own ServiceCard links already use
//      (`/accounts/${id}/${svc.id}`, svc.id === resource_type). The
//      allowlist is gone; every non-hidden resource type is clickable
//      now, on every provider.
//   3. The "N other resources" disclosure defaulted to collapsed,
//      putting most of an account's inventory one extra click away
//      from "each and every resource" -- now defaults open. The main
//      graph itself is deliberately still edges-only (see the header
//      note above this one) -- that decluttering call was sound and
//      isn't reversed here, only the visibility of what's *not* in it.
//
// Deliberately still no graph-layout library (react-flow etc.) --
// frontend/package.json has zero graph dependencies today, and a fixed
// tier-column layout is enough for the shapes this data actually takes
// (a handful of load-balancer/compute/data edges per account). Revisit
// with a real layout engine only if that stops being true.
import { useState, useEffect, useCallback, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { getTopology, addManualEdge, deleteManualEdge } from "../api/api";
import { useAuth } from "../auth/AuthContext";
import { CloudServiceIcon } from "../components/cloud-icons";
import { AlertTriangleIcon, TrashIcon, PlusIcon, InfoIcon, ExternalLinkIcon, XIcon } from "../components/icons";
import "./Topology.css";

// Which column a resource_type lands in. Every type discovery actually
// writes for AWS (app/collector/discovery/runner.py + extended.py),
// Azure (app/providers/azure/discovery.py) and GCP
// (app/providers/gcp/discovery.py) is listed explicitly below --
// cross-checked 2026-09-17 against all three files plus each
// provider's metric_catalog_data.py, so nothing should be silently
// falling through to TIER_FALLBACK anymore. A brand-new service type
// added later without a matching entry here still renders (icon +
// fallback tier), it just won't be perfectly placed until this list is
// updated too.
const TIERS = [
  {
    key: "entry", label: "Entry / Routing",
    types: [
      // aws
      "elb", "alb", "nlb", "cloudfront", "apigateway", "route53",
      "globalaccelerator", "natgateway", "transitgateway", "vpn",
      "directconnect", "wafv2",
      // azure
      "load_balancer", "application_gateway", "cdn_profile", "vpn_gateway",
      // gcp
      "cloud_lb", "nat_gateway",
    ],
  },
  {
    key: "compute", label: "Compute",
    types: [
      // aws
      "ec2", "lambda", "ecs", "ecs_service", "eks", "autoscaling", "states",
      // azure
      "vm", "vmss", "app_service", "aks_cluster", "function_app", "container_instance",
      // gcp
      "compute_instance", "cloud_run_service", "gke_cluster", "gke_node", "cloudfunctions_function",
    ],
  },
  {
    key: "data", label: "Data / Storage / Messaging",
    types: [
      // aws
      "ebs", "rds", "s3", "dynamodb", "elasticache", "efs", "redshift",
      "opensearch", "documentdb", "neptune", "sqs", "sns", "kinesis",
      "firehose", "msk", "memorydb", "dax", "events", "backup", "dms", "logs",
      // azure
      "storage_account", "sql_database", "cosmosdb_account", "redis_cache",
      "service_bus_namespace", "eventhub_namespace", "managed_disk", "data_factory",
      // gcp
      "gce_persistent_disk", "gcs_bucket", "cloudsql_instance", "firestore_database",
      "bigquery_project", "spanner_instance", "redis_instance", "pubsub_topic", "pubsub_subscription",
    ],
  },
  {
    // New 2026-09-17: kms/certificatemanager/cognito (aws) and
    // key_vault (azure) had no honest home in the three tiers above --
    // they aren't compute, storage, or an entry point -- and were
    // landing in Compute purely via TIER_FALLBACK. No GCP catalog key
    // maps here today (GCP's catalog has no dedicated KMS/Secret
    // Manager entry), which is simply reality, not an omission.
    key: "security", label: "Security / Identity",
    types: ["kms", "certificatemanager", "cognito", "key_vault"],
  },
];
const TIER_FALLBACK = "compute";
// Excluded entirely -- see file header. Not just "no branded icon", not
// drawn at all: filtered out of nodes, edges, and the others-list below.
const HIDDEN_RESOURCE_TYPES = new Set(["eni"]);

const NODE_W = 208, NODE_H = 60, COL_GAP = 130, ROW_GAP = 20, PAD = 30;

function tierIndexOf(resourceType) {
  const i = TIERS.findIndex(t => t.types.includes(resourceType));
  return i === -1 ? TIERS.findIndex(t => t.key === TIER_FALLBACK) : i;
}

// 2026-09-17: was a small hardcoded AWS-only allowlist (7 service
// keys) that returned null -- no link at all -- for everything else,
// including every Azure/GCP resource. That was correct when it was
// written (ServiceDetail.jsx really was the only detail page and only
// covered those 7), but ServiceDetailRouter.jsx now sends every other
// service key to GenericServiceDetail.jsx, which works for any
// provider -- see that router's own docstring. The route segment is
// simply the resource_type itself, exactly the convention
// ServiceList.jsx's own ServiceCard links already use
// (`/accounts/${id}/${svc.id}`, svc.id === resource_type), so no
// lookup table is needed at all anymore: any non-hidden, non-ghost
// node is clickable now, on every provider.
function detailRoute(node, accountId) {
  if (!node || node.ghost) return null;
  return `/accounts/${accountId}/${node.resource_type}`;
}

function StateDot({ state }) {
  const s = (state || "").toLowerCase();
  const cls = s === "running" ? "td-green" : s === "stopped" ? "td-muted" : s.includes("term") ? "td-red" : null;
  if (!cls) return null;
  return <span className={`td-dot ${cls}`} title={state} />;
}

function NodeCard({ node, active, dimmed, pinned, onHover, onLeave, onSelect, provider, onOpen }) {
  const isGhost = node.ghost;
  return (
    <div
      className={`topo-node topo-node-selectable ${active ? "topo-node-hovered" : ""} ${dimmed ? "topo-node-dimmed" : ""} ${isGhost ? "topo-node-ghost" : ""} ${pinned ? "topo-node-pinned" : ""}`}
      style={{ left: node.x, top: node.y, width: NODE_W, height: NODE_H }}
      onMouseEnter={onHover}
      onMouseLeave={onLeave}
      onClick={onSelect}
      title={pinned ? "Click to clear selection" : "Click to trace this resource's connections"}
    >
      <span className="topo-node-icon">
        {isGhost
          ? <InfoIcon size={16} />
          : <CloudServiceIcon provider={provider} service={node.resource_type} size={20} />}
      </span>
      <div className="topo-node-body">
        <div className="topo-node-type">
          {node.resource_type}
          <StateDot state={node.instance_state} />
        </div>
        <div className="topo-node-name">{isGhost ? node.resource_id : (node.name || node.resource_id)}</div>
      </div>
      {onOpen && (
        <button
          className="topo-node-open"
          onClick={(e) => { e.stopPropagation(); onOpen(); }}
          title="View metrics"
        >
          <ExternalLinkIcon size={12} />
        </button>
      )}
    </div>
  );
}

export default function Topology() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { hasPermission } = useAuth();
  const canManage = hasPermission("topology.manage");
  const [data, setData] = useState(null);
  const [account, setAccount] = useState(null);
  const [error, setError] = useState(null);
  const [hoveredId, setHoveredId] = useState(null);
  // Persists a highlight regardless of where the mouse moves afterward
  // -- see file header. Takes precedence over hoveredId wherever both
  // could apply.
  const [pinnedId, setPinnedId] = useState(null);
  // 2026-09-17: defaults to expanded now -- see file header's audit
  // note. This is most of an account's inventory (81 of 81 resources
  // in the account that prompted this, only 21 tracked in an edge);
  // hiding it behind an extra click contradicted "every resource in
  // this account, not just the ones with a tracked relationship" being
  // the whole point of this disclosure existing at all. Still
  // collapsible for anyone who wants the shorter view back.
  const [showOthers, setShowOthers] = useState(true);
  const [addingEdge, setAddingEdge] = useState(false);
  const [form, setForm] = useState({ source: "", target: "" });
  const [saving, setSaving] = useState(false);
  // Matches the free describe-poll loop's own 30s cadence (see
  // app/main.py's _run_describe_poll_loop) -- a shorter interval here
  // would never see fresher data anyway, since that's how often
  // AWS-side auto-sync edges can actually change. Azure/GCP edges only
  // change at discovery time (onboarding / manual re-trigger), much
  // less often than 30s, but re-fetching this page's own data on the
  // same short interval is still correct for them -- it just usually
  // finds nothing new, which costs one cheap GET, not a real problem.
  const [autoRefresh, setAutoRefresh] = useState(true);

  const load = useCallback(() => {
    getTopology(id).then(setData).catch(e => setError(e.message));
  }, [id]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!autoRefresh) return;
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [autoRefresh, load]);
  useEffect(() => {
    fetch(`/api/admin/accounts/${id}`).then(r => r.ok ? r.json() : null).then(d => d && setAccount(d)).catch(() => {});
  }, [id]);

  const layout = useMemo(() => {
    if (!data) return null;

    const eniIds = new Set(data.nodes.filter(n => HIDDEN_RESOURCE_TYPES.has(n.resource_type)).map(n => n.resource_id));
    const nodes = data.nodes.filter(n => !HIDDEN_RESOURCE_TYPES.has(n.resource_type));
    const edges = data.edges.filter(e => !eniIds.has(e.source_resource_id) && !eniIds.has(e.target_resource_id));

    const byId = Object.fromEntries(nodes.map(n => [n.resource_id, n]));
    const connectedIds = new Set();
    edges.forEach(e => { connectedIds.add(e.source_resource_id); connectedIds.add(e.target_resource_id); });

    // Real nodes that participate in an edge, plus "ghost" placeholder
    // nodes for edge endpoints with no matching resources row (the
    // node_gap case app/api/topology.py's docstring calls out) -- these
    // still get drawn, just visually distinct, instead of the edge
    // silently vanishing because one end has nowhere to attach to.
    const connectedReal = [...connectedIds].filter(rid => byId[rid]).map(rid => byId[rid]);
    const ghostIds = [...connectedIds].filter(rid => !byId[rid]);
    const ghostNodes = ghostIds.map(rid => ({ resource_id: rid, resource_type: "unknown", ghost: true }));
    const allConnected = [...connectedReal, ...ghostNodes];

    const others = nodes.filter(n => !connectedIds.has(n.resource_id));

    const columns = TIERS.map(() => []);
    allConnected.forEach(n => columns[tierIndexOf(n.resource_type)].push(n));

    const positioned = {};
    let maxRows = 0;
    columns.forEach((col, ci) => {
      maxRows = Math.max(maxRows, col.length);
      col.forEach((n, ri) => {
        positioned[n.resource_id] = { ...n, x: ci * (NODE_W + COL_GAP) + PAD, y: ri * (NODE_H + ROW_GAP) + PAD, col: ci };
      });
    });

    const usedCols = TIERS.filter((_, i) => columns[i].length > 0).length || 1;
    const width  = usedCols * (NODE_W + COL_GAP) - COL_GAP + PAD * 2;
    const height = Math.max(1, maxRows) * (NODE_H + ROW_GAP) - ROW_GAP + PAD * 2;

    return { nodes, edges, positioned, others, width, height, columns };
  }, [data]);

  if (error) return <div className="topo-page"><div className="topo-error"><AlertTriangleIcon size={14} /> Failed to load topology: {error}</div></div>;
  if (!data || !layout) return <div className="topo-page"><div className="topo-loading">Loading topology…</div></div>;

  const gaps = layout.edges.filter(e => e.source_missing || e.target_missing);
  const manualEdges = layout.edges.filter(e => e.source === "manual");
  const provider = account?.provider || "aws";

  const activeId = pinnedId ?? hoveredId;
  const isEdgeActive = (e) => activeId && (e.source_resource_id === activeId || e.target_resource_id === activeId);
  const isNodeActive = (rid) => activeId && (activeId === rid || layout.edges.some(e =>
    (e.source_resource_id === activeId && e.target_resource_id === rid) ||
    (e.target_resource_id === activeId && e.source_resource_id === rid)
  ));

  const handleAddEdge = async (e) => {
    e.preventDefault();
    if (!form.source || !form.target || form.source === form.target) return;
    setSaving(true);
    try {
      await addManualEdge(id, form.source, form.target);
      setForm({ source: "", target: "" });
      setAddingEdge(false);
      load();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const nodeLabel = (n) => `${n.resource_type} — ${n.name || n.resource_id}`;

  return (
    <div className="topo-page">
      <div className="c-header">
        <div>
          <h1>Resource <span className="hl">Topology</span></h1>
          <p className="sub">{account?.account_name ? `${account.account_name} — ` : ""}{layout.edges.length} tracked relationship{layout.edges.length === 1 ? "" : "s"} across {layout.nodes.length} resources</p>
        </div>
        <div className="c-header-actions">
          <label className="topo-refresh-toggle" title="Auto-refresh every 30s">
            <input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} />
            <span className="topo-refresh-track"><span className="topo-refresh-thumb" /></span>
            <span className="topo-refresh-label">Auto-refresh</span>
          </label>
          <button className="topo-btn-back" onClick={() => navigate(`/accounts/${id}/services`)}>← Back to Services</button>
          {canManage && (
            <button className="c-btn-primary" onClick={() => setAddingEdge(v => !v)}>
              <PlusIcon size={13} /> {addingEdge ? "Cancel" : "Add dependency"}
            </button>
          )}
        </div>
      </div>

      {addingEdge && canManage && (
        <form className="topo-add-form" onSubmit={handleAddEdge}>
          <div className="topo-field">
            <label>Source resource</label>
            <select value={form.source} onChange={e => setForm(f => ({ ...f, source: e.target.value }))} required>
              <option value="" disabled>Select a resource…</option>
              {layout.nodes.map(n => <option key={n.resource_id} value={n.resource_id}>{nodeLabel(n)}</option>)}
            </select>
          </div>
          <span className="topo-add-arrow">depends on / routes to →</span>
          <div className="topo-field">
            <label>Target resource</label>
            <select value={form.target} onChange={e => setForm(f => ({ ...f, target: e.target.value }))} required>
              <option value="" disabled>Select a resource…</option>
              {layout.nodes.map(n => <option key={n.resource_id} value={n.resource_id}>{nodeLabel(n)}</option>)}
            </select>
          </div>
          <button type="submit" className="topo-btn-add" disabled={saving || !form.source || !form.target}>
            {saving ? "Saving…" : "Save"}
          </button>
        </form>
      )}

      {gaps.length > 0 && (
        <div className="topo-gap-banner">
          <div className="topo-gap-title"><AlertTriangleIcon size={14} /> {gaps.length} edge{gaps.length > 1 ? "s" : ""} point at a resource not in this account's inventory</div>
          <p>The cloud provider reports this relationship but the resource never showed up in discovery — the same failure class as the 2026-08-26 RCA. Shown below as a dashed node; worth checking discovery logs for it.</p>
        </div>
      )}

      {Object.keys(layout.positioned).length === 0 ? (
        <div className="topo-empty">No relationships tracked for this account yet{canManage ? " — add a manual dependency, or wait for the next auto-sync." : " yet."}</div>
      ) : (
        <div className="topo-graph-wrap">
          <div className="topo-tier-labels" style={{ width: layout.width }}>
            {TIERS.map((t, i) => layout.columns[i].length > 0 && (
              <span key={t.key} className="topo-tier-label" style={{ left: i * (NODE_W + COL_GAP) + PAD }}>{t.label}</span>
            ))}
          </div>
          <div className="topo-graph" style={{ width: layout.width, height: layout.height }}>
            <svg width={layout.width} height={layout.height} className="topo-svg">
              <defs>
                <marker id="arrow-auto" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
                  <path d="M0,0 L6,3 L0,6 Z" fill="var(--accent)" />
                </marker>
                <marker id="arrow-manual" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
                  <path d="M0,0 L6,3 L0,6 Z" fill="var(--accent-purple)" />
                </marker>
              </defs>
              {layout.edges.map(e => {
                const s = layout.positioned[e.source_resource_id];
                const t = layout.positioned[e.target_resource_id];
                if (!s || !t) return null;
                const forward = s.col <= t.col;
                const sx = forward ? s.x + NODE_W : s.x, sy = s.y + NODE_H / 2;
                const tx = forward ? t.x : t.x + NODE_W, ty = t.y + NODE_H / 2;
                const midx = (sx + tx) / 2;
                const active = isEdgeActive(e);
                const isManual = e.source === "manual";
                return (
                  <path
                    key={e.id}
                    d={`M${sx},${sy} C${midx},${sy} ${midx},${ty} ${tx},${ty}`}
                    fill="none"
                    stroke={isManual ? "var(--accent-purple)" : "var(--accent)"}
                    strokeWidth={active ? 2.5 : 1.5}
                    strokeDasharray={isManual ? "5 4" : undefined}
                    opacity={activeId ? (active ? 1 : 0.15) : 0.7}
                    markerEnd={`url(#${isManual ? "arrow-manual" : "arrow-auto"})`}
                    style={{ transition: "opacity .15s, stroke-width .15s" }}
                  />
                );
              })}
            </svg>
            {Object.values(layout.positioned).map(n => {
              const route = detailRoute(n, id);
              return (
                <NodeCard
                  key={n.resource_id}
                  node={n}
                  provider={provider}
                  active={activeId === n.resource_id}
                  pinned={pinnedId === n.resource_id}
                  dimmed={activeId && !isNodeActive(n.resource_id)}
                  onHover={() => !pinnedId && setHoveredId(n.resource_id)}
                  onLeave={() => setHoveredId(null)}
                  onSelect={() => setPinnedId(p => p === n.resource_id ? null : n.resource_id)}
                  onOpen={route ? () => navigate(route) : null}
                />
              );
            })}
          </div>
          <div className="topo-legend">
            <span><i className="topo-legend-line topo-legend-auto" /> Auto-detected</span>
            <span><i className="topo-legend-line topo-legend-manual" /> Manually declared</span>
            <span className="topo-legend-hint">Click a resource to trace its connections while scrolling</span>
            {pinnedId && (
              <button className="topo-legend-clear" onClick={() => setPinnedId(null)}>
                <XIcon size={11} /> Clear selection
              </button>
            )}
          </div>
        </div>
      )}

      {layout.others.length > 0 && (
        <div className="topo-others">
          <button className="topo-others-toggle" onClick={() => setShowOthers(v => !v)}>
            {showOthers ? "▾" : "▸"} {layout.others.length} other resource{layout.others.length === 1 ? "" : "s"} in this account not part of any tracked relationship
          </button>
          {showOthers && (
            <div className="topo-others-grid">
              {layout.others.map(n => {
                const route = detailRoute(n, id);
                return (
                  <div
                    key={n.resource_id}
                    className={`topo-chip ${route ? "topo-chip-clickable" : ""}`}
                    title={n.resource_id}
                    onClick={route ? () => navigate(route) : undefined}
                  >
                    <span className="topo-chip-icon">
                      <CloudServiceIcon provider={provider} service={n.resource_type} size={15} />
                    </span>
                    {n.name || n.resource_id}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {manualEdges.length > 0 && (
        <div className="topo-manual-list">
          <div className="topo-manual-title">Manual dependencies</div>
          {manualEdges.map(e => (
            <div key={e.id} className="topo-manual-row">
              <span className="mono">{e.source_resource_id} → {e.target_resource_id}</span>
              {canManage && (
                <button className="topo-btn-delete" onClick={async () => { await deleteManualEdge(id, e.id); load(); }} title="Delete this manual edge">
                  <TrashIcon size={13} />
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
