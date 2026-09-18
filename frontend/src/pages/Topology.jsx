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
//   - "Add dependency" / delete controls only render for a user with
//     topology.manage (see db/migrations/024_topology_manage_permission.sql)
//     -- previously any viewer could mutate manually-declared edges
//     because the write endpoints rode along on the same permission as
//     the read-only view.
//
// 2026-09-17, first pass ("every resource in the account, aligned for
// GCP/Azure"): confirmed discovery + icons were already complete for
// all three providers (see app/collector/discovery/*, app/providers/
// {azure,gcp}/discovery.py, components/cloud-icons.jsx) and fixed the
// three real gaps that were left: incomplete tier categorization,
// AWS-only detail-page routing, and the others-list defaulting closed.
//
// 2026-09-17, second pass -- REDESIGN ("think like a cloud architect
// and a UI/UX pro; NAT/VPC/WAF are implicitly attached to almost
// everything, not just what has a literal discovered edge; build real
// layers"): the flat 4-tier model (Entry/Compute/Data/Security) and
// the flat alphabetical "others" chip wall both under-served what this
// data actually is -- an account's infrastructure has real
// architectural layers, and a few resource types (NAT, WAF, VPC-level
// networking, KMS) are foundational to *everything downstream* in a
// way no discovered edge will ever capture (AWS's describe/tagging
// APIs report "this ALB targets that EC2 instance", never "every
// private-subnet instance's egress passes through this NAT gateway" --
// that's implied by VPC route tables, not a discrete API relationship
// this app polls). Drawing literal edges from one NAT gateway to every
// compute node it plausibly serves would be a correct-ish but
// unreadable hairball -- the real cloud-architecture-diagram answer
// (AWS's own reference architectures, and every serious diagramming
// tool) is a BOUNDARY, not N edges: foundational network/security
// infrastructure is drawn as a zone the rest of the diagram sits
// inside, not as a node with a thousand arrows leaving it. That's what
// PERIMETER_LAYER_KEYS + the dashed boundary box below implement.
//
// What changed:
//   1. TIERS -> LAYERS: 6 real architectural layers instead of 4 loose
//      buckets -- Network & Perimeter, Security & Identity, Compute,
//      Data & Storage, Messaging & Eventing, Governance &
//      Observability -- each with its own icon and accent color, and
//      a complete type list per provider (see the audit comment this
//      replaced for the exact discovery-vs-catalog cross-check; the
//      type assignments are unchanged in substance, just regrouped
//      from 4 buckets into 6 more precise ones. New homes that didn't
//      exist before: Messaging split out from Data, Governance split
//      out from Data, for backup/logs/data_factory).
//   2. Network & Security are drawn inside a shared dashed "Perimeter"
//      boundary box spanning their combined columns -- the visual
//      answer to "NAT/WAF/etc. are attached to everything": they
//      frame the diagram, they don't get individually wired to every
//      node behind them. See PerimeterBoundary below.
//   3. Edges now visually flow: auto-detected edges animate a moving
//      dash pattern (CSS, see topo-edge-auto in Topology.css) so a
//      live/current relationship reads as "live" at a glance, not just
//      via a legend color key. Manually-declared edges stay static
//      dashed -- deliberately NOT animated, since "flowing" implies an
//      observed, current relationship and a manual edge is a stated
//      belief, not something this app has re-confirmed since it was
//      typed in.
//   4. Every node card gets a thin left accent stripe in its layer's
//      color, and the others-list is now grouped under the same 6
//      layer headers (icon + accent + count) instead of one flat
//      alphabetical wall -- color and grouping both do real
//      categorization work now, on the graph AND on the 60+ resources
//      that never show up in it.
//
// Deliberately still no graph-layout library (react-flow etc.) --
// frontend/package.json has zero graph dependencies today, and a fixed
// layer-column layout plus CSS-only edge animation covers everything
// this redesign needed. Revisit with a real layout engine only if a
// future ask needs true force-directed placement or draggable nodes.
import { useState, useEffect, useCallback, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { getTopology, addManualEdge, deleteManualEdge } from "../api/api";
import { useAuth } from "../auth/AuthContext";
import { CloudServiceIcon } from "../components/cloud-icons";
import {
  AlertTriangleIcon, TrashIcon, PlusIcon, InfoIcon, ExternalLinkIcon, XIcon,
  GlobeIcon, ShieldIcon, ServerIcon, DatabaseIcon, MailIcon, ClipboardIcon,
} from "../components/icons";
import "./Topology.css";

// Six real architectural layers, left-to-right in request-flow order:
// a request crosses the network perimeter, is subject to security
// controls, reaches compute, which reads/writes data and publishes/
// consumes async messages -- governance doesn't sit IN that flow, it
// watches all of it, hence its own distinct treatment (see
// GOVERNANCE_LAYER_KEY below and its render-time styling).
//
// Every type discovery actually writes for AWS
// (app/collector/discovery/runner.py + extended.py), Azure
// (app/providers/azure/discovery.py) and GCP
// (app/providers/gcp/discovery.py) is listed explicitly -- cross-
// checked against all three files plus each provider's
// metric_catalog_data.py, so nothing should be silently falling
// through to TIER_FALLBACK. A brand-new service type added later
// without a matching entry here still renders (icon + fallback layer),
// it just won't be perfectly placed until this list is updated too.
const LAYERS = [
  {
    key: "network", label: "Network & Perimeter", icon: GlobeIcon, accent: "#3b82f6",
    blurb: "Edge, DNS, gateways & traffic filtering -- every request crosses this first.",
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
    // kms/certificatemanager/cognito (aws) and key_vault (azure) have
    // no discovered edges to anything -- there's no Describe API that
    // reports "this KMS key encrypts that EBS volume" -- but they
    // gate what the layers to the right are allowed to do, same
    // conceptual role as the network perimeter. Grouped with it inside
    // one shared boundary box below for exactly that reason. No GCP
    // catalog key maps here today (GCP's catalog has no dedicated
    // KMS/Secret Manager entry), which is reality, not an omission.
    key: "security", label: "Security & Identity", icon: ShieldIcon, accent: "#ef4444",
    blurb: "Keys, certificates & identity -- governs what every layer to the right can do.",
    types: ["kms", "certificatemanager", "cognito", "key_vault"],
  },
  {
    key: "compute", label: "Compute", icon: ServerIcon, accent: "#a855f7",
    blurb: "Where your code actually runs.",
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
    key: "data", label: "Data & Storage", icon: DatabaseIcon, accent: "#10b981",
    blurb: "Databases, object storage & block storage -- state that outlives a single request.",
    types: [
      // aws
      "ebs", "rds", "s3", "dynamodb", "elasticache", "efs", "redshift",
      "opensearch", "documentdb", "neptune", "memorydb", "dax", "dms",
      // azure
      "storage_account", "sql_database", "cosmosdb_account", "redis_cache", "managed_disk",
      // gcp
      "gce_persistent_disk", "gcs_bucket", "cloudsql_instance", "firestore_database",
      "bigquery_project", "spanner_instance", "redis_instance",
    ],
  },
  {
    key: "messaging", label: "Messaging & Eventing", icon: MailIcon, accent: "#f59e0b",
    blurb: "Queues, topics & event buses -- how compute talks to compute asynchronously.",
    types: [
      // aws
      "sqs", "sns", "kinesis", "firehose", "msk", "events",
      // azure
      "service_bus_namespace", "eventhub_namespace",
      // gcp
      "pubsub_topic", "pubsub_subscription",
    ],
  },
  {
    // Deliberately last, and visually distinct (see GOVERNANCE_LAYER_KEY
    // below) -- backup/logs/data_factory observe or operate on every
    // other layer rather than participating in a request's own path
    // through the system, so placing them inline at the end of the
    // same left-to-right flow would misrepresent them as a downstream
    // processing step instead of an out-of-band concern.
    key: "governance", label: "Governance & Observability", icon: ClipboardIcon, accent: "#64748b",
    blurb: "Backup, logs & data orchestration -- operates across every layer, not inside the request path.",
    types: [
      // aws
      "backup", "logs",
      // azure
      "data_factory",
      // gcp -- none in the catalog today
    ],
  },
];
const TIER_FALLBACK = "compute";
// The network + security layers get drawn inside one shared dashed
// "perimeter" boundary box (see PerimeterBoundary) instead of literal
// edges to everything they implicitly affect -- see this file's
// 2026-09-17 redesign note above for why. Indices, not keys, since
// PerimeterBoundary needs to know which *columns* (by position) to
// span.
const PERIMETER_LAYER_INDICES = [0, 1]; // network, security
const GOVERNANCE_LAYER_KEY = "governance";
// Excluded entirely -- see file header. Not just "no branded icon", not
// drawn at all: filtered out of nodes, edges, and the others-list below.
const HIDDEN_RESOURCE_TYPES = new Set(["eni"]);

const NODE_W = 208, NODE_H = 60, COL_GAP = 130, ROW_GAP = 20, PAD = 30;

function layerIndexOf(resourceType) {
  const i = LAYERS.findIndex(t => t.types.includes(resourceType));
  return i === -1 ? LAYERS.findIndex(t => t.key === TIER_FALLBACK) : i;
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
  // 2026-09-17 redesign: left accent stripe in the node's layer color
  // -- ghosts stay neutral (grey), since "unresolved edge endpoint" is
  // its own category, not a real layer membership.
  const accent = isGhost ? "var(--text-muted)" : LAYERS[layerIndexOf(node.resource_type)].accent;
  return (
    <div
      className={`topo-node topo-node-selectable ${active ? "topo-node-hovered" : ""} ${dimmed ? "topo-node-dimmed" : ""} ${isGhost ? "topo-node-ghost" : ""} ${pinned ? "topo-node-pinned" : ""}`}
      style={{ left: node.x, top: node.y, width: NODE_W, height: NODE_H, "--node-accent": accent }}
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

// 2026-09-17 redesign: the visual answer to "NAT/WAF/security are
// implicitly attached to almost everything" -- a shared dashed
// boundary drawn behind the network + security columns (see
// PERIMETER_LAYER_INDICES), spanning the full graph height, instead of
// literal edges fanning out from each one. Real cloud reference
// architectures draw a VPC/security boundary as a box the rest of the
// diagram sits inside, not as a node wired to everything it affects --
// this is that convention, not a novel one. Renders nothing if neither
// layer has any nodes in the graph (usedCols may exclude both).
function PerimeterBoundary({ columns, height }) {
  const activeIdx = PERIMETER_LAYER_INDICES.filter(i => columns[i]?.length > 0);
  if (activeIdx.length === 0) return null;
  const first = Math.min(...activeIdx), last = Math.max(...activeIdx);
  const left = first * (NODE_W + COL_GAP) + PAD - 14;
  const width = (last - first) * (NODE_W + COL_GAP) + NODE_W + 28;
  return (
    <div
      className="topo-perimeter"
      style={{ left, width, height: height + 28, top: -14 }}
      title="Network & security infrastructure -- foundational to every resource inside it, even without a discovered edge to each one"
    >
      <span className="topo-perimeter-label"><ShieldIcon size={11} /> Perimeter</span>
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
    // 2026-09-17: group the unconnected ("others") resources by the
    // same 6 layers the graph itself uses, sorted for scannability --
    // this is what turns the old flat alphabetical chip wall into
    // something a person can actually navigate by category. Empty
    // layers are kept (as empty arrays) so the render side can just
    // zip this against LAYERS by index without a second lookup.
    const othersByLayer = LAYERS.map(() => []);
    others.forEach(n => othersByLayer[layerIndexOf(n.resource_type)].push(n));
    othersByLayer.forEach(group => group.sort((a, b) => (a.name || a.resource_id).localeCompare(b.name || b.resource_id)));

    const columns = LAYERS.map(() => []);
    allConnected.forEach(n => columns[layerIndexOf(n.resource_type)].push(n));

    const positioned = {};
    let maxRows = 0;
    columns.forEach((col, ci) => {
      maxRows = Math.max(maxRows, col.length);
      col.forEach((n, ri) => {
        positioned[n.resource_id] = { ...n, x: ci * (NODE_W + COL_GAP) + PAD, y: ri * (NODE_H + ROW_GAP) + PAD, col: ci };
      });
    });

    const usedCols = LAYERS.filter((_, i) => columns[i].length > 0).length || 1;
    const width  = usedCols * (NODE_W + COL_GAP) - COL_GAP + PAD * 2;
    const height = Math.max(1, maxRows) * (NODE_H + ROW_GAP) - ROW_GAP + PAD * 2;

    return { nodes, edges, positioned, others, othersByLayer, width, height, columns };
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
            {LAYERS.map((t, i) => layout.columns[i].length > 0 && (
              <span
                key={t.key}
                className="topo-tier-label"
                style={{ left: i * (NODE_W + COL_GAP) + PAD, "--layer-accent": t.accent }}
                title={t.blurb}
              >
                <t.icon size={12} /> {t.label}
              </span>
            ))}
          </div>
          <div className="topo-graph" style={{ width: layout.width, height: layout.height }}>
            <PerimeterBoundary columns={layout.columns} height={layout.height} />
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
                    // 2026-09-17: auto-detected edges get the flowing-
                    // dash animation (topo-edge-auto, see Topology.css)
                    // -- a currently-observed relationship reads as
                    // "live" at a glance. Manual edges deliberately do
                    // NOT flow: a manually-declared dependency is a
                    // stated belief this app has never independently
                    // re-confirmed, and animating it the same way would
                    // claim a freshness it doesn't have.
                    className={isManual ? "topo-edge-manual" : "topo-edge-auto"}
                    d={`M${sx},${sy} C${midx},${sy} ${midx},${ty} ${tx},${ty}`}
                    fill="none"
                    stroke={isManual ? "var(--accent-purple)" : "var(--accent)"}
                    strokeWidth={active ? 2.5 : 1.5}
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
            <span><i className="topo-legend-line topo-legend-auto" /> Auto-detected · live</span>
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
            // 2026-09-17 redesign: grouped by the same 6 architectural
            // layers the graph above uses, instead of one flat
            // alphabetical wall -- an account's "everything else" is
            // still an inventory worth being able to scan by category
            // (all the ACM certs together, all the log/backup/StackSet
            // governance noise together, etc.), not just a list.
            <div className="topo-others-layers">
              {LAYERS.map((layer, i) => {
                const items = layout.othersByLayer[i];
                if (!items.length) return null;
                const isGovernance = layer.key === GOVERNANCE_LAYER_KEY;
                return (
                  <div
                    key={layer.key}
                    className={`topo-layer-group ${isGovernance ? "topo-layer-group-governance" : ""}`}
                    style={{ "--layer-accent": layer.accent }}
                  >
                    <div className="topo-layer-group-header" title={layer.blurb}>
                      <layer.icon size={13} />
                      <span>{layer.label}</span>
                      <span className="topo-layer-count">{items.length}</span>
                    </div>
                    <div className="topo-others-grid">
                      {items.map(n => {
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
