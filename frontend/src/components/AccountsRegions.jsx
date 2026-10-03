// components/AccountsRegions.jsx -- Settings > Accounts & Regions.
// The only place an account/region can be removed from monitoring. Moved here from a red cross on the Overview
// cards: removal is irreversible and wide, so it needs a deliberate dialog, not a dashboard button.
// RBAC: anyone who can open Settings sees the list; the Remove action needs `accounts.delete` (the same
// permission the server enforces on DELETE /api/admin/accounts/{id}, admin by default).
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { getLiveAccounts, deleteAccount } from "../api/api";
import { useAuth } from "../auth/AuthContext";
import { setCached } from "../utils/dataCache";
import { ConfirmDialog, Badge, EmptyState } from "./ui";
import { removalPhrase, groupAccounts, removalError, regionStatusTone } from "../utils/accountsAdmin";
import "./AccountsRegions.css";

const CACHE_KEY = "overview:accounts";      // same key the shell + Overview read, so they update at once

export default function AccountsRegions() {
  const { hasPermission } = useAuth();
  const canRemove = hasPermission("accounts.delete");
  const [rows, setRows] = useState(undefined);          // undefined = loading, null = not available
  const [target, setTarget] = useState(null);           // row being confirmed
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);                 // {kind: "ok"|"err", text}
  const [q, setQ] = useState("");

  const load = useCallback(() => getLiveAccounts().then(r => setRows(Array.isArray(r) ? r : [])).catch(() => setRows(null)), []);
  useEffect(() => { load(); }, [load]);

  const groups = useMemo(() => groupAccounts((rows || []).filter(r => !q.trim() || `${r.account_name} ${r.account_id} ${r.region}`.toLowerCase().includes(q.trim().toLowerCase()))), [rows, q]);

  function afterRemoved(row, text) {
    const rest = (rows || []).filter(r => r.id !== row.id);
    setRows(rest);
    setCached(CACHE_KEY, { accounts: rest });
    window.dispatchEvent(new Event("mh:accounts-changed"));       // sidebar tree + scope switcher refresh immediately
    setMsg({ kind: "ok", text });
  }
  async function confirmRemove() {
    if (!target || busy) return;
    setBusy(true);
    try {
      await deleteAccount(target.id);
      afterRemoved(target, `Removed ${target.account_name} (${target.region}) from monitoring.`);
      setTarget(null);
    } catch (e) {
      const er = removalError(e);
      if (er.gone) { afterRemoved(target, er.text); setTarget(null); }
      else { setMsg({ kind: "err", text: er.text }); setTarget(null); }
    } finally { setBusy(false); }
  }

  return (
    <div className="settings-card" id="accounts">
      <div className="card-header">
        <div className="card-title-row"><span className="card-title">ACCOUNTS &amp; REGIONS</span></div>
        <Link className="ov-link" to="/onboarding" style={{ fontSize: 12, fontWeight: 600 }}>Add an account or region →</Link>
      </div>
      <div className="card-body ar-body">
        <p className="ar-note">Every monitored cloud account and region. {canRemove
          ? "Removing one stops monitoring and permanently deletes its collected data."
          : "Removing an account or region needs the “Remove Accounts” permission; ask an administrator."}</p>
        {msg && <div className={`ar-msg ${msg.kind}`} role={msg.kind === "err" ? "alert" : "status"}>{msg.text}<button type="button" className="ar-x" onClick={() => setMsg(null)} aria-label="Dismiss message">Dismiss</button></div>}
        {rows && rows.length > 8 && <input className="ui-input ar-search" placeholder="Filter accounts and regions…" aria-label="Filter accounts and regions" value={q} onChange={e => setQ(e.target.value)} />}
        {rows === undefined ? <div className="ar-note">Loading…</div>
          : rows === null ? <EmptyState title="Accounts aren't available" body="The list couldn't be loaded for your role right now." />
          : groups.length === 0 ? <EmptyState title={q ? "No match" : "No accounts yet"} body={q ? "Clear the filter." : "Onboard an account to start monitoring."} />
          : groups.map(g => (
            <section key={g.key} className="ar-acct" aria-label={g.account_name}>
              <header><b>{g.account_name}</b><span className="ar-id">{g.account_id}</span><span className="ar-n">{g.regions.length} region{g.regions.length === 1 ? "" : "s"}</span></header>
              <table className="ar-table"><thead><tr><th>Region</th><th>Status</th><th>Alerts</th><th>Last sync</th>{canRemove && <th aria-label="Actions" />}</tr></thead>
                <tbody>{g.regions.map(r => (
                  <tr key={r.id}>
                    <td className="mono">{r.region}</td>
                    <td><Badge tone={regionStatusTone(r.status)}>{r.status || "unknown"}</Badge></td>
                    <td className="mono">{(r.critical_alerts || 0) + (r.warning_alerts || 0) || "—"}</td>
                    <td className="mono">{r.last_synced_at ? new Date(r.last_synced_at.includes("T") || /Z$/.test(r.last_synced_at) ? r.last_synced_at : r.last_synced_at.replace(" ", "T") + "Z").toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) : "—"}</td>
                    {canRemove && <td className="ar-act"><button type="button" className="ar-remove" onClick={() => { setMsg(null); setTarget(r); }} aria-label={`Remove ${r.account_name} ${r.region}`}>Remove…</button></td>}
                  </tr>))}</tbody></table>
            </section>))}
      </div>
      <ConfirmDialog open={!!target} danger busy={busy} title={target ? `Remove ${target.account_name} · ${target.region}?` : ""}
        confirmLabel="Remove permanently" typeToConfirm={target ? removalPhrase(target) : undefined} onConfirm={confirmRemove} onCancel={() => !busy && setTarget(null)}
        body={<>
          <p>This stops monitoring this region and <b>permanently deletes</b> everything collected for it:</p>
          <ul className="ar-list"><li>alerts, metrics and discovered resources</li><li>incidents, health scores and cloud events</li><li>SLOs, synthetic checks, security findings, maintenance windows</li><li>escalation policies, status-page components and stored credentials</li></ul>
          <p>Past generated reports are kept. This cannot be undone; the account can only be onboarded again from scratch.</p></>} />
    </div>
  );
}
