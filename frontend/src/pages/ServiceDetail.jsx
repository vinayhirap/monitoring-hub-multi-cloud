// monitoring-hub/frontend/src/pages/ServiceDetail.jsx
import { useEffect, useState, useCallback, useRef } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import {
  ServerIcon, SaveIcon, DatabaseIcon, BucketIcon, PackageIcon, ScaleIcon,
  ArrowLeftIcon, CloudIcon, ExternalLinkIcon, AlertTriangleIcon, RedDotIcon,
  LockIcon, CheckIcon, XCircleIcon, LinkIcon, GlobeIcon, LayersIcon,
  XIcon, TagIcon, BarChartIcon, ToolIcon, ZapIcon,
} from "../components/icons";

const BASE = "";

// The 7 AWS resource types this page can actually fetch live data for
// today (app/api/live_data.py only has AWS endpoints — no GCP/Azure
// resource-list endpoints exist yet). URL params arrive lowercase
// (matching resources.resource_type / metric_catalog.service, e.g. "ec2",
// "vm", "compute_instance") — normalize to the internal keys this file
// has always used.
const KNOWN_SERVICE_KEYS = { ec2: "EC2", ebs: "EBS", rds: "RDS", s3: "S3", ecs: "ECS", elb: "ELB", lambda: "Lambda" };

function normalizeService(rawParam) {
  return KNOWN_SERVICE_KEYS[(rawParam || "").toLowerCase()] || null;
}

const SERVICE_META = {
  EC2:    { icon: ServerIcon,   color: "#2bb3ac", label: "EC2 Instances" },
  EBS:    { icon: SaveIcon,     color: "#38bdf8", label: "EBS Volumes" },
  RDS:    { icon: DatabaseIcon, color: "#7c6ee0", label: "RDS Databases" },
  Lambda: { icon: "λ",          color: "#22c55e", label: "Lambda Functions" },
  S3:     { icon: BucketIcon,   color: "#fbbf24", label: "S3 Buckets" },
  ECS:    { icon: PackageIcon,  color: "#34d399", label: "ECS Services" },
  ELB:    { icon: ScaleIcon,    color: "#f472b6", label: "Load Balancers" },
};

const TIME_RANGES = [
  { label: "1H",  hours: 1 },
  { label: "3H",  hours: 3 },
  { label: "6H",  hours: 6 },
  { label: "1D",  hours: 24 },
  { label: "1W",  hours: 168 },
  { label: "1M",  hours: 720 },
  { label: "6M",  hours: 4320 },
  { label: "1Y",  hours: 8760 },
  { label: "ALL", hours: 17520 },
];

async function fetchService(accountId, service) {
  const paths = {
    EC2:    `/api/live/ec2/${accountId}`,
    EBS:    `/api/live/ebs/${accountId}`,
    RDS:    `/api/live/rds/${accountId}`,
    Lambda: `/api/live/lambda/${accountId}`,
    S3:     `/api/live/s3/${accountId}`,
    ECS:    `/api/live/ecs/${accountId}`,
    ELB:    `/api/live/elb/${accountId}`,
  };
  const path = paths[service];
  if (!path) { const e = new Error("404"); e.status = 404; throw e; }
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) { const e = new Error(String(res.status)); e.status = res.status; throw e; }
  return res.json();
}

async function fetchAccount(id) {
  const res = await fetch(`${BASE}/api/admin/accounts/${id}`);
  if (!res.ok) throw new Error(String(res.status));
  return res.json();
}

// FIX: accountId param added — ELB was using undefined `id` from outer scope
async function fetchMetrics(service, row, region, hours, accountId) {
  const h = `hours=${hours}`;
  const r = `region=${region}`;
  switch (service) {
    case "EC2":
      if (row.state !== "running") return null;
      return fetch(`${BASE}/api/live/metrics/ec2/${row.instance_id}?${r}&${h}`).then(res => res.json());
    case "EBS":
      if (row.state !== "in-use") return null;
      return fetch(`${BASE}/api/live/metrics/ebs/${row.volume_id}?${r}&${h}`).then(res => res.json());
    case "Lambda":
      return fetch(`${BASE}/api/live/metrics/lambda/${row.function_name}?${r}&${h}`).then(res => res.json());
    case "RDS":
      return fetch(`${BASE}/api/live/metrics/rds/${row.db_instance_id}?${r}&${h}`).then(res => res.json());
    case "S3":
      return fetch(`${BASE}/api/live/metrics/s3/${row.bucket_name || row.name}?${h}`).then(res => res.json());
    case "ELB":
      // FIX: was using `id` (undefined) — now correctly uses accountId param
      return fetch(
        `${BASE}/api/live/metrics/elb/${accountId}?lb_name=${encodeURIComponent(row.name)}&${r}&${h}`
      ).then(res => res.json());
    case "ECS":
      // row here is a service object with cluster_name attached
      return fetch(
        `${BASE}/api/live/metrics/ecs/${accountId}?cluster_name=${encodeURIComponent(row.cluster_name)}&service_name=${encodeURIComponent(row.service_name)}&${r}&${h}`
      ).then(res => res.json());
    default:
      return null;
  }
}

// Finds the row that matches a `?resource=` deep-link value coming from
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

export default function ServiceDetail() {
  const { id, service: rawService } = useParams();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const service  = normalizeService(rawService);       // "EC2" etc, or null if unsupported
  const meta     = service ? (SERVICE_META[service] || SERVICE_META.EC2) : {
    icon: CloudIcon, color: "#6382be", label: (rawService || "Service"),
  };

  const [account,    setAccount]    = useState(null);
  const [rows,       setRows]       = useState([]);
  const [loading,    setLoading]    = useState(true);
  const [error,      setError]      = useState(null);
  const [notImpl,    setNotImpl]    = useState(false);
  const [selected,   setSelected]   = useState(null);
  const [metrics,    setMetrics]    = useState(null);
  const [mLoading,   setMLoading]   = useState(false);
  const [search,     setSearch]     = useState("");
  const [filter,     setFilter]     = useState("all");
  const [sortKey,    setSortKey]    = useState("name");
  const [timeRange,  setTimeRange]  = useState(6);
  const [activeAlerts, setActiveAlerts] = useState([]);
  const notImplRef  = useRef(false);
  const selectedRef = useRef(null);
  const autoSelectedRef = useRef(null);

  useEffect(() => {
fetchAccount(id).then(setAccount).catch(err => {
      console.error(err);
      navigate("/overview");
    });
        fetch("/api/alerts")
      .then(r => r.ok ? r.json() : [])
      .then(a => setActiveAlerts((Array.isArray(a) ? a : []).filter(x => (x.status||"").toLowerCase() === "active")))
      .catch(() => {});
  }, [id]);

  const loadRows = useCallback(async () => {
    if (notImplRef.current) return;
    if (!service) {
      // Unsupported/unmapped service (GCP/Azure, or an AWS directory-tier
      // service without a live-data endpoint yet) -- don't even attempt a
      // fetch, go straight to the same graceful NotImplState AWS services
      // without endpoints already use.
      notImplRef.current = true;
      setNotImpl(true);
      setRows([]);
      setLoading(false);
      return;
    }
    setError(null);
    try {
      const data = await fetchService(id, service);
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      if (e.status === 404) {
        // Any unmapped/unimplemented service 404s the same way -- treat
        // it as "not configured yet" rather than a hard error.
        notImplRef.current = true;
        setNotImpl(true);
        setRows([]);
      } else {
        setError(e.message);
      }
    } finally {
      setLoading(false);
    }
  }, [id, service]);

  useEffect(() => {
    notImplRef.current = false;
    setNotImpl(false);
    setLoading(true);
    setRows([]);
    setError(null);
    loadRows();
    const t = setInterval(() => { if (!notImplRef.current) loadRows(); }, 15000);
    return () => clearInterval(t);
  }, [loadRows]);

  useEffect(() => {
    if (!selectedRef.current) return;
    const row    = selectedRef.current;
    const region = row.region || account?.default_region || "ap-south-2";
    setMetrics(null);
    setMLoading(true);
    fetchMetrics(service, row, region, timeRange, id)
      .then(data => setMetrics(data))
      .catch(console.error)
      .finally(() => setMLoading(false));
  }, [timeRange, service, account, id]);

  async function selectRow(row) {
    selectedRef.current = row;
    setSelected(row);
    setMetrics(null);
    setMLoading(true);
    const region = row.region || account?.default_region || "ap-south-2";
    try {
      const data = await fetchMetrics(service, row, region, timeRange, id);
      setMetrics(data);
      if (service === "EC2" && data?.cpu?.length > 0) {
        const latestCpu = data.cpu[data.cpu.length - 1].v;
        setRows(prev => prev.map(r =>
          r.instance_id === row.instance_id ? { ...r, cpu_utilization: latestCpu } : r
        ));
        const updated = { ...row, cpu_utilization: latestCpu };
        selectedRef.current = updated;
        setSelected(updated);
      }
    } catch (e) {
      console.error("Metrics fetch error:", e);
    } finally {
      setMLoading(false);
    }
  }

  // Deep-link support: if we arrived via Alerts' "Metrics" link
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

  const stateCounts = rows.reduce((acc, r) => {
    const s = (r.state || r.status || "unknown").toLowerCase();
    acc[s] = (acc[s] || 0) + 1;
    return acc;
  }, {});
  const filterStates = ["all", ...Object.keys(stateCounts)];

  const STATE_PRIORITY = { running: 0, active: 0, "in-use": 0, available: 1, stopped: 2, terminated: 3 };

  const visible = rows
    .filter(r => {
      if (search && !Object.values(r).join(" ").toLowerCase().includes(search.toLowerCase())) return false;
      if (filter !== "all") {
        const s = (r.state || r.status || "").toLowerCase();
        if (s !== filter) return false;
      }
      return true;
    })
    .sort((a, b) => {
      const sa = (a.state || a.status || "").toLowerCase();
      const sb = (b.state || b.status || "").toLowerCase();
      const pa = STATE_PRIORITY[sa] ?? 1;
      const pb = STATE_PRIORITY[sb] ?? 1;
      if (pa !== pb) return pa - pb;
      if (sortKey === "cpu")   return (b.cpu_utilization || 0) - (a.cpu_utilization || 0);
      if (sortKey === "size")  return (a.instance_type || a.size || "").localeCompare(b.instance_type || b.size || "");
      if (sortKey === "state") return sa.localeCompare(sb);
      const na = a.name || a.instance_id || a.function_name || a.bucket_name || a.dns_name || "";
      const nb = b.name || b.instance_id || b.function_name || b.bucket_name || b.dns_name || "";
      return na.localeCompare(nb);
    });

  const region = account?.default_region || "ap-south-2";

  return (
    <div className="detail-page">
      <div className="breadcrumb">
        <span className="bc-link" onClick={() => navigate("/overview")}>ALL ACCOUNTS</span>
        <span className="bc-sep">›</span>
        <span className="bc-link" onClick={() => navigate(`/accounts/${id}/services`)}>
          {account?.account_name ?? `Account ${id}`}
        </span>
        <span className="bc-sep">›</span>
        <span className="bc-current">{meta.label}</span>
      </div>

      <div className="detail-header">
        <div>
          <h1>
            <span style={{ marginRight: 8, display: "inline-flex", verticalAlign: "middle" }}><ServiceIcon icon={meta.icon} size={22} /></span>
            {meta.label} — <span className="hl">{notImpl ? "—" : `${rows.length} total`}</span>
          </h1>
          <div className="detail-meta">
            <span className="meta-tag">{region}</span>
            <span className="meta-tag">PROD</span>
            {service === "EC2" && !notImpl && (
              <>
                <span className="meta-sep">·</span>
                <span className="meta-running">● {rows.filter(r => r.state === "running").length} running</span>
                <span className="meta-sep">·</span>
                <span className="meta-stopped">◯ {rows.filter(r => r.state === "stopped").length} stopped</span>
              </>
            )}
          </div>
        </div>
        <div className="detail-header-right">
          <button className="btn-back" onClick={() => navigate(`/accounts/${id}/services`)}><ArrowLeftIcon size={13} /> Back</button>
          <button className="btn-aws" onClick={() => openAccountConsole(id, rawService, { region })}>
            <CloudIcon size={13} /> Open Console <ExternalLinkIcon size={12} />
          </button>
        </div>
      </div>

      {notImpl ? (
        <NotImplState service={meta.label} rawService={rawService} meta={meta} region={region} accountId={id} />
      ) : (
        <div className="detail-layout">
          <div className={`instance-panel ${selected ? "with-detail" : ""}`}>
            <div className="inst-toolbar">
              <input
                className="inst-search"
                placeholder={`Search ${service} resources…`}
                value={search}
                onChange={e => setSearch(e.target.value)}
              />
              <div className="state-filters">
                {filterStates.map(s => (
                  <button
                    key={s}
                    className={`sf-btn ${filter === s ? "sf-active" : ""}`}
                    onClick={() => setFilter(s)}
                  >
                    {s === "all" ? "All" : capitalize(s)}
                    <span className="sf-count">{s === "all" ? rows.length : (stateCounts[s] || 0)}</span>
                  </button>
                ))}
              </div>
              <select className="sort-select" value={sortKey} onChange={e => setSortKey(e.target.value)}>
                <option value="name">Sort: Name</option>
                {service === "EC2" && <option value="cpu">Sort: CPU</option>}
                <option value="size">Sort: Type / Size</option>
                <option value="state">Sort: State</option>
              </select>
            </div>
            <div className="inst-table-wrap">
              {error ? (
                <div style={{ padding: 16, color: "#ff6b8a", fontSize: 12, display: "flex", alignItems: "center", gap: 6 }}>
                  <AlertTriangleIcon size={13} /> Failed to load: {error}{" "}
                  <button onClick={loadRows} style={{ marginLeft: 8, background: "none", border: "1px solid rgba(239,68,68,0.3)", color: "#ff6b8a", borderRadius: 4, padding: "2px 8px", cursor: "pointer", fontSize: 11 }}>
                    Retry
                  </button>
                </div>
              ) : (
                <ServiceTable service={service} rows={visible} loading={loading} selected={selected} onSelect={selectRow} allRows={rows} activeAlerts={activeAlerts} />
              )}
            </div>
          </div>
          {selected && (
            <ServiceDetailPanel
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
            />
          )}
        </div>
      )}
    </div>
  );
}

function NotImplState({ service, rawService, meta, region, accountId }) {
  return (
    <div style={{ textAlign: "center", padding: "64px 32px", background: "rgba(13,22,39,0.5)", border: "1px solid rgba(99,130,190,0.1)", borderRadius: 12, marginTop: 8 }}>
      <div style={{ display: "flex", justifyContent: "center", marginBottom: 16 }}><ServiceIcon icon={meta.icon} size={48} /></div>
      <div style={{ fontSize: 16, fontWeight: 700, color: "#e2e8f0", marginBottom: 8 }}>{service} not configured</div>
      <div style={{ fontSize: 12, color: "rgba(99,130,190,0.65)", lineHeight: 1.7, marginBottom: 20 }}>
        This app doesn't have a resource list view for {service.toLowerCase?.() || service} yet.
        You can still open it directly in the cloud console.
      </div>
      <button className="btn-aws" onClick={() => openAccountConsole(accountId, rawService, { region })}>
        <CloudIcon size={13} /> Open in Console <ExternalLinkIcon size={12} />
      </button>
    </div>
  );
}

function ServiceTable({ service, rows, loading, selected, onSelect, allRows, activeAlerts = [] }) {
  if (loading) return <table className="inst-table"><tbody><tr><td colSpan={9} className="tbl-empty">Loading…</td></tr></tbody></table>;
  if (rows.length === 0) return <table className="inst-table"><tbody><tr><td colSpan={9} className="tbl-empty">No resources found.</td></tr></tbody></table>;
  switch (service) {
    case "EC2":    return <EC2Table    rows={rows} selected={selected} onSelect={onSelect} allRows={allRows} activeAlerts={activeAlerts} />;
    case "EBS":    return <EBSTable    rows={rows} selected={selected} onSelect={onSelect} allRows={allRows} />;
    case "RDS":    return <RDSTable    rows={rows} selected={selected} onSelect={onSelect} />;
    case "Lambda": return <LambdaTable rows={rows} selected={selected} onSelect={onSelect} />;
    case "S3":     return <S3Table     rows={rows} selected={selected} onSelect={onSelect} />;
    case "ELB":    return <ELBTable    rows={rows} selected={selected} onSelect={onSelect} />;
    case "ECS":    return <ECSTable    rows={rows} selected={selected} onSelect={onSelect} />;
    default:       return null;
  }
}

function EC2Table({ rows, selected, onSelect, activeAlerts = [] }) {
  function getRowAlert(instanceId) {
    const a = activeAlerts.find(x => x.resource === instanceId);
    return a ? (a.severity||"").toUpperCase() : null;
  }
  return (
    <table className="inst-table">
      <thead>
        <tr>
          <th>NAME / ID</th><th>TYPE</th><th>STATE</th><th>ZONE</th>
          <th>CPU %</th><th>STATUS CHECK</th><th>UPTIME</th>
        </tr>
      </thead>
      <tbody>{rows.map(r => {
        const alertSev = getRowAlert(r.instance_id);
        return (
          <tr key={r.instance_id} className={`inst-row ${selected?.instance_id === r.instance_id ? "inst-selected" : ""}`} onClick={() => onSelect(r)}>
            <td>
              <div style={{ display:"flex", alignItems:"center", gap:6 }}>
                {alertSev && (
                  <span style={{
                    fontSize:9, fontWeight:700, padding:"1px 5px", borderRadius:4,
                    background: alertSev==="CRITICAL" ? "rgba(239,68,68,0.15)" : "rgba(245,158,11,0.15)",
                    color: alertSev==="CRITICAL" ? "#ef4444" : "#f59e0b",
                    border: `1px solid ${alertSev==="CRITICAL" ? "rgba(239,68,68,0.3)" : "rgba(245,158,11,0.3)"}`,
                    fontFamily:"var(--font-mono)",
                    display:"inline-flex", alignItems:"center", gap:3,
                  }}>
                    {alertSev==="CRITICAL" ? <RedDotIcon size={9} /> : <AlertTriangleIcon size={9} />} {alertSev}
                  </span>
                )}
                <div>
                  <div className="inst-name">{r.name || r.instance_id}</div>
                  <div className="inst-id mono">{r.instance_id}</div>
                </div>
              </div>
            </td>
            <td className="mono">{r.instance_type}</td>
            <td><StateBadge state={r.state} /></td>
            <td className="mono small">{r.availability_zone}</td>
            <td><CpuBar cpu={r.cpu_utilization} state={r.state} /></td>
            <td>
              {r.state === "running"
                ? <StatusCheckBadge value={r.status_check_failed ?? 0} />
                : <span className="mono small muted">—</span>}
            </td>
            <td className="mono small">{r.uptime_days ?? "—"}d</td>
          </tr>
        );
      })}</tbody>
    </table>
  );
}

function EBSTable({ rows, selected, onSelect }) {
  return (
    <table className="inst-table">
      <thead>
        <tr>
          <th>NAME / ID</th><th>TYPE</th><th>SIZE</th><th>STATE</th>
          <th>ZONE</th><th>IOPS</th><th>ENCRYPTED</th><th>ATTACHED TO</th>
        </tr>
      </thead>
      <tbody>{rows.map(r => (
        <tr key={r.volume_id} className={`inst-row ${selected?.volume_id === r.volume_id ? "inst-selected" : ""}`} onClick={() => onSelect(r)}>
          <td>
            <div className="inst-name">{r.name || r.volume_id}</div>
            <div className="inst-id mono">{r.volume_id}</div>
          </td>
          <td className="mono small">{r.volume_type}</td>
          <td className="mono small">{r.size_gb} GB</td>
          <td><StatusChip status={r.state} colorMap={{ "in-use": "green", available: "blue", error: "red" }} /></td>
          <td className="mono small">{r.availability_zone}</td>
          <td className="mono small">{r.iops ?? "—"}</td>
          <td className="mono small">{r.encrypted ? <span style={{display:"inline-flex",alignItems:"center",gap:4}}><LockIcon size={11} /> Yes</span> : "No"}</td>
          <td>
            {r.attached_to ? (
              <div>
                {r.attached_instance_name && (
                  <div className="inst-name" style={{ fontSize: 11 }}>{r.attached_instance_name}</div>
                )}
                <div className="inst-id mono" style={{ fontSize: 10 }}>{r.attached_to}</div>
              </div>
            ) : (
              <span className="mono small muted">—</span>
            )}
          </td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function RDSTable({ rows, selected, onSelect }) {
  return (
    <table className="inst-table">
      <thead><tr><th>IDENTIFIER</th><th>ENGINE</th><th>CLASS</th><th>STATUS</th><th>MULTI-AZ</th><th>STORAGE</th><th>ENDPOINT</th></tr></thead>
      <tbody>{rows.map(r => (
        <tr key={r.db_instance_id || r.identifier} className={`inst-row ${selected?.db_instance_id === r.db_instance_id ? "inst-selected" : ""}`} onClick={() => onSelect(r)}>
          <td><div className="inst-name">{r.identifier || r.db_instance_id}</div><div className="inst-id mono">{r.db_instance_id}</div></td>
          <td className="mono small">{r.engine} {r.engine_version}</td>
          <td className="mono small">{r.instance_class}</td>
          <td><StatusChip status={r.status} /></td>
          <td className="mono small">{r.multi_az ? <span style={{display:"inline-flex",alignItems:"center",gap:4}}><CheckIcon size={11} /> Yes</span> : "No"}</td>
          <td className="mono small">{r.allocated_storage ?? "—"} GB</td>
          <td className="mono small truncate">{r.endpoint || "—"}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function LambdaTable({ rows, selected, onSelect }) {
  return (
    <table className="inst-table">
      <thead><tr><th>FUNCTION NAME</th><th>RUNTIME</th><th>MEMORY</th><th>TIMEOUT</th><th>LAST MODIFIED</th><th>SIZE</th></tr></thead>
      <tbody>{rows.map((r, idx) => (
        <tr key={r.function_name || `lambda-${idx}`} className={`inst-row ${selected?.function_name === r.function_name ? "inst-selected" : ""}`} onClick={() => onSelect(r)}>
          <td><div className="inst-name">{r.function_name}</div><div className="inst-id mono">{r.function_arn?.split(":").slice(-1)[0] ?? ""}</div></td>
          <td className="mono small">{r.runtime}</td>
          <td className="mono small">{r.memory_size ?? "—"} MB</td>
          <td className="mono small">{r.timeout ?? "—"}s</td>
          <td className="mono small">{r.last_modified ? shortDate(r.last_modified) : "—"}</td>
          <td className="mono small">{r.code_size ? fmtBytes(r.code_size) : "—"}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function S3Table({ rows, selected, onSelect }) {
  return (
    <table className="inst-table">
      <thead><tr><th>BUCKET NAME</th><th>REGION</th><th>CREATED</th><th>VERSIONING</th><th>ACCESS</th></tr></thead>
      <tbody>{rows.map(r => (
        <tr key={r.bucket_name || r.name} className={`inst-row ${selected?.bucket_name === r.bucket_name ? "inst-selected" : ""}`} onClick={() => onSelect(r)}>
          <td><div className="inst-name">{r.bucket_name || r.name}</div></td>
          <td className="mono small">{r.region || "—"}</td>
          <td className="mono small">{r.creation_date ? shortDate(r.creation_date) : "—"}</td>
          <td className="mono small">{r.versioning ?? "—"}</td>
          <td><StatusChip status={r.public_access === false ? "private" : r.public_access === true ? "public" : "—"} colorMap={{ private: "green", public: "red" }} /></td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function ELBTable({ rows, selected, onSelect }) {
  return (
    <table className="inst-table">
      <thead><tr><th>NAME</th><th>TYPE</th><th>SCHEME</th><th>STATE</th><th>DNS NAME</th><th>AZs</th><th>CREATED</th></tr></thead>
      <tbody>{rows.map(r => (
        <tr key={r.load_balancer_arn || r.name} className={`inst-row ${selected?.load_balancer_arn === r.load_balancer_arn ? "inst-selected" : ""}`} onClick={() => onSelect(r)}>
          <td><div className="inst-name">{r.name}</div><div className="inst-id mono small truncate">{r.load_balancer_arn?.split("/").slice(-1)[0] ?? ""}</div></td>
          <td className="mono small">{r.type || "—"}</td>
          <td className="mono small">{r.scheme || "—"}</td>
          <td><StatusChip status={r.state || r.status} /></td>
          <td className="mono small truncate">{r.dns_name || "—"}</td>
          <td className="mono small">{Array.isArray(r.availability_zones) ? r.availability_zones.join(", ") : r.availability_zones || "—"}</td>
          <td className="mono small">{r.created_time ? shortDate(r.created_time) : "—"}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function ECSTable({ rows, selected, onSelect }) {
  // rows = array of cluster objects; flatten to service rows for table
  const allServices = rows.flatMap(cluster =>
    (cluster.services || []).map(s => ({
      ...s,
      cluster_name: cluster.cluster_name,
      region:       cluster.region,
    }))
  );
  if (allServices.length === 0) {
    return (
      <table className="inst-table">
        <tbody><tr><td colSpan={8} className="tbl-empty">
          {rows.length > 0 ? `${rows.length} cluster(s) found but no services running.` : "No ECS clusters found."}
        </td></tr></tbody>
      </table>
    );
  }
  return (
    <table className="inst-table">
      <thead>
        <tr>
          <th>SERVICE NAME</th><th>CLUSTER</th><th>STATUS</th><th>LAUNCH</th>
          <th>DESIRED</th><th>RUNNING</th><th>CPU %</th><th>MEM %</th>
        </tr>
      </thead>
      <tbody>
        {allServices.map((s, i) => (
          <tr
            key={`${s.cluster_name}-${s.service_name}-${i}`}
            className={`inst-row ${selected?.service_name === s.service_name && selected?.cluster_name === s.cluster_name ? "inst-selected" : ""}`}
            onClick={() => onSelect(s)}
          >
            <td>
              <div className="inst-name">{s.service_name}</div>
              <div className="inst-id mono">{s.task_definition}</div>
            </td>
            <td className="mono small">{s.cluster_name}</td>
            <td><StatusChip status={s.status} /></td>
            <td className="mono small">{s.launch_type}</td>
            <td className="mono small">{s.desired_count}</td>
            <td className="mono small">{s.running_count}</td>
            <td><CpuBar cpu={s.cpu_utilization} state="running" /></td>
            <td>
              <div className="cpu-cell">
                <div className="cpu-bar-bg">
                  <div className="cpu-bar-fill" style={{
                    width: `${Math.max(2, s.mem_utilization || 0)}%`,
                    background: (s.mem_utilization || 0) > 85 ? "#ef4444" : (s.mem_utilization || 0) > 70 ? "#f59e0b" : "#7c6ee0"
                  }} />
                </div>
                <span className="cpu-label mono">{(s.mem_utilization || 0).toFixed(1)}%</span>
              </div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ResourceRelationships({ service, row, allRows, onSelectRelated }) {
  if (service === "EC2" && allRows) {
    const attachedVolumes = (allRows._ebs || []).filter(v =>
      v.attached_to && v.attached_to.includes(row.instance_id)
    );
    const blockDevices = row.block_device_mappings || row.volumes || [];
    if (attachedVolumes.length === 0 && blockDevices.length === 0) return null;
    return (
      <div className="id-section">
        <div className="id-section-title"><LinkIcon size={12} /> ATTACHED VOLUMES</div>
        <div className="rel-list">
          {attachedVolumes.length > 0 ? attachedVolumes.map(v => (
            <div key={v.volume_id} className="rel-item">
              <span className="rel-icon"><SaveIcon size={16} /></span>
              <div className="rel-info">
                <div className="rel-name">{v.name || v.volume_id}</div>
                <div className="rel-sub mono">{v.volume_id} · {v.volume_type} · {v.size_gb}GB · {v.state}</div>
              </div>
              <StatusChip status={v.state} colorMap={{ "in-use": "green", available: "blue" }} />
            </div>
          )) : blockDevices.map((d, i) => (
            <div key={i} className="rel-item">
              <span className="rel-icon"><SaveIcon size={16} /></span>
              <div className="rel-info">
                <div className="rel-name">{d.volume_id || d.device_name || `Volume ${i + 1}`}</div>
                <div className="rel-sub mono">{d.device_name || ""} · {d.status || "attached"}</div>
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (service === "EBS") {
    const attachedTo = row.attached_to;
    if (!attachedTo) return null;
    const instanceId = typeof attachedTo === "string"
      ? attachedTo.split(",")[0].trim()
      : attachedTo;
    return (
      <div className="id-section">
        <div className="id-section-title"><LinkIcon size={12} /> ATTACHED TO INSTANCE</div>
        <div className="rel-list">
          <div className="rel-item">
            <span className="rel-icon"><ServerIcon size={16} /></span>
            <div className="rel-info">
              {row.attached_instance_name && (
                <div className="rel-name">{row.attached_instance_name}</div>
              )}
              <div className={row.attached_instance_name ? "rel-sub mono" : "rel-name mono"}>{instanceId}</div>
              <div className="rel-sub mono">EC2 Instance · {row.availability_zone}</div>
            </div>
            <span className="rel-badge">Attached</span>
          </div>
        </div>
      </div>
    );
  }

  if (service === "RDS" && row.endpoint) {
    return (
      <div className="id-section">
        <div className="id-section-title"><LinkIcon size={12} /> NETWORK</div>
        <div className="rel-list">
          <div className="rel-item">
            <span className="rel-icon"><GlobeIcon size={16} /></span>
            <div className="rel-info">
              <div className="rel-name">Database Endpoint</div>
              <div className="rel-sub mono truncate">{row.endpoint}</div>
            </div>
          </div>
          {row.vpc_id && (
            <div className="rel-item">
              <span className="rel-icon"><LayersIcon size={16} /></span>
              <div className="rel-info">
                <div className="rel-name">VPC</div>
                <div className="rel-sub mono">{row.vpc_id}</div>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }

  if (service === "ECS" && row.cluster_name) {
    return (
      <div className="id-section">
        <div className="id-section-title"><LinkIcon size={12} /> CLUSTER</div>
        <div className="rel-list">
          <div className="rel-item">
            <span className="rel-icon"><PackageIcon size={16} /></span>
            <div className="rel-info">
              <div className="rel-name">{row.cluster_name}</div>
              <div className="rel-sub mono">Task: {row.task_definition} · {row.launch_type}</div>
            </div>
            <StatusChip status={row.status} />
          </div>
        </div>
      </div>
    );
  }

  return null;
}

function ServiceDetailPanel({ service, row, metrics, mLoading, region, timeRange, onTimeRangeChange, allRows, onClose, onSelectRelated, accountId }) {
  const name = row.name || row.service_name || row.identifier || row.function_name || row.bucket_name || row.instance_id || "Resource";

  const noMetricsMsg = {
    EC2:    row.state === "stopped" ? "Instance stopped — start to see live metrics." : null,
    EBS:    row.state !== "in-use"  ? "Volume not attached — no I/O metrics."         : null,
    RDS:    null,
    Lambda: null,
    S3:     null,
    ELB:    null,
    ECS:    null,
  }[service];

  const rangLabel = TIME_RANGES.find(t => t.hours === timeRange)?.label || "6H";

  const s3SizeDisplay = row.size_bytes
    ? fmtBytes(row.size_bytes)
    : row.bucket_size_bytes
    ? fmtBytes(row.bucket_size_bytes)
    : "—";
  const s3ObjCount = row.object_count ?? row.objects ?? "—";

  return (
    <div className="inst-detail">
      <div className="id-header">
        <div>
          <div className="id-name">{name}</div>
          <div className="id-sub mono">{detailSubline(service, row)}</div>
        </div>
        <button className="id-close" onClick={onClose}><XIcon size={14} /></button>
      </div>

      <div className="id-stats">
        {detailStats(service, row).map(s => (
          <QuickStat key={s.label} label={s.label} value={s.value} color={s.color} mono={s.mono} />
        ))}
      </div>

      {service === "S3" && (
        <div className="id-section">
          <div className="id-section-title"><PackageIcon size={12} /> BUCKET INFO</div>
          <div className="id-stats" style={{ gridTemplateColumns: "1fr 1fr 1fr", marginBottom: 0 }}>
            <QuickStat label="Size"    value={s3SizeDisplay} />
            <QuickStat label="Objects" value={String(s3ObjCount)} />
            <QuickStat label="Created" value={row.creation_date ? shortDate(row.creation_date) : "—"} />
          </div>
          {(s3SizeDisplay === "—" && s3ObjCount === "—") && (
            <div style={{ fontSize: 11, color: "rgba(99,130,190,0.5)", marginTop: 8, fontStyle: "italic" }}>
              ℹ S3 size/object metrics require CloudWatch Storage Lens or S3 bucket metrics enabled. CW reports daily, not real-time.
            </div>
          )}
        </div>
      )}

      {service === "Lambda" && (
        <div className="id-section">
          <div className="id-section-title"><ZapIcon size={12} /> FUNCTION DETAILS</div>
          <div className="id-stats" style={{ gridTemplateColumns: "1fr 1fr", marginBottom: 0 }}>
            <QuickStat label="Handler"       value={row.handler      || "—"} mono />
            <QuickStat label="Description"   value={row.description  || "—"} />
            <QuickStat label="Last Modified" value={row.last_modified ? shortDate(row.last_modified) : "—"} />
            <QuickStat label="Code Size"     value={row.code_size ? fmtBytes(row.code_size) : "—"} />
          </div>
          {!metrics && !mLoading && (
            <div style={{ fontSize: 11, color: "rgba(99,130,190,0.5)", marginTop: 8, fontStyle: "italic" }}>
              ℹ Lambda invocation metrics appear only after the function is invoked.
            </div>
          )}
        </div>
      )}

      {service === "ECS" && (
        <div className="id-section">
          <div className="id-section-title"><PackageIcon size={12} /> SERVICE DETAILS</div>
          <div className="id-stats" style={{ gridTemplateColumns: "1fr 1fr", marginBottom: 0 }}>
            <QuickStat label="Desired"  value={String(row.desired_count  ?? "—")} />
            <QuickStat label="Running"  value={String(row.running_count  ?? "—")} color={row.running_count === row.desired_count ? "green" : "yellow"} />
            <QuickStat label="Pending"  value={String(row.pending_count  ?? "—")} />
            <QuickStat label="Launch"   value={row.launch_type || "—"} mono />
          </div>
        </div>
      )}

      <ResourceRelationships
        service={service}
        row={row}
        allRows={allRows}
        onSelectRelated={onSelectRelated}
      />

      {Object.keys(row.tags || {}).length > 0 && (
        <div className="id-section">
          <div className="id-section-title"><TagIcon size={12} /> TAGS</div>
          <div className="id-tags">
            {Object.entries(row.tags).map(([k, v]) => (
              <div key={k} className="id-tag">
                <span className="id-tag-key">{k}</span>
                <span className="id-tag-val">{v}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="id-section">
        <div className="id-section-title-row">
          <span className="id-section-title" style={{ marginBottom: 0 }}><BarChartIcon size={12} /> CLOUDWATCH METRICS</span>
          {!noMetricsMsg && (
            <div className="time-range-tabs">
              {TIME_RANGES.map(t => (
                <button
                  key={t.label}
                  className={`tr-btn ${timeRange === t.hours ? "tr-active" : ""}`}
                  onClick={() => onTimeRangeChange(t.hours)}
                >
                  {t.label}
                </button>
              ))}
            </div>
          )}
        </div>

        {noMetricsMsg ? (
          <div style={{
            display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center",
            padding:"40px 20px", gap:12, textAlign:"center",
          }}>
            <div style={{ display:"flex", justifyContent:"center" }}><ToolIcon size={32} /></div>
            <div style={{ fontWeight:700, fontSize:14, color:"var(--text-primary)" }}>No Metrics Available</div>
            <div style={{ fontSize:12, color:"var(--text-muted)", maxWidth:280, lineHeight:1.6 }}>{noMetricsMsg}</div>
          </div>
        ) : mLoading ? (
          <div className="id-loading">⏳ Fetching CloudWatch data…</div>
        ) : metrics ? (
          <div className="charts-grid">
            {service === "EC2" && (
              <div className="chart-full">
                <MetricChart title="CPU Utilization %" data={metrics.cpu} color="#2bb3ac" unit="%" threshold={85} thresholdLabel="alert threshold" timeRange={rangLabel} />
              </div>
            )}
            {service === "EC2" && <>
              <MetricChart title="Network In (KB)"  data={metrics.network_in?.map(d => ({ ...d, v: d.v / 1024 }))}  color="#22c55e" unit="KB" timeRange={rangLabel} />
              <MetricChart title="Network Out (KB)" data={metrics.network_out?.map(d => ({ ...d, v: d.v / 1024 }))} color="#7c6ee0" unit="KB" timeRange={rangLabel} />
              <MetricChart title="Disk Read (bytes)"  data={metrics.disk_read}  color="#fbbf24" unit="B" timeRange={rangLabel} />
              <MetricChart title="Disk Write (bytes)" data={metrics.disk_write} color="#fb7185" unit="B" timeRange={rangLabel} />
            </>}

            {service === "EBS" && <>
              <MetricChart title="Read Ops/s"      data={metrics.read_ops}      color="#38bdf8" unit=" ops" timeRange={rangLabel} />
              <MetricChart title="Write Ops/s"     data={metrics.write_ops}     color="#7c6ee0" unit=" ops" timeRange={rangLabel} />
              <MetricChart title="Read Bytes"      data={metrics.read_bytes}    color="#22c55e" unit="B"    timeRange={rangLabel} />
              <MetricChart title="Write Bytes"     data={metrics.write_bytes}   color="#fbbf24" unit="B"    timeRange={rangLabel} />
              <MetricChart title="Queue Length"    data={metrics.queue_length}  color="#ef4444" unit=""     threshold={5} timeRange={rangLabel} />
              <MetricChart title="Burst Balance %" data={metrics.burst_balance} color="#2bb3ac" unit="%"    threshold={20} timeRange={rangLabel} />
            </>}

            {service === "Lambda" && <>
              <MetricChart title="Invocations"     data={metrics.invocations} color="#22c55e" unit=""   timeRange={rangLabel} />
              <MetricChart title="Errors"          data={metrics.errors}      color="#ef4444" unit=""   threshold={5} timeRange={rangLabel} />
              <div className="chart-full">
                <MetricChart title="Duration (ms)" data={metrics.duration}    color="#2bb3ac" unit="ms" threshold={8000} timeRange={rangLabel} />
              </div>
              <MetricChart title="Throttles"       data={metrics.throttles}   color="#f59e0b" unit=""   timeRange={rangLabel} />
              <MetricChart title="Concurrent Exec" data={metrics.concurrent}  color="#7c6ee0" unit=""   timeRange={rangLabel} />
            </>}

            {service === "RDS" && <>
              <div className="chart-full">
                <MetricChart title="CPU Utilization %" data={metrics.cpu} color="#2bb3ac" unit="%" threshold={85} timeRange={rangLabel} />
              </div>
              <MetricChart title="DB Connections"  data={metrics.db_connections}  color="#7c6ee0" unit=""    timeRange={rangLabel} />
              <MetricChart title="Free Memory"     data={metrics.freeable_memory} color="#f472b6" unit="B"   timeRange={rangLabel} />
              <MetricChart title="Read IOPS"       data={metrics.read_iops}       color="#22c55e" unit=" ops" timeRange={rangLabel} />
              <MetricChart title="Write IOPS"      data={metrics.write_iops}      color="#fbbf24" unit=" ops" timeRange={rangLabel} />
              <MetricChart title="Read Latency"    data={metrics.read_latency}    color="#38bdf8" unit="s"   threshold={0.02} timeRange={rangLabel} />
              <MetricChart title="Write Latency"   data={metrics.write_latency}   color="#e879f9" unit="s"   threshold={0.02} timeRange={rangLabel} />
            </>}

            {service === "S3" && <>
              <div className="chart-full">
                <MetricChart title="Bucket Size (bytes)" data={metrics?.bucket_size   || []} color="#fbbf24" unit="B" timeRange={rangLabel} />
              </div>
              <div className="chart-full">
                <MetricChart title="Object Count"        data={metrics?.object_count  || []} color="#22c55e" unit=""  timeRange={rangLabel} />
              </div>
              <MetricChart title="All Requests"          data={metrics?.all_requests  || []} color="#2bb3ac" unit=""  timeRange={rangLabel} />
              <MetricChart title="GET Requests"          data={metrics?.get_requests  || []} color="#7c6ee0" unit=""  timeRange={rangLabel} />
              <MetricChart title="PUT Requests"          data={metrics?.put_requests  || []} color="#38bdf8" unit=""  timeRange={rangLabel} />
              <MetricChart title="4XX Errors"            data={metrics?.errors_4xx    || []} color="#f59e0b" unit=""  timeRange={rangLabel} />
              <MetricChart title="5XX Errors"            data={metrics?.errors_5xx    || []} color="#ef4444" unit=""  threshold={5} timeRange={rangLabel} />
              <MetricChart title="Bytes Downloaded"      data={metrics?.bytes_download|| []} color="#f472b6" unit="B" timeRange={rangLabel} />
            </>}

            {service === "ELB" && <>
              <div className="chart-full">
                <MetricChart title="Request Count"           data={metrics?.requests           || []} color="#2bb3ac" unit=""  timeRange={rangLabel} />
              </div>
              <MetricChart title="5XX Errors (Target)"       data={metrics?.errors_5xx         || []} color="#ef4444" unit=""  threshold={20} timeRange={rangLabel} />
              <MetricChart title="4XX Errors (Target)"       data={metrics?.errors_4xx         || []} color="#f59e0b" unit=""  threshold={50} timeRange={rangLabel} />
              <MetricChart title="5XX Errors (ELB)"          data={metrics?.errors_elb_5xx     || []} color="#f472b6" unit=""  threshold={5}  timeRange={rangLabel} />
              <div className="chart-full">
                <MetricChart title="Target Response Time (s)" data={metrics?.latency           || []} color="#fbbf24" unit="s" threshold={0.5} timeRange={rangLabel} />
              </div>
              <MetricChart title="Healthy Hosts"             data={metrics?.healthy_hosts      || []} color="#22c55e" unit=""  timeRange={rangLabel} />
              <MetricChart title="Unhealthy Hosts"           data={metrics?.unhealthy_hosts    || []} color="#ef4444" unit=""  threshold={1} timeRange={rangLabel} />
              <MetricChart title="Active Connections"        data={metrics?.active_connections || []} color="#7c6ee0" unit=""  timeRange={rangLabel} />
              <MetricChart title="New Connections"           data={metrics?.new_connections    || []} color="#38bdf8" unit=""  timeRange={rangLabel} />
            </>}

            {service === "ECS" && <>
              <div className="chart-full">
                <MetricChart title="CPU Utilization %"    data={metrics?.cpu_utilization    || []} color="#34d399" unit="%" threshold={85} timeRange={rangLabel} />
              </div>
              <div className="chart-full">
                <MetricChart title="Memory Utilization %" data={metrics?.mem_utilization    || []} color="#7c6ee0" unit="%" threshold={85} timeRange={rangLabel} />
              </div>
              <MetricChart title="Running Tasks"          data={metrics?.running_task_count || []} color="#22c55e" unit=""  timeRange={rangLabel} />
              <MetricChart title="Pending Tasks"          data={metrics?.pending_task_count || []} color="#f59e0b" unit=""  timeRange={rangLabel} />
              <MetricChart title="Desired Tasks"          data={metrics?.desired_task_count || []} color="#38bdf8" unit=""  timeRange={rangLabel} />
              <MetricChart title="CPU Reserved"           data={metrics?.cpu_reserved       || []} color="#f472b6" unit=""  timeRange={rangLabel} />
              <MetricChart title="Memory Reserved"        data={metrics?.mem_reserved       || []} color="#fbbf24" unit=""  timeRange={rangLabel} />
            </>}
          </div>
        ) : (
          <div className="id-no-metrics">No metric data in last {rangLabel} — resource may be idle. Try a longer time range.</div>
        )}
      </div>

      <button
        className="btn-open-aws"
        onClick={() => openAccountConsole(accountId, service, { region, ...consoleParamsFor(service, row) })}
      >
        <CloudIcon size={13} /> Open in AWS <ExternalLinkIcon size={12} />
      </button>

      <style>{`
        .id-section-title-row {
          display: flex; align-items: center; justify-content: space-between;
          margin-bottom: 10px; flex-wrap: wrap; gap: 8px;
        }
        .time-range-tabs {
          display: flex; gap: 2px;
          background: rgba(8,14,26,0.6); border: 1px solid rgba(99,130,190,0.15);
          border-radius: 6px; padding: 3px;
        }
        .tr-btn {
          background: none; border: none; color: rgba(99,130,190,0.6);
          font-size: 10px; font-family: monospace; padding: 3px 7px;
          border-radius: 4px; cursor: pointer; letter-spacing: 0.5px;
          transition: all 0.15s; white-space: nowrap;
        }
        .tr-btn:hover { color: #a8bdd8; background: rgba(99,130,190,0.1); }
        .tr-active { background: rgba(43,179,172,0.15) !important; color: #2bb3ac !important; font-weight: 700; }
        .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
        .chart-full { grid-column: 1 / -1; }
        .rel-list { display: flex; flex-direction: column; gap: 6px; }
        .rel-item {
          display: flex; align-items: center; gap: 10px;
          background: rgba(8,14,26,0.5); border: 1px solid rgba(99,130,190,0.12);
          border-radius: 7px; padding: 8px 10px; transition: border-color 0.15s;
        }
        .rel-item:hover { border-color: rgba(99,130,190,0.25); }
        .rel-icon { font-size: 16px; flex-shrink: 0; }
        .rel-info { flex: 1; min-width: 0; }
        .rel-name { font-size: 12px; font-weight: 600; color: #c8d8f0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .rel-sub  { font-size: 10px; color: rgba(99,130,190,0.55); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .rel-badge {
          font-size: 10px; font-family: monospace;
          background: rgba(34,197,94,0.12); color: #22c55e;
          border: 1px solid rgba(34,197,94,0.25); border-radius: 4px;
          padding: 2px 7px; white-space: nowrap; flex-shrink: 0;
        }
        .sc-ok   { font-size: 10px; font-weight: 700; color: #22c55e; font-family: monospace; display: inline-flex; align-items: center; gap: 3px; }
        .sc-fail { font-size: 10px; font-weight: 700; color: #ef4444; font-family: monospace; background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.25); border-radius: 4px; padding: 2px 7px; display: inline-flex; align-items: center; gap: 3px; }
      `}</style>
    </div>
  );
}

/* ── helpers ── */
function StatusCheckBadge({ value }) {
  if (value === 0) return <span className="sc-ok"><CheckIcon size={11} /> OK</span>;
  return <span className="sc-fail"><XCircleIcon size={11} /> FAILED ({value})</span>;
}

async function openAccountConsole(accountId, service, params = {}) {
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
}

function detailSubline(service, row) {
  switch (service) {
    case "EC2":    return `${row.instance_id} · ${row.instance_type} · ${row.availability_zone}`;
    case "EBS":    return `${row.volume_id} · ${row.volume_type} · ${row.availability_zone}`;
    case "RDS":    return `${row.db_instance_id} · ${row.engine} ${row.engine_version}`;
    case "Lambda": return `${row.runtime} · ${row.memory_size ?? "—"} MB · ${row.timeout}s timeout`;
    case "S3":     return `${row.region || ""} · Created ${row.creation_date ? shortDate(row.creation_date) : "—"}`;
    case "ELB":    return `${row.type} · ${row.scheme}`;
    case "ECS":    return `${row.cluster_name} · ${row.task_definition} · ${row.launch_type}`;
    default:       return "";
  }
}

function detailStats(service, row) {
  switch (service) {
    case "EC2": return [
      { label: "State",      value: row.state,                      color: row.state === "running" ? "green" : "muted" },
      { label: "Private IP", value: row.private_ip || "—",          mono: true },
      { label: "CPU",        value: `${row.cpu_utilization ?? 0}%`, color: (row.cpu_utilization ?? 0) > 75 ? "red" : "green" },
      { label: "Uptime",     value: `${row.uptime_days ?? "—"}d` },
    ];
    case "EBS": return [
      { label: "State",  value: row.state,          color: row.state === "in-use" ? "green" : "blue" },
      { label: "Size",   value: `${row.size_gb} GB` },
      { label: "Type",   value: row.volume_type,    mono: true },
      { label: "IOPS",   value: row.iops ?? "—" },
    ];
    case "RDS": return [
      { label: "Status",   value: row.status,          color: row.status === "available" ? "green" : "yellow" },
      { label: "Class",    value: row.instance_class,  mono: true },
      { label: "Storage",  value: `${row.allocated_storage ?? "—"} GB` },
      { label: "Multi-AZ", value: row.multi_az ? "Yes" : "No", color: row.multi_az ? "green" : "muted" },
    ];
    case "Lambda": return [
      { label: "Runtime", value: row.runtime,                           mono: true },
      { label: "Memory",  value: `${row.memory_size ?? "—"} MB` },
      { label: "Timeout", value: `${row.timeout ?? "—"}s` },
      { label: "Size",    value: row.code_size ? fmtBytes(row.code_size) : "—" },
    ];
    case "S3": return [
      { label: "Versioning", value: row.versioning || "—" },
      { label: "Access",     value: row.public_access === false ? "Private" : "Public", color: row.public_access === false ? "green" : "red" },
      { label: "Region",     value: row.region || "—", mono: true },
    ];
    case "ELB": return [
      { label: "State",  value: row.state || row.status || "—", color: (row.state || row.status) === "active" ? "green" : "yellow" },
      { label: "Type",   value: row.type   || "—" },
      { label: "Scheme", value: row.scheme || "—" },
      { label: "AZs",    value: Array.isArray(row.availability_zones) ? row.availability_zones.length : "—" },
    ];
    case "ECS": return [
      { label: "Status",   value: row.status        || "—", color: row.status === "ACTIVE" ? "green" : "yellow" },
      { label: "CPU %",    value: `${(row.cpu_utilization || 0).toFixed(1)}%`, color: (row.cpu_utilization || 0) > 75 ? "red" : "green" },
      { label: "Mem %",    value: `${(row.mem_utilization || 0).toFixed(1)}%`, color: (row.mem_utilization || 0) > 75 ? "red" : "green" },
      { label: "Running",  value: `${row.running_count ?? "—"} / ${row.desired_count ?? "—"}` },
    ];
    default: return [];
  }
}

function ServiceIcon({ icon: Icon, size = 16 }) {
  if (typeof Icon === "string") return <span style={{ fontSize: size * 0.7 }}>{Icon}</span>;
  return <Icon size={size} />;
}
function capitalize(s) { return s.charAt(0).toUpperCase() + s.slice(1); }
function shortDate(iso) { try { return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }); } catch { return iso; } }
function fmtBytes(b) { if (b == null) return "—"; if (b < 1024) return `${b} B`; if (b < 1048576) return `${(b / 1024).toFixed(1)} KB`; if (b < 1073741824) return `${(b / 1048576).toFixed(1)} MB`; return `${(b / 1073741824).toFixed(2)} GB`; }
function StateBadge({ state }) { const m = { running: "sb-green", stopped: "sb-muted", pending: "sb-yellow", terminated: "sb-red" }; return <span className={`state-badge ${m[state] || "sb-muted"}`}>{state}</span>; }
function StatusChip({ status, colorMap = {} }) { const s = (status || "").toLowerCase(); const d = { available: "green", active: "green", running: "green", "in-use": "green", stopped: "muted", failed: "red", public: "red", private: "green" }; const color = { ...d, ...colorMap }[s] || "yellow"; return <span className={`state-badge sb-${color}`} style={{ textTransform: "capitalize" }}>{status || "—"}</span>; }
function CpuBar({ cpu, state }) { if (state !== "running") return <span className="mono small muted">—</span>; const pct = cpu ?? 0; const color = pct > 75 ? "#ef4444" : pct > 50 ? "#f59e0b" : "#22c55e"; return <div className="cpu-cell"><div className="cpu-bar-bg"><div className="cpu-bar-fill" style={{ width: `${Math.max(2, pct)}%`, background: color }} /></div><span className="cpu-label mono">{pct.toFixed(1)}%</span></div>; }
function QuickStat({ label, value, color, mono }) { return <div className="qs-item"><div className="qs-label">{label}</div><div className={`qs-value ${color ? `c-${color}` : ""}${mono ? " mono" : ""}`}>{value}</div></div>; }

function MetricChart({ title, data, color, unit, threshold, thresholdLabel, timeRange }) {
  if (!data || data.length === 0) return (
    <div className="chart-box">
      <div className="chart-title">{title}</div>
      <div className="chart-empty">No data in last {timeRange || "6H"}</div>
    </div>
  );
  const latest = data[data.length - 1]?.v ?? 0;
  const formatted = data.map(d => ({
    t: new Date(d.t).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false }),
    v: d.v,
    ...(threshold ? { threshold } : {}),
  }));
  return (
    <div className="chart-box">
      <div className="chart-header">
        <span className="chart-title">{title}</span>
        <span className="chart-latest" style={{ color }}>{latest.toFixed(1)}{unit}</span>
      </div>
      <ResponsiveContainer width="100%" height={90}>
        <LineChart data={formatted} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
          <CartesianGrid stroke="rgba(99,130,190,0.08)" strokeDasharray="3 3" />
          <XAxis dataKey="t" tick={{ fontSize: 9, fill: "#3d5070" }} tickLine={false} axisLine={false} interval="preserveStartEnd" />
          <YAxis tick={{ fontSize: 9, fill: "#3d5070" }} tickLine={false} axisLine={false} />
          <Tooltip
            contentStyle={{ background: "#0b1220", border: "1px solid rgba(99,130,190,0.2)", borderRadius: 6, fontSize: 11 }}
            labelStyle={{ color: "#7a90b8" }}
            formatter={(value, name) => {
              if (name === "threshold") return [`${value}${unit} (${thresholdLabel || "threshold"})`, <span style={{display:"inline-flex",alignItems:"center",gap:4}}><AlertTriangleIcon size={11} /> Alert at</span>];
              return [`${value.toFixed(2)}${unit}`, title];
            }}
            itemStyle={{ color }}
          />
          {threshold && (
            <Line type="monotone" dataKey="threshold" stroke="#ef4444" strokeDasharray="4 4" dot={false} strokeWidth={1} legendType="none" />
          )}
          <Line type="monotone" dataKey="v" stroke={color} strokeWidth={2} dot={false} activeDot={{ r: 3, fill: color }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}