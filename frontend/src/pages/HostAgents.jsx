// src/pages/HostAgents.jsx
//
// Host agents (small binary installed on servers that PUSHES metrics to the hub; NOT the AI agent in app/agent/*).
// API: /api/host-agents/* (src/api/hostAgents.js). Everything here is gated twice: the nav entry / route need
// hostagents.view, the write controls need hostagents.manage, and the server enforces both regardless.
//
// Secrets: an enrollment key lives only in this component's React state while the "Add host" dialog is open. It is
// never put in a URL, localStorage, sessionStorage, a log line or an error message, and is gone when the dialog closes.
import { Suspense, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { PageHeader, Badge, FilterBar, DataTable, EmptyState, ConfirmDialog, Panel, Skeleton } from "../components/ui";
import ResourceDrawer from "../components/ResourceDrawer";
import { lazyWithRetry } from "../utils/lazyWithRetry";
import { useAuth } from "../auth/AuthContext";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { copyText } from "../utils/copyText";
import { getLiveAccounts, getAlertsForResource } from "../api/api";
import {
  listHostAgents, listEnrollmentKeys, createEnrollmentKey, revokeEnrollmentKey, revokeHostAgent, patchHostAgentConfig,
  getHostAgentMetrics, getHostAgentServices,
} from "../api/hostAgents";
import {
  STATE_LABEL, STATE_TONE, KEY_STATUS_TONE, INTERVAL_MIN, INTERVAL_MAX, stateOf, stateCounts, accountOptions,
  filterHosts, relativeTime, utcStamp, buildConfigPatch, buildKeyRequest, buildInstallCommand,
  firingAlertsForHost, mergeAlertLists, hostMetricRows, hostMetricsNote,
  serviceRows, serviceSummaryText, serviceWorstTone, servicesNote, configFormDefaults, currentConfigOf,
} from "../utils/hostAgentsView";
import "./HostAgents.css";

// The charts pull in recharts (the largest chunk), so they load only when a host drawer is opened.
const HostMetricCharts = lazyWithRetry(() => import("../components/HostMetricCharts"));

const REFRESH_MS = 30000;

const errText = e => {
  const base = e?.detail || e?.message || "Something went wrong";
  return e?.requestId ? `${base} (ref ${e.requestId})` : base;
};

// ── dialog shell (focus moves in, Tab stays inside, Esc closes unless `locked`, focus returns on close) ─────────────
function Modal({ title, onClose, locked = false, children, wide }) {
  const id = useId();
  const panel = useRef(null);
  const closeRef = useRef(onClose);
  useEffect(() => { closeRef.current = onClose; });
  useEffect(() => {
    const prev = document.activeElement;
    const first = panel.current?.querySelector("input,select,textarea,button:not([disabled])");
    (first || panel.current)?.focus();
    const h = e => {
      if (e.key === "Escape" && !locked) { closeRef.current(); return; }
      if (e.key !== "Tab" || !panel.current) return;
      const f = [...panel.current.querySelectorAll("button:not([disabled]),a[href],select,input:not([disabled]),textarea,[tabindex]:not([tabindex='-1'])")]
        .filter(x => x.offsetParent !== null);
      if (!f.length) { e.preventDefault(); return; }
      const a = f[0], z = f[f.length - 1];
      if (e.shiftKey && (document.activeElement === a || document.activeElement === panel.current)) { e.preventDefault(); z.focus(); }
      else if (!e.shiftKey && document.activeElement === z) { e.preventDefault(); a.focus(); }
    };
    document.addEventListener("keydown", h);
    return () => { document.removeEventListener("keydown", h); if (prev && prev.focus && document.contains(prev)) prev.focus(); };
  }, [locked]);
  return (
    <div className="ui-scrim" onMouseDown={e => { if (!locked && e.target === e.currentTarget) onClose(); }}>
      <div className={`ui-dialog ha-dialog${wide ? " ha-dialog-wide" : ""}`} role="dialog" aria-modal="true" aria-labelledby={id} ref={panel} tabIndex={-1}>
        <h2 id={id} className="ui-h2">{title}</h2>
        {children}
      </div>
    </div>
  );
}

function Field({ label, hint, children }) {
  const id = useId();
  return (
    <div className="ha-field">
      <label htmlFor={id}>{label}</label>
      {children(id)}
      {hint && <p className="ha-hint">{hint}</p>}
    </div>
  );
}

function CopyButton({ text, label }) {
  const [done, setDone] = useState(null);
  const timer = useRef(null);
  useEffect(() => () => clearTimeout(timer.current), []);
  const go = async () => {
    const ok = await copyText(text);
    setDone(ok ? "Copied" : "Copy failed: select the text and copy it by hand");
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setDone(null), 3000);
  };
  return (
    <span className="ha-copy">
      <button type="button" className="ui-btn" onClick={go}>{label}</button>
      <span role="status" className="ha-copy-msg">{done}</span>
    </span>
  );
}

// ── Add host ────────────────────────────────────────────────────────────────────────────────────────────────────────
function AddHostDialog({ onClose, onCreated, defaultAccountId }) {
  const [accounts, setAccounts] = useState(null);
  const [accountsError, setAccountsError] = useState(null);
  const [form, setForm] = useState({ accountId: defaultAccountId ? String(defaultAccountId) : "", label: "", maxUses: "1", expiresHours: "24" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [created, setCreated] = useState(null);   // { key, id, account } -- the ONLY place the key is held

  useEffect(() => {
    let live = true;
    getLiveAccounts()
      .then(d => { if (live) setAccounts(Array.isArray(d) ? d : []); })
      .catch(e => { if (live) setAccountsError(errText(e)); });
    return () => { live = false; };
  }, []);

  const set = k => e => setForm(f => ({ ...f, [k]: e.target.value }));
  const submit = async e => {
    e.preventDefault();
    const { body, error: bad } = buildKeyRequest(form);
    if (bad) { setError(bad); return; }
    setBusy(true); setError(null);
    try {
      const r = await createEnrollmentKey(body);
      const acct = (accounts || []).find(a => String(a.id) === String(body.aws_account_id));
      setCreated({ key: r.key, id: r.id, account: acct?.account_name || `Account ${body.aws_account_id}` });
      onCreated?.();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  };

  if (created) {
    const cmd = buildInstallCommand({ hubUrl: window.location.origin, key: created.key });
    return (
      <Modal title="Enrollment key created" onClose={onClose} locked wide>
        <div className="ha-warn" role="alert">
          <strong>This key is shown once.</strong> It is stored only as a hash, so it cannot be shown again. Copy it now.
          Closing this window with Esc or a click outside is disabled so the key is not lost by accident.
        </div>
        <p className="ha-hint">For {created.account}. Anyone holding this key can enroll a host into that account until it is used up, expires or is revoked.</p>
        <Field label="Enrollment key">
          {id => <input id={id} className="ui-input ha-mono ha-full" readOnly value={created.key} onFocus={e => e.target.select()} autoComplete="off" spellCheck={false} />}
        </Field>
        <CopyButton text={created.key} label="Copy key" />
        <div className="ha-field">
          <span className="ha-label">Install command (run on the server)</span>
          <p className="ha-hint">
            Linux with systemd, run by a user who can use <code>sudo</code>. The installer and agent are downloaded from this hub
            and checked against their SHA-256 sums before anything is installed. macOS and Windows: see Help, “Host agents”.
          </p>
          <pre className="ha-code" tabIndex={0} aria-label="Install command">{cmd.command}</pre>
          <p className="ha-hint">The command contains the key: do not paste it into tickets or chat.</p>
        </div>
        <CopyButton text={cmd.command} label="Copy command" />
        <div className="ui-dialog-actions ha-actions">
          <button type="button" className="ui-btn ui-btn-primary" onClick={onClose}>I have saved the key: close</button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="Add host" onClose={onClose}>
      <form onSubmit={submit} className="ha-form">
        <p className="ha-hint">Creates a one-time enrollment key. The key lets a server install the agent and register itself in the chosen account.</p>
        <Field label="Account">
          {id => (
            <select id={id} className="ui-input ha-full" value={form.accountId} onChange={set("accountId")} required disabled={!accounts}>
              <option value="">{accounts ? "Choose an account" : accountsError ? "Accounts unavailable" : "Loading accounts…"}</option>
              {(accounts || []).map(a => <option key={a.id} value={a.id}>{a.account_name}</option>)}
            </select>
          )}
        </Field>
        {accountsError && <p className="ha-error" role="alert">Could not load accounts: {accountsError}</p>}
        <Field label="Label (optional)" hint="Shown in the keys list, for example “web fleet”.">
          {id => <input id={id} className="ui-input ha-full" maxLength={100} value={form.label} onChange={set("label")} autoComplete="off" />}
        </Field>
        <div className="ha-row">
          <Field label="Max uses" hint="How many hosts may enroll with it (1–1000).">
            {id => <input id={id} className="ui-input ha-full" type="number" min={1} max={1000} step={1} value={form.maxUses} onChange={set("maxUses")} required />}
          </Field>
          <Field label="Expires after (hours)" hint="1–8760.">
            {id => <input id={id} className="ui-input ha-full" type="number" min={1} max={8760} step={1} value={form.expiresHours} onChange={set("expiresHours")} required />}
          </Field>
        </div>
        {error && <p className="ha-error" role="alert">{error}</p>}
        <div className="ui-dialog-actions ha-actions">
          <button type="button" className="ui-btn" onClick={onClose}>Cancel</button>
          <button type="submit" className="ui-btn ui-btn-primary" disabled={busy || !accounts}>{busy ? "Creating…" : "Create key"}</button>
        </div>
      </form>
    </Modal>
  );
}

// ── Edit watched services + interval ────────────────────────────────────────────────────────────────────────────────
function EditConfigDialog({ host, onClose, onSaved }) {
  const [intervalText, setIntervalText] = useState("");
  const [servicesText, setServicesText] = useState("");
  const [clearServices, setClearServices] = useState(false);
  const [current, setCurrent] = useState(null);          // { interval_seconds, watch_services } once loaded; null = unknown
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  // Pre-fill from the server so the form shows what is configured (and a save only sends what the person changed).
  useEffect(() => {
    let live = true;
    getHostAgentServices(host.id)
      .then(d => {
        if (!live) return;
        const def = configFormDefaults(d);
        if (def) { setCurrent(currentConfigOf(d)); setIntervalText(def.intervalText); setServicesText(def.servicesText); }
        setLoading(false);
      })
      .catch(e => { if (live) { setLoadError(errText(e)); setLoading(false); } });
    return () => { live = false; };
  }, [host.id]);

  const submit = async e => {
    e.preventDefault();
    const { body, error: bad } = buildConfigPatch({ intervalText, servicesText, clearServices }, current);
    if (bad) { setError(bad); return; }
    setBusy(true); setError(null);
    try {
      const r = await patchHostAgentConfig(host.id, body);
      onSaved(`Config for ${host.hostname} saved (version ${r.config_version}). The agent picks it up on its next report.`);
    } catch (err) {
      setError(err?.status === 409 ? "This host is revoked and can no longer be configured." : errText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={`Configure ${host.hostname}`} onClose={onClose}>
      <form onSubmit={submit} className="ha-form">
        {loading && <div aria-busy="true"><Skeleton h={14} /></div>}
        {!loading && current && (
          <p className="ha-hint">Showing the current configuration. Only what you change is saved; the agent picks it up on its next report.</p>
        )}
        {!loading && !current && (
          <p className="ha-hint">
            {loadError ? `The current values could not be loaded (${loadError}), so nothing is pre-filled. ` : "The current values are not available, so nothing is pre-filled. "}
            Only the fields you fill in are changed.
          </p>
        )}
        <Field label="Report interval (seconds)" hint={`${current ? "" : "Leave blank to keep the current interval. "}${INTERVAL_MIN}–${INTERVAL_MAX}.`}>
          {id => <input id={id} className="ui-input ha-full" inputMode="numeric" value={intervalText} onChange={e => setIntervalText(e.target.value)} placeholder="unchanged" autoComplete="off" disabled={loading} />}
        </Field>
        <Field label="Watched services" hint={current
          ? "Service names (systemd unit, launchd label or Windows service name) separated by commas, spaces or new lines. This is the whole list: remove a name to stop watching it. Each name's state appears in the host's Services panel after the agent's next report."
          : "Service names separated by commas, spaces or new lines. Replaces the whole list. Leave blank to keep the current list."}>
          {id => <textarea id={id} className="ui-input ha-full ha-textarea" rows={4} value={servicesText} onChange={e => setServicesText(e.target.value)} disabled={clearServices || loading} placeholder="nginx, sshd" spellCheck={false} />}
        </Field>
        <label className="ha-check">
          <input type="checkbox" checked={clearServices} onChange={e => setClearServices(e.target.checked)} disabled={loading} />
          Stop watching all services (empties the list)
        </label>
        {error && <p className="ha-error" role="alert">{error}</p>}
        <div className="ui-dialog-actions ha-actions">
          <button type="button" className="ui-btn" onClick={onClose}>Cancel</button>
          <button type="submit" className="ui-btn ui-btn-primary" disabled={busy || loading}>{busy ? "Saving…" : "Save"}</button>
        </div>
      </form>
    </Modal>
  );
}

// ── Enrollment keys list ────────────────────────────────────────────────────────────────────────────────────────────
function KeysDialog({ onClose, onNotice }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setRows(await listEnrollmentKeys()); setError(null); }
    catch (e) { setError(errText(e)); setRows(prev => prev ?? []); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const doRevoke = async () => {
    setBusy(true);
    try {
      await revokeEnrollmentKey(confirm.id);
      onNotice(`Enrollment key ${confirm.id} revoked.`);
      setConfirm(null);
      await load();
    } catch (e) {
      setError(errText(e)); setConfirm(null);
    } finally { setBusy(false); }
  };

  const columns = [
    { key: "id", header: "ID", num: true },
    { key: "account", header: "Account", render: r => r.account || `Account ${r.aws_account_id}` },
    { key: "label", header: "Label", render: r => r.label || "—" },
    { key: "uses", header: "Used", num: true, render: r => `${r.used_count} / ${r.max_uses}` },
    { key: "expires_at", header: "Expires (UTC)", render: r => utcStamp(r.expires_at) },
    { key: "status", header: "Status", render: r => <Badge tone={KEY_STATUS_TONE[r.status] || "mute"}>{r.status}</Badge> },
    { key: "created_by", header: "Created by", render: r => r.created_by || "—" },
    { key: "act", header: <span className="ha-sr">Actions</span>, render: r => r.status === "active"
      ? <button type="button" className="ui-btn" onClick={() => setConfirm(r)} aria-label={`Revoke enrollment key ${r.id}`}>Revoke</button> : null },
  ];

  return (
    <>
      <Modal title="Enrollment keys" onClose={onClose} wide>
        <p className="ha-hint">Keys are never shown again after creation, only their status.</p>
        {error && <p className="ha-error" role="alert">{error}</p>}
        <DataTable columns={columns} rows={rows || []} rowKey={r => r.id} loading={rows == null} dense
          empty={<EmptyState title="No enrollment keys" body="Use “Add host” to create one." />} />
        <div className="ui-dialog-actions ha-actions">
          <button type="button" className="ui-btn" onClick={onClose}>Close</button>
        </div>
      </Modal>
      <ConfirmDialog open={!!confirm} danger busy={busy} title="Revoke enrollment key?" confirmLabel="Revoke key"
        body={confirm && <>Key {confirm.id}{confirm.label ? ` (${confirm.label})` : ""} will stop accepting new hosts. Hosts already enrolled with it keep working.</>}
        onConfirm={doRevoke} onCancel={() => setConfirm(null)} />
    </>
  );
}

// ── Host detail drawer ──────────────────────────────────────────────────────────────────────────────────────────────
function HostDrawer({ host, onClose, canManage, onEdit, onRevoke }) {
  const [alerts, setAlerts] = useState(null);
  const [alertsError, setAlertsError] = useState(null);
  const [metrics, setMetrics] = useState(null);             // null = first load in flight; else the API payload
  const [metricsError, setMetricsError] = useState(null);
  const state = stateOf(host);
  const hostId = host.id;

  // Current metrics: stored readings only (GET /api/host-agents/{id}/metrics is a DB read). Reloaded every REFRESH_MS
  // while this drawer is open and the tab is visible; a failed refresh keeps the last good table and says so.
  const [reloadN, setReloadN] = useState(0);
  useEffect(() => {
    let live = true;
    getHostAgentMetrics(hostId)
      .then(data => { if (live) { setMetrics(data && typeof data === "object" ? data : { metrics: [] }); setMetricsError(null); } })
      .catch(e => {
        if (!live) return;
        setMetricsError(e?.status === 404 ? "This host or the host agent feature is no longer available." : errText(e));
        setMetrics(prev => prev ?? { metrics: [] });
      });
    return () => { live = false; };
  }, [hostId, reloadN]);
  useAutoRefresh(() => setReloadN(n => n + 1), REFRESH_MS);
  const metricRows = useMemo(() => hostMetricRows(metrics), [metrics]);
  const metricsNote = hostMetricsNote(metrics, host);

  // Watched services: stored states only (GET /api/host-agents/{id}/services), reloaded with the metrics.
  const [svc, setSvc] = useState(null);                    // null = first load in flight
  const [svcError, setSvcError] = useState(null);
  useEffect(() => {
    let live = true;
    getHostAgentServices(hostId)
      .then(d => { if (live) { setSvc(d && typeof d === "object" ? d : { services: [] }); setSvcError(null); } })
      .catch(e => {
        if (!live) return;
        setSvcError(e?.status === 404 ? "This host or the host agent feature is no longer available." : errText(e));
        setSvc(prev => prev ?? { services: [] });
      });
    return () => { live = false; };
  }, [hostId, reloadN]);
  const svcRows = useMemo(() => serviceRows(svc), [svc]);
  const svcSummary = serviceSummaryText(svc);
  const svcTone = serviceWorstTone(svc);
  const svcNote = servicesNote(svc, host);
  const watching = Array.isArray(svc?.watch_services) ? svc.watch_services.length : 0;

  useEffect(() => {
    let live = true;
    // Existing alerts API (DB read, no cloud calls). One search per identifier, then exact-match filtered client side.
    const terms = [...new Set([host.hostname, host.instance_id].filter(Boolean))];
    Promise.all(terms.map(t => getAlertsForResource(host.aws_account_id, t)))
      .then(lists => { if (live) { setAlerts(firingAlertsForHost(mergeAlertLists(lists.map(l => (Array.isArray(l) ? l : []))), host)); setAlertsError(null); } })
      .catch(e => { if (live) { setAlertsError(errText(e)); setAlerts([]); } });
    return () => { live = false; };
  }, [host]);

  const rows = [
    ["Account", host.account || `Account ${host.aws_account_id}`],
    ["Instance", host.instance_id || "—"],
    ["OS / arch", [host.os, host.arch].filter(Boolean).join(" / ") || "—"],
    ["Agent version", host.agent_version || "—"],
    ["Last seen", `${relativeTime(host.last_seen_at)} · ${utcStamp(host.last_seen_at)}`],
  ];

  return (
    <ResourceDrawer onClose={onClose} label={`Host ${host.hostname}`}>
      <div className="ha-drawer">
        <div className="ha-drawer-head">
          <div>
            <h2 className="ui-h1 ha-mono">{host.hostname}</h2>
            <Badge tone={STATE_TONE[state]}>{STATE_LABEL[state]}</Badge>
          </div>
          <button type="button" className="ui-btn" onClick={onClose}>Close</button>
        </div>
        <dl className="ha-dl">
          {rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
        </dl>
        {canManage && state !== "revoked" && (
          <div className="ha-drawer-actions">
            <button type="button" className="ui-btn" onClick={() => onEdit(host)}>Configure</button>
            <button type="button" className="ui-btn ui-btn-danger" onClick={() => onRevoke(host)}>Revoke host</button>
          </div>
        )}
        <Suspense fallback={<Panel title="Metric history"><div aria-busy="true"><Skeleton h={120} /></div></Panel>}>
          <HostMetricCharts host={host} />
        </Suspense>
        <Panel title="Current metrics" subtitle="Latest reading the agent delivered for each metric (not live, nothing estimated)">
          {metrics == null && <div aria-busy="true"><Skeleton h={14} /></div>}
          {metricsError && <p className="ha-error" role="alert">Could not load metrics: {metricsError}</p>}
          {metrics != null && metricRows.length === 0 && !metricsError && (
            <EmptyState title="No readings yet"
              body="This host has not delivered any metric readings, so nothing is shown." />
          )}
          {metricRows.length > 0 && (
            <div className="ha-metrics-wrap">
              <table className="ha-metrics">
                <caption className="ha-sr">Current metrics for {host.hostname}</caption>
                <thead><tr><th scope="col">Metric</th><th scope="col" className="ha-num">Value</th><th scope="col">Updated</th></tr></thead>
                <tbody>
                  {metricRows.map(m => (
                    <tr key={m.key}>
                      <th scope="row">{m.title}</th>
                      <td className="ha-num ha-mono">{m.value}</td>
                      <td title={utcStamp(m.timestamp)}>{relativeTime(m.timestamp)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {metricsNote && <p className="ha-hint">{metricsNote}</p>}
        </Panel>
        <Panel title="Services"
               subtitle="State of each watched service from the agent's last report (not live). Change the list with Configure."
               actions={svcTone ? <Badge tone={svcTone}>{svcSummary}</Badge> : undefined}>
          {svc == null && <div aria-busy="true"><Skeleton h={14} /></div>}
          {svcError && <p className="ha-error" role="alert">Could not load services: {svcError}</p>}
          {svc != null && !svcError && watching === 0 && (
            <EmptyState title="No services are watched"
              body={canManage ? "Use Configure to list the services this host should report (for example nginx or sshd)." : "An administrator can choose which services this host reports."} />
          )}
          {svcRows.length > 0 && (
            <div className="ha-metrics-wrap">
              <table className="ha-metrics">
                <caption className="ha-sr">Watched services of {host.hostname}</caption>
                <thead><tr><th scope="col">Service</th><th scope="col">State</th><th scope="col">In this state for</th><th scope="col">Reported</th></tr></thead>
                <tbody>
                  {svcRows.map(r => (
                    <tr key={r.key}>
                      <th scope="row" className="ha-mono">{r.name}</th>
                      <td><Badge tone={r.tone}>{r.label}</Badge></td>
                      <td title={utcStamp(r.since)}>{r.sinceText}</td>
                      <td title={utcStamp(r.observedAt)}>{relativeTime(r.observedAt)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {svcNote && <p className="ha-hint">{svcNote}</p>}
        </Panel>
        <Panel title="Active alerts" subtitle="Firing alerts naming this host or its instance id">
          {alerts == null && <div aria-busy="true"><Skeleton h={14} /></div>}
          {alertsError && <p className="ha-error" role="alert">Could not load alerts: {alertsError}</p>}
          {alerts && !alertsError && alerts.length === 0 && <EmptyState title="No active alerts for this host" />}
          {alerts && alerts.length > 0 && (
            <ul className="ha-alerts">
              {alerts.map(a => (
                <li key={a.id}>
                  <Badge tone={String(a.severity).toUpperCase() === "CRITICAL" ? "crit" : String(a.severity).toUpperCase() === "WARNING" ? "warn" : "info"}>{a.severity}</Badge>
                  <span className="ha-alert-name">{a.metric_name || "Alert"}</span>
                  <span className="ha-hint">{a.resource_name || a.resource} · {utcStamp(a.triggered_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </ResourceDrawer>
  );
}

// ── page ────────────────────────────────────────────────────────────────────────────────────────────────────────────
export default function HostAgents() {
  const { hasPermission } = useAuth();
  const canManage = hasPermission("hostagents.manage");
  const [rows, setRows] = useState(null);                 // null = first load in flight
  const [error, setError] = useState(null);               // { kind: "off"|"forbidden"|"other", message }
  const [lastOk, setLastOk] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [state, setState] = useState("all");
  const [accountId, setAccountId] = useState("all");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState(null);
  const [addOpen, setAddOpen] = useState(false);
  const [keysOpen, setKeysOpen] = useState(false);
  const [editHost, setEditHost] = useState(null);
  const [revokeHost, setRevokeHost] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const [, tick] = useState(0);

  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      const data = await listHostAgents();
      setRows(Array.isArray(data) ? data : []);
      setError(null);
      setLastOk(Date.now());
    } catch (e) {
      if (e?.status === 404) setError({ kind: "off", message: "Host agents are not enabled on this server." });
      else if (e?.status === 403) setError({ kind: "forbidden", message: "You do not have permission to view host agents." });
      else setError({ kind: "other", message: errText(e) });
      setRows(prev => prev ?? []);
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  useAutoRefresh(load, REFRESH_MS);
  // keep "5m ago" honest between refreshes
  useEffect(() => { const t = setInterval(() => tick(n => n + 1), 15000); return () => clearInterval(t); }, []);

  const all = useMemo(() => rows || [], [rows]);
  const counts = useMemo(() => stateCounts(all), [all]);
  const accounts = useMemo(() => accountOptions(all), [all]);
  const shown = useMemo(() => filterHosts(all, { accountId, state, query }), [all, accountId, state, query]);
  const selected = all.find(r => r.id === selectedId) || null;
  const hardOff = error && (error.kind === "off" || error.kind === "forbidden");

  const doRevoke = async () => {
    setBusy(true);
    try {
      await revokeHostAgent(revokeHost.id);
      setNotice(`Host ${revokeHost.hostname} revoked. Its tokens no longer work.`);
      setRevokeHost(null);
      await load();
    } catch (e) {
      setError({ kind: "other", message: errText(e) });
      setRevokeHost(null);
    } finally { setBusy(false); }
  };

  const chips = [
    { key: "all", label: "All", count: counts.all },
    { key: "online", label: STATE_LABEL.online, tone: "ok", count: counts.online },
    { key: "stale", label: STATE_LABEL.stale, tone: "warn", count: counts.stale },
    { key: "offline", label: STATE_LABEL.offline, tone: "crit", count: counts.offline },
    { key: "revoked", label: STATE_LABEL.revoked, tone: "mute", count: counts.revoked },
  ];

  const stop = e => e.stopPropagation();
  const columns = [
    { key: "hostname", header: "Hostname", sort: r => r.hostname, render: r => <span className="ha-mono">{r.hostname}</span> },
    { key: "account", header: "Account", sort: r => r.account || "", render: r => r.account || `Account ${r.aws_account_id}` },
    { key: "instance_id", header: "Instance", render: r => <span className="ha-mono">{r.instance_id || "—"}</span> },
    { key: "os", header: "OS / arch", render: r => [r.os, r.arch].filter(Boolean).join(" / ") || "—" },
    { key: "agent_version", header: "Agent", sort: r => r.agent_version || "", render: r => r.agent_version || "—" },
    { key: "state", header: "State", sort: r => stateOf(r), render: r => <Badge tone={STATE_TONE[stateOf(r)]}>{STATE_LABEL[stateOf(r)]}</Badge> },
    { key: "last_seen_at", header: "Last seen", sort: r => r.last_seen_at || "", render: r => (
      <span className="ha-seen"><span>{relativeTime(r.last_seen_at)}</span><span className="ha-hint">{utcStamp(r.last_seen_at)}</span></span>
    ) },
    ...(canManage ? [{ key: "actions", header: <span className="ha-sr">Actions</span>, render: r => (
      <span className="ha-rowactions" onClick={stop} onKeyDown={stop}>
        <button type="button" className="ui-btn" disabled={stateOf(r) === "revoked"} onClick={() => setEditHost(r)} aria-label={`Configure ${r.hostname}`}>Configure</button>
        <button type="button" className="ui-btn" disabled={stateOf(r) === "revoked"} onClick={() => setRevokeHost(r)} aria-label={`Revoke ${r.hostname}`}>Revoke</button>
      </span>
    ) }] : []),
  ];

  const emptyBody = all.length === 0
    ? <EmptyState title="No host agents yet" body={canManage ? "Create an enrollment key with “Add host”, then run the install command on a server." : "Hosts appear here once an administrator enrolls them."}
        action={canManage ? <button type="button" className="ui-btn ui-btn-primary" onClick={() => setAddOpen(true)}>Add host</button> : null} />
    : <EmptyState title="No hosts match these filters" action={<button type="button" className="ui-btn" onClick={() => { setState("all"); setAccountId("all"); setQuery(""); }}>Clear filters</button>} />;

  return (
    <div className="ha-page">
      <PageHeader title="Host Agents" subtitle="Servers that push their own metrics to the hub."
        meta={lastOk ? `Updated ${relativeTime(new Date(lastOk).toISOString())} · refreshes every ${REFRESH_MS / 1000}s` : undefined}
        actions={<>
          <button type="button" className="ui-btn" onClick={load} disabled={refreshing || hardOff}>{refreshing ? "Refreshing…" : "Refresh"}</button>
          {canManage && <button type="button" className="ui-btn" onClick={() => setKeysOpen(true)} disabled={hardOff}>Enrollment keys</button>}
          {canManage && <button type="button" className="ui-btn ui-btn-primary" onClick={() => setAddOpen(true)} disabled={hardOff}>Add host</button>}
        </>} />

      <div role="status" aria-live="polite" className={notice ? "ha-notice" : "ha-sr"}>
        {notice}{notice && <button type="button" className="ha-link" onClick={() => setNotice(null)}>Dismiss</button>}
      </div>

      {error && (
        <div className={error.kind === "other" ? "ha-error-box" : "ha-info-box"} role="alert">
          {error.message}
          {error.kind === "other" && <button type="button" className="ha-link" onClick={load}>Retry</button>}
        </div>
      )}

      {hardOff ? (
        <Panel><EmptyState title={error.kind === "off" ? "Host agents are turned off" : "No access"} body={error.kind === "off" ? "An administrator can enable the feature on the server." : error.message} /></Panel>
      ) : (
        <>
          <FilterBar chips={chips} active={state} onChange={setState} search={query} onSearch={setQuery}
            placeholder="Search hostname, instance, OS…"
            right={
              <label className="ha-acct">
                <span className="ha-sr">Account</span>
                <select className="ui-input" value={accountId} onChange={e => setAccountId(e.target.value)} aria-label="Filter by account">
                  <option value="all">All accounts</option>
                  {accounts.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
                </select>
              </label>
            } />
          <Panel flush>
            <DataTable columns={columns} rows={shown} rowKey={r => r.id} loading={rows == null} onRowClick={r => setSelectedId(r.id)}
              initialSort={{ key: "hostname", dir: "asc" }} empty={emptyBody} />
          </Panel>
        </>
      )}

      {selected && (
        <HostDrawer key={selected.id} host={selected} onClose={() => setSelectedId(null)} canManage={canManage}
          onEdit={h => setEditHost(h)} onRevoke={h => setRevokeHost(h)} />
      )}
      {addOpen && canManage && <AddHostDialog defaultAccountId={accountId !== "all" ? accountId : null} onClose={() => setAddOpen(false)} onCreated={load} />}
      {keysOpen && canManage && <KeysDialog onClose={() => setKeysOpen(false)} onNotice={setNotice} />}
      {editHost && canManage && <EditConfigDialog host={editHost} onClose={() => setEditHost(null)} onSaved={msg => { setEditHost(null); setNotice(msg); load(); }} />}
      <ConfirmDialog open={!!revokeHost && canManage} danger busy={busy} title="Revoke this host?" confirmLabel="Revoke host"
        body={revokeHost && <>Revoking <strong>{revokeHost.hostname}</strong> invalidates all of its tokens: the agent stops being accepted and must be enrolled again with a new key. This cannot be undone.</>}
        onConfirm={doRevoke} onCancel={() => setRevokeHost(null)} />
    </div>
  );
}
