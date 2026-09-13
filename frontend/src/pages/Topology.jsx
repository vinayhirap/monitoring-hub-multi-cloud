// src/pages/Topology.jsx
//
// Roadmap phase 4/7 (2026-09-13). Deliberately built with plain SVG, no
// graph-layout library (react-flow etc.) -- this stack has zero graph
// dependencies today (see frontend/package.json), and pulling one in for
// a first-cut view is a build-size/maintenance cost this feature doesn't
// need yet. Layout is a static "known architecture layers" heuristic
// (LB -> compute -> data), not a real force-directed/DAG layout --
// good enough to see routing at a glance; revisit with a real layout
// library only if the account's real topology turns out to need one
// (many-to-many edges across more than ~3 layers).
import { useState, useEffect, useCallback } from "react";
import { useParams } from "react-router-dom";
import { getTopology, addManualEdge, deleteManualEdge } from "../api/api";
import { AlertTriangleIcon, TrashIcon } from "../components/icons";

// Column order for the static layered layout. Anything not listed here
// (ecs, lambda, etc.) lands in the trailing "other" column rather than
// being dropped.
const LAYER_ORDER = ["elb", "ec2", "rds"];
const NODE_W = 190, NODE_H = 46, COL_GAP = 260, ROW_GAP = 64;

function layoutNodes(nodes) {
  const columns = {};
  for (const n of nodes) {
    const layer = LAYER_ORDER.includes(n.resource_type) ? n.resource_type : "other";
    (columns[layer] ??= []).push(n);
  }
  const colOrder = [...LAYER_ORDER, "other"].filter((c) => columns[c]);
  const positioned = {};
  colOrder.forEach((col, ci) => {
    columns[col].forEach((n, ri) => {
      positioned[n.resource_id] = {
        ...n,
        x: ci * COL_GAP + 20,
        y: ri * ROW_GAP + 20,
        col,
      };
    });
  });
  return positioned;
}

export default function Topology() {
  const { id } = useParams();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [addingEdge, setAddingEdge] = useState(false);
  const [form, setForm] = useState({ source: "", target: "" });

  const load = useCallback(() => {
    getTopology(id).then(setData).catch((e) => setError(e.message));
  }, [id]);

  useEffect(() => { load(); }, [load]);

  if (error) return <div className="p-6 text-red-600">Failed to load topology: {error}</div>;
  if (!data) return <div className="p-6 text-gray-400">Loading topology…</div>;

  const positioned = layoutNodes(data.nodes);
  const rowCounts = {};
  Object.values(positioned).forEach((n) => { rowCounts[n.col] = (rowCounts[n.col] || 0) + 1; });
  const width = (LAYER_ORDER.length + 1) * COL_GAP + 200;
  const height = Math.max(200, (Math.max(0, ...Object.values(rowCounts)) + 1) * ROW_GAP + 20);

  const gaps = data.edges.filter((e) => e.source_missing || e.target_missing);
  const manualEdges = data.edges.filter((e) => e.source === "manual");

  const handleAddEdge = async (e) => {
    e.preventDefault();
    if (!form.source || !form.target) return;
    try {
      await addManualEdge(id, form.source, form.target);
      setForm({ source: "", target: "" });
      setAddingEdge(false);
      load();
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-semibold">Resource Topology</h1>
        <button
          className="px-3 py-1.5 text-sm rounded bg-blue-600 text-white hover:bg-blue-700"
          onClick={() => setAddingEdge((v) => !v)}
        >
          {addingEdge ? "Cancel" : "Add dependency"}
        </button>
      </div>

      {addingEdge && (
        <form onSubmit={handleAddEdge} className="mb-4 flex gap-2 items-center bg-gray-50 border rounded p-3 text-sm">
          <span>Source resource ID</span>
          <input className="border rounded px-2 py-1" value={form.source}
                 onChange={(e) => setForm((f) => ({ ...f, source: e.target.value }))} />
          <span>depends on / routes to</span>
          <input className="border rounded px-2 py-1" value={form.target}
                 onChange={(e) => setForm((f) => ({ ...f, target: e.target.value }))} />
          <button type="submit" className="px-3 py-1 rounded bg-blue-600 text-white">Save</button>
        </form>
      )}

      {gaps.length > 0 && (
        <div className="mb-4 border border-amber-300 bg-amber-50 rounded p-3 text-sm text-amber-800">
          <div className="flex items-center gap-2 font-medium mb-1">
            <AlertTriangleIcon className="w-4 h-4" />
            {gaps.length} edge{gaps.length > 1 ? "s" : ""} point at a resource not in this account's inventory
          </div>
          <div className="text-xs opacity-80">
            AWS reports this relationship but the resource never showed up in discovery —
            same failure class as the 2026-08-26 RCA. Worth checking discovery logs for it.
          </div>
          <ul className="mt-2 space-y-0.5 font-mono text-xs">
            {gaps.map((g) => (
              <li key={g.id}>
                {g.source_resource_id} → {g.target_resource_id}
                {g.source_missing && " (source missing)"}
                {g.target_missing && " (target missing)"}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="border rounded bg-white overflow-auto">
        <svg width={width} height={height}>
          {data.edges.map((e) => {
            const s = positioned[e.source_resource_id];
            const t = positioned[e.target_resource_id];
            if (!s || !t) return null; // gap edges are listed above, not drawn
            return (
              <line
                key={e.id}
                x1={s.x + NODE_W} y1={s.y + NODE_H / 2}
                x2={t.x} y2={t.y + NODE_H / 2}
                stroke={e.source === "manual" ? "#9333ea" : "#94a3b8"}
                strokeDasharray={e.source === "manual" ? "4 3" : undefined}
                strokeWidth={1.5}
                markerEnd="url(#arrow)"
              />
            );
          })}
          <defs>
            <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
              <path d="M0,0 L6,3 L0,6 Z" fill="#94a3b8" />
            </marker>
          </defs>
          {Object.values(positioned).map((n) => (
            <g key={n.resource_id} transform={`translate(${n.x},${n.y})`}>
              <rect width={NODE_W} height={NODE_H} rx={6} fill="#f8fafc" stroke="#cbd5e1" />
              <text x={10} y={18} fontSize={11} fill="#64748b">{n.resource_type}</text>
              <text x={10} y={34} fontSize={12} fill="#0f172a" fontWeight={500}>
                {(n.name || n.resource_id).slice(0, 22)}
              </text>
            </g>
          ))}
        </svg>
      </div>

      {manualEdges.length > 0 && (
        <div className="mt-4 text-sm">
          <div className="font-medium mb-1">Manual dependencies</div>
          <ul className="space-y-1">
            {manualEdges.map((e) => (
              <li key={e.id} className="flex items-center gap-2 font-mono text-xs">
                {e.source_resource_id} → {e.target_resource_id}
                <button onClick={async () => { await deleteManualEdge(id, e.id); load(); }}>
                  <TrashIcon className="w-3.5 h-3.5 text-gray-400 hover:text-red-500" />
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
