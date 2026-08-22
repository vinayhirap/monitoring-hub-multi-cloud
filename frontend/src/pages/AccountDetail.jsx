// monitoring-hub/frontend/src/pages/AccountDetail.jsx
import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { getLiveEC2, getLiveEC2Metrics } from "../api/api";
import "./AccountDetail.css";

async function openAccountConsole(accountId, service, resourceId) {
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

const STATE_COLOR = {
  running: "green",
  stopped: "muted",
  pending: "yellow",
  terminated: "red",
};

export default function AccountDetail() {
  const { id }    = useParams();
  const navigate  = useNavigate();
  const [instances, setInstances] = useState([]);
  const [loading,   setLoading]   = useState(true);
  const [selected,  setSelected]  = useState(null);
  const [metrics,   setMetrics]   = useState(null);
  const [mLoading,  setMLoading]  = useState(false);
  const [search,    setSearch]    = useState("");
  const [stateFilter, setStateFilter] = useState("all");
  const [sortKey,   setSortKey]   = useState("name");
  const [account,   setAccount]   = useState({ name: "AuroGov", id: "924922671984", region: "ap-south-2" });

  useEffect(() => {
    loadInstances();
    const t = setInterval(loadInstances, 30000);
    return () => clearInterval(t);
  }, [id]);

  async function loadInstances() {
    try {
      const data = await getLiveEC2(id);
      setInstances(Array.isArray(data) ? data : []);
    } catch (e) {
      console.error("EC2 load error:", e);
    } finally {
      setLoading(false);
    }
  }

  async function selectInstance(inst) {
    setSelected(inst);
    setMetrics(null);
    setMLoading(true);
    try {
      const data = await getLiveEC2Metrics(inst.instance_id, inst.region);
      setMetrics(data);
    } catch (e) {
      console.error("Metrics load error:", e);
    } finally {
      setMLoading(false);
    }
  }

  const visible = instances
    .filter(i => {
      if (stateFilter !== "all" && i.state !== stateFilter) return false;
      if (search) {
        const q = search.toLowerCase();
        return (
          i.instance_id.toLowerCase().includes(q) ||
          (i.name || "").toLowerCase().includes(q) ||
          i.instance_type.toLowerCase().includes(q)
        );
      }
      return true;
    })
    .sort((a, b) => {
      if (sortKey === "cpu")   return b.cpu_utilization - a.cpu_utilization;
      if (sortKey === "type")  return a.instance_type.localeCompare(b.instance_type);
      if (sortKey === "state") return a.state.localeCompare(b.state);
      return (a.name || "").localeCompare(b.name || "");
    });

  const running = instances.filter(i => i.state === "running").length;
  const stopped = instances.filter(i => i.state === "stopped").length;

  return (
    <div className="detail-page">
      {/* Breadcrumb */}
      <div className="breadcrumb">
        <span className="bc-link" onClick={() => navigate("/overview")}>ALL ACCOUNTS</span>
        <span className="bc-sep">›</span>
        <span className="bc-link" onClick={() => navigate("/overview")}>{account.name}</span>
        <span className="bc-sep">›</span>
        <span className="bc-current">EC2</span>
      </div>

      {/* Header */}
      <div className="detail-header">
        <div>
          <h1>EC2 — <span className="hl">{instances.length} instances</span></h1>
          <div className="detail-meta">
            <span className="meta-tag">{account.region}</span>
            <span className="meta-tag">PROD</span>
            <span className="meta-sep">·</span>
            <span className="meta-running">● {running} running</span>
            <span className="meta-sep">·</span>
            <span className="meta-stopped">◯ {stopped} stopped</span>
          </div>
        </div>
        <div className="detail-header-right">
          <button className="btn-back" onClick={() => navigate("/overview")}>← Back to Account</button>
          {/* Console link now backend-generated — federated, correct account */}
          <button className="btn-aws" onClick={() => openAccountConsole(id, "ec2")}>
            ☁ AWS Console ↗
          </button>
        </div>
      </div>

      <div className="detail-layout">
        {/* ── Instance Table ── */}
        <div className={`instance-panel ${selected ? "with-detail" : ""}`}>
          {/* Toolbar */}
          <div className="inst-toolbar">
            <input
              className="inst-search"
              placeholder="Search instance ID, name, type…"
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
            <div className="state-filters">
              {["all","running","stopped"].map(s => (
                <button
                  key={s}
                  className={`sf-btn ${stateFilter === s ? "sf-active" : ""}`}
                  onClick={() => setStateFilter(s)}
                >
                  {s === "all" ? "All" : s.charAt(0).toUpperCase() + s.slice(1)}
                  <span className="sf-count">
                    {s === "all" ? instances.length :
                     instances.filter(i => i.state === s).length}
                  </span>
                </button>
              ))}
            </div>
            <select
              className="sort-select"
              value={sortKey}
              onChange={e => setSortKey(e.target.value)}
            >
              <option value="name">Sort: Name</option>
              <option value="cpu">Sort: CPU</option>
              <option value="type">Sort: Type</option>
              <option value="state">Sort: State</option>
            </select>
          </div>

          {/* Table */}
          <div className="inst-table-wrap">
            <table className="inst-table">
              <thead>
                <tr>
                  <th>NAME / ID</th>
                  <th>TYPE</th>
                  <th>STATE</th>
                  <th>ZONE</th>
                  <th>CPU %</th>
                  <th>NET IN</th>
                  <th>NET OUT</th>
                  <th>UPTIME</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr><td colSpan={8} className="tbl-empty">Loading instances…</td></tr>
                ) : visible.length === 0 ? (
                  <tr><td colSpan={8} className="tbl-empty">No instances match filter.</td></tr>
                ) : visible.map(inst => (
                  <tr
                    key={inst.instance_id}
                    className={`inst-row ${selected?.instance_id === inst.instance_id ? "inst-selected" : ""}`}
                    onClick={() => selectInstance(inst)}
                  >
                    <td>
                      <div className="inst-name">{inst.name || inst.instance_id}</div>
                      <div className="inst-id mono">{inst.instance_id}</div>
                    </td>
                    <td className="mono">{inst.instance_type}</td>
                    <td>
                      <StateBadge state={inst.state} />
                    </td>
                    <td className="mono small">{inst.availability_zone}</td>
                    <td>
                      <CpuCell cpu={inst.cpu_utilization} state={inst.state} />
                    </td>
                    <td className="mono small">{inst.state === "running" ? `${inst.network_in_kb} KB/s` : "—"}</td>
                    <td className="mono small">{inst.state === "running" ? `${inst.network_out_kb} KB/s` : "—"}</td>
                    <td className="mono small">{inst.uptime_days}d</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* ── Instance Detail Panel ── */}
        {selected && (
          <div className="inst-detail">
            <div className="id-header">
              <div>
                <div className="id-name">{selected.name || selected.instance_id}</div>
                <div className="id-sub mono">{selected.instance_id} · {selected.instance_type} · {selected.availability_zone}</div>
              </div>
              <button className="id-close" onClick={() => setSelected(null)}>✕</button>
            </div>

            {/* Quick stats */}
            <div className="id-stats">
              <QuickStat label="State"      value={selected.state}           color={STATE_COLOR[selected.state]} />
              <QuickStat label="Private IP" value={selected.private_ip}      mono />
              <QuickStat label="CPU"        value={`${selected.cpu_utilization}%`} color={selected.cpu_utilization > 75 ? "red" : "green"} />
              <QuickStat label="Uptime"     value={`${selected.uptime_days}d`} />
            </div>

            {/* Tags */}
            {Object.keys(selected.tags || {}).length > 0 && (
              <div className="id-section">
                <div className="id-section-title">TAGS</div>
                <div className="id-tags">
                  {Object.entries(selected.tags).map(([k, v]) => (
                    <div key={k} className="id-tag">
                      <span className="id-tag-key">{k}</span>
                      <span className="id-tag-val">{v}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Metrics Charts */}
            <div className="id-section">
              <div className="id-section-title">CLOUDWATCH METRICS (6H)</div>
              {selected.state === "stopped" ? (
                <div className="id-no-metrics">
                  Instance is stopped — no live metrics available.
                  <br />
                  <small>Start the instance to see CloudWatch data.</small>
                </div>
              ) : mLoading ? (
                <div className="id-loading">Fetching CloudWatch data…</div>
              ) : metrics ? (
                <div className="charts-stack">
                  <MetricChart
                    title="CPU Utilization %"
                    data={metrics.cpu}
                    color="#2bb3ac"
                    unit="%"
                    threshold={75}
                  />
                  <MetricChart
                    title="Network In (kilobytes)"
                    data={metrics.network_in?.map(d => ({ ...d, v: d.v / 1024 }))}
                    color="#22c55e"
                    unit="KB"
                  />
                  <MetricChart
                    title="Network Out (kilobytes)"
                    data={metrics.network_out?.map(d => ({ ...d, v: d.v / 1024 }))}
                    color="#7c6ee0"
                    unit="KB"
                  />
                  <MetricChart
                    title="Disk Read (bytes)"
                    data={metrics.disk_read}
                    color="#fbbf24"
                    unit="B"
                  />
                  <MetricChart
                    title="Disk Write (bytes)"
                    data={metrics.disk_write}
                    color="#fb7185"
                    unit="B"
                  />
                </div>
              ) : (
                <div className="id-no-metrics">No metric data returned.</div>
              )}
            </div>

            {/* Console link now backend-generated — federated, correct account */}
            <button className="btn-open-aws" onClick={() => openAccountConsole(id, "ec2", selected.instance_id)}>
              ☁ Open in AWS ↗
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function StateBadge({ state }) {
  const map = {
    running:    { cls: "sb-green",  label: "running" },
    stopped:    { cls: "sb-muted",  label: "stopped" },
    pending:    { cls: "sb-yellow", label: "pending" },
    terminated: { cls: "sb-red",    label: "terminated" },
  };
  const { cls, label } = map[state] || map.stopped;
  return <span className={`state-badge ${cls}`}>{label}</span>;
}

function CpuCell({ cpu, state }) {
  if (state !== "running") return <span className="mono small muted">—</span>;
  const color = cpu > 75 ? "#ef4444" : cpu > 50 ? "#f59e0b" : "#22c55e";
  const w = Math.max(2, cpu);
  return (
    <div className="cpu-cell">
      <div className="cpu-bar-bg">
        <div className="cpu-bar-fill" style={{ width: `${w}%`, background: color }} />
      </div>
      <span className="cpu-label mono">{cpu.toFixed(1)}%</span>
    </div>
  );
}

function QuickStat({ label, value, color, mono }) {
  return (
    <div className="qs-item">
      <div className="qs-label">{label}</div>
      <div className={`qs-value ${color ? `c-${color}` : ""} ${mono ? "mono" : ""}`}>{value}</div>
    </div>
  );
}

function MetricChart({ title, data, color, unit, threshold }) {
  if (!data || data.length === 0) {
    return (
      <div className="chart-box">
        <div className="chart-title">{title}</div>
        <div className="chart-empty">No data points in last 6h</div>
      </div>
    );
  }

  const latest = data[data.length - 1]?.v ?? 0;
  const formatted = data.map(d => ({
    t: new Date(d.t).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false }),
    v: d.v,
  }));

  return (
    <div className="chart-box">
      <div className="chart-header">
        <span className="chart-title">{title}</span>
        <span className="chart-latest" style={{ color }}>{latest.toFixed(1)}{unit}</span>
      </div>
      <ResponsiveContainer width="100%" height={100}>
        <LineChart data={formatted} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
          <CartesianGrid stroke="rgba(99,130,190,0.08)" strokeDasharray="3 3" />
          <XAxis dataKey="t" tick={{ fontSize: 9, fill: "#3d5070" }} tickLine={false} axisLine={false} interval="preserveStartEnd" />
          <YAxis tick={{ fontSize: 9, fill: "#3d5070" }} tickLine={false} axisLine={false} />
          <Tooltip
            contentStyle={{ background: "#0b1220", border: "1px solid rgba(99,130,190,0.2)", borderRadius: 6, fontSize: 11 }}
            labelStyle={{ color: "#7a90b8" }}
            itemStyle={{ color }}
          />
          {threshold && (
            <Line type="monotone" dataKey={() => threshold} stroke="#ef4444" strokeDasharray="4 4" dot={false} strokeWidth={1} />
          )}
          <Line type="monotone" dataKey="v" stroke={color} strokeWidth={2} dot={false} activeDot={{ r: 3, fill: color }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}