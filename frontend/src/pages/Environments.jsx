// src/pages/Environments.jsx
// Phase 4a: environment classification. Two views over the same data:
//   - Needs classification: resources no override / tag / name token / account default could
//     place. They are alerted like prod until someone settles them here.
//   - All resources: every resource with its environment and HOW it was decided.
// Writes need environments.manage; everything is scoped to the caller's accounts server-side.
import { useState, useEffect, useCallback } from "react";
import {
  getEnvironmentSummary, getEnvironmentResources, classifyResources, setAccountDefaultEnv,
} from "../api/api";
import { useAuth } from "../auth/AuthContext";
import "./Environments.css";

const PAGE = 50;
const SOURCE_LABEL = { manual: "Manual", tag: "Tag", name: "Name", account: "Account default", unknown: "Unknown" };
const ENV_LABEL = { prod: "Prod", uat: "UAT", dev: "Dev", unknown: "Unknown", ignore: "Ignored" };

function EnvBadge({ env }) {
  if (!env) return <span className="env-badge env-none">Unclassified</span>;
  return <span className={`env-badge env-${env}`}>{ENV_LABEL[env] || env}</span>;
}

export default function Environments() {
  const { hasPermission } = useAuth();
  const canManage = hasPermission("environments.manage");
  const [view, setView] = useState("needs_classification");
  const [summary, setSummary] = useState(null);
  const [data, setData] = useState({ items: [], total: 0 });
  const [offset, setOffset] = useState(0);
  const [filters, setFilters] = useState({ account_id: "", q: "", env: "", source: "" });
  const [selected, setSelected] = useState(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  const load = useCallback(() => {
    getEnvironmentSummary().then(setSummary).catch(e => setError(e.message));
    getEnvironmentResources({ view, ...filters, limit: PAGE, offset })
      .then(d => { setData(d); setError(null); })
      .catch(e => setError(e.message));
  }, [view, filters, offset]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { setSelected(new Set()); }, [view, filters, offset]);

  const setFilter = (k, v) => { setOffset(0); setFilters(f => ({ ...f, [k]: v })); };
  const switchView = v => { setView(v); setOffset(0); setFilters(f => ({ ...f, env: "", source: "" })); };

  const toggle = id => setSelected(prev => {
    const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n;
  });
  const allOnPage = data.items.length > 0 && data.items.every(r => selected.has(r.id));
  const togglePage = () => setSelected(allOnPage ? new Set() : new Set(data.items.map(r => r.id)));

  const apply = async env => {
    if (!selected.size) return;
    setBusy(true); setNotice(null);
    try {
      const r = await classifyResources([...selected], env);
      setNotice(env === "clear"
        ? `Cleared the manual choice on ${r.updated} resource(s).`
        : `Marked ${r.updated} resource(s) as ${ENV_LABEL[env] || env}.`);
      setSelected(new Set());
      load();
    } catch (e) { setError(e.message); }
    setBusy(false);
  };

  const changeDefault = async (accountId, env) => {
    setBusy(true); setNotice(null);
    try {
      const r = await setAccountDefaultEnv(accountId, env || null);
      setNotice(`Account default ${env ? `set to ${ENV_LABEL[env]}` : "cleared"}; ${r.reclassified} resource(s) re-evaluated.`);
      load();
    } catch (e) { setError(e.message); }
    setBusy(false);
  };

  const pages = Math.max(1, Math.ceil(data.total / PAGE));
  const page = Math.floor(offset / PAGE) + 1;

  return (
    <div className="env-page">
      <div className="c-header">
        <div>
          <h1>Environment <span className="hl">classification</span></h1>
          <div className="sub">
            Unclassified resources are alerted at prod strength until placed here.
            Ignoring a resource only removes it from this queue; it does not silence its alerts.
          </div>
        </div>
      </div>

      {error && <div className="env-error">{error}</div>}
      {notice && <div className="env-notice">{notice}</div>}

      {summary && (
        <div className="env-accounts">
          {summary.accounts.map(a => (
            <div key={a.account_id} className="env-account">
              <div className="env-account-name">{a.account_name}</div>
              <div className="env-account-meta">{a.total} resources, {a.needs_classification} to classify</div>
              <label className="env-field">
                <span>Account default</span>
                <select value={a.default_env || ""} disabled={!canManage || busy}
                        onChange={e => changeDefault(a.account_id, e.target.value)}>
                  <option value="">None</option>
                  <option value="prod">Prod</option>
                  <option value="uat">UAT</option>
                  <option value="dev">Dev</option>
                </select>
              </label>
            </div>
          ))}
        </div>
      )}

      <div className="env-tabs">
        <button className={view === "needs_classification" ? "active" : ""} onClick={() => switchView("needs_classification")}>
          Needs classification{summary ? ` (${summary.needs_classification})` : ""}
        </button>
        <button className={view === "all" ? "active" : ""} onClick={() => switchView("all")}>All resources</button>
      </div>

      <div className="env-filters">
        <select value={filters.account_id} onChange={e => setFilter("account_id", e.target.value)}>
          <option value="">All accounts</option>
          {(summary?.accounts || []).map(a => <option key={a.account_id} value={a.account_id}>{a.account_name}</option>)}
        </select>
        {view === "all" && (
          <>
            <select value={filters.env} onChange={e => setFilter("env", e.target.value)}>
              <option value="">Any environment</option>
              {["prod", "uat", "dev", "unknown", "ignore", "unclassified"].map(v =>
                <option key={v} value={v}>{ENV_LABEL[v] || "Unclassified"}</option>)}
            </select>
            <select value={filters.source} onChange={e => setFilter("source", e.target.value)}>
              <option value="">Any source</option>
              {Object.entries(SOURCE_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
          </>
        )}
        <input placeholder="Search name or ID" value={filters.q} onChange={e => setFilter("q", e.target.value)} />
      </div>

      {canManage && (
        <div className="env-bulk">
          <span>{selected.size} selected</span>
          {["prod", "uat", "dev", "ignore"].map(e => (
            <button key={e} disabled={!selected.size || busy} onClick={() => apply(e)}>
              Mark {ENV_LABEL[e]}
            </button>
          ))}
          <button disabled={!selected.size || busy} onClick={() => apply("clear")}>Clear manual choice</button>
        </div>
      )}

      <table className="env-table">
        <thead>
          <tr>
            {canManage && <th><input type="checkbox" checked={allOnPage} onChange={togglePage} aria-label="Select page" /></th>}
            <th>Resource</th><th>Type</th><th>Account</th><th>Region</th>
            <th>Environment</th><th>Decided by</th><th>Detail</th>
          </tr>
        </thead>
        <tbody>
          {data.items.map(r => (
            <tr key={r.id}>
              {canManage && <td><input type="checkbox" checked={selected.has(r.id)} onChange={() => toggle(r.id)} /></td>}
              <td><div className="env-name">{r.name || r.resource_id}</div>{r.name && <div className="env-id">{r.resource_id}</div>}</td>
              <td>{r.resource_type}</td>
              <td>{r.account_name}</td>
              <td>{r.region || "-"}</td>
              <td><EnvBadge env={r.env} /></td>
              <td>{SOURCE_LABEL[r.source] || "Pending"}</td>
              <td className="env-reason">
                {r.reason}{r.override_by ? ` (by ${r.override_by})` : ""}
              </td>
            </tr>
          ))}
          {!data.items.length && (
            <tr><td colSpan={canManage ? 8 : 7} className="env-empty">
              {view === "needs_classification" ? "Nothing needs classification." : "No resources match."}
            </td></tr>
          )}
        </tbody>
      </table>

      <div className="env-pager">
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
        <span>Page {page} of {pages} ({data.total} resources)</span>
        <button disabled={page >= pages} onClick={() => setOffset(offset + PAGE)}>Next</button>
      </div>
    </div>
  );
}
