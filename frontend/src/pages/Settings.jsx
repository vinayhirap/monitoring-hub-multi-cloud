// monitoring-hub/frontend/src/pages/Settings.jsx
import { useState, useEffect, useCallback } from "react";
import MetricSelector from "../components/MetricSelector";
import {
  getAccountMetrics, saveAccountMetrics,
  applyDefaultTemplate, discoverNamespaceMetrics, downloadYaceConfig,
} from "../api/api";
import "./Settings.css";
import {
  ServerIcon, SaveIcon, ScaleIcon, DatabaseIcon, BucketIcon, SettingsIcon,
  MailIcon, InfoIcon, ArrowRightIcon, BarChartIcon, TrashIcon, AlertTriangleIcon,
  RedDotIcon, CheckIcon, CompassIcon, RotateCcwIcon, DownloadIcon, LockIcon,
  ShieldIcon, KeyIcon, ZapIcon, PackageIcon, EyeIcon, SlashIcon,
  ClipboardIcon, XIcon,
} from "../components/icons";

const BASE = "";

const IDEAL = {
  CPUUtilization:           { warn:70,  crit:85,  unit:"%",   desc:"Sustained >85% = under-provisioned" },
  StatusCheckFailed:        { warn:0,   crit:1,   unit:"cnt", desc:">0 = instance health failure" },
  VolumeQueueLength:        { warn:1,   crit:5,   unit:"ops", desc:">1 sustained = I/O bottleneck" },
  BurstBalance:             { warn:30,  crit:10,  unit:"%",   desc:"<10% = EBS throughput degraded" },
  HTTPCode_Target_5XX_Count:{ warn:5,   crit:20,  unit:"cnt", desc:"Server errors hitting users" },
  "5XXError":               { warn:5,   crit:20,  unit:"cnt", desc:"API Gateway 5XX errors" },
  HealthyHostCount:         { warn:0,   crit:0,   unit:"cnt", desc:"<1 = no healthy targets" },
  FreeStorageSpace:         { warn:10,  crit:5,   unit:"GB",  desc:"<5GB = DB writes will halt" },
  DatabaseConnections:      { warn:80,  crit:95,  unit:"%",   desc:"% of max_connections limit" },
  Errors:                   { warn:1,   crit:5,   unit:"%",   desc:"Lambda invocation error rate" },
  Duration:                 { warn:3000,crit:8000,unit:"ms",  desc:"Approaching function timeout" },
  NetworkIn:                { warn:80,  crit:100, unit:"MB/s",desc:"Bandwidth saturation" },
  NetworkOut:               { warn:80,  crit:100, unit:"MB/s",desc:"Outbound saturation" },
};

const INVERTED_WARN = new Set(["HealthyHostCount", "BurstBalance", "FreeStorageSpace"]);
const ONE_BOUNDARY  = new Set(["HealthyHostCount", "StatusCheckFailed"]);

const SVC_ICON  = { ec2:ServerIcon, ebs:SaveIcon, alb:ScaleIcon, rds:DatabaseIcon, lambda:"λ", s3:BucketIcon };
const SVC_COLOR = { ec2:"#2bb3ac", ebs:"#38bdf8", alb:"#f472b6", rds:"#7c6ee0", lambda:"#22c55e", s3:"#fbbf24" };

export default function Settings() {
  const [accounts,    setAccounts]    = useState([]);
  const [accountId,   setAccountId]   = useState(null);
  const [thresholds,  setThresholds]  = useState([]);
  const [loading,     setLoading]     = useState(true);
  const [saving,      setSaving]      = useState(null);
  const [saveMsg,     setSaveMsg]     = useState({});
  const [checkResult, setCheckResult] = useState(null);
  const [checking,    setChecking]    = useState(false);
  const [emailOn,     setEmailOn]     = useState(false);
  const [email,       setEmail]       = useState("");
  const [webhook,     setWebhook]     = useState("");

  const [metricCatalog,   setMetricCatalog]   = useState([]);
  const [metricSelected,  setMetricSelected]  = useState(new Set());
  const [metricsLoading,  setMetricsLoading]  = useState(true);
  const [metricsDirty,    setMetricsDirty]    = useState(false);
  const [metricsSaving,   setMetricsSaving]   = useState(false);
  const [metricsSaveMsg,  setMetricsSaveMsg]  = useState(null);

  const loadAccountMetrics = useCallback(async () => {
    if (!accountId) return;
    setMetricsLoading(true);
    try {
      const data = await getAccountMetrics(accountId);
      const groups = Array.isArray(data) ? data : [];
      setMetricCatalog(groups);
      const enabled = new Set();
      groups.forEach(g => g.metrics.forEach(m => { if (m.enabled) enabled.add(m.id); }));
      setMetricSelected(enabled);
      setMetricsDirty(false);
    } catch (e) {
      console.error("Load account metrics:", e);
    } finally {
      setMetricsLoading(false);
    }
  }, [accountId]);

  useEffect(() => { loadAccountMetrics(); }, [loadAccountMetrics]);

  function onMetricSelectionChange(nextSet) {
    setMetricSelected(nextSet);
    setMetricsDirty(true);
    setMetricsSaveMsg(null);
  }

  async function saveMetricSelection() {
    if (!accountId) return;
    setMetricsSaving(true);
    try {
      await saveAccountMetrics(accountId, Array.from(metricSelected));
      setMetricsDirty(false);
      setMetricsSaveMsg("Saved");
      setTimeout(() => setMetricsSaveMsg(null), 2500);
    } catch (e) {
      setMetricsSaveMsg("Save failed");
    } finally {
      setMetricsSaving(false);
    }
  }

  async function resetToDefaultMetrics() {
    if (!accountId) return;
    setMetricsSaving(true);
    try {
      await applyDefaultTemplate(accountId);
      await loadAccountMetrics();
      setMetricsSaveMsg("Reset to recommended defaults");
      setTimeout(() => setMetricsSaveMsg(null), 2500);
    } finally {
      setMetricsSaving(false);
    }
  }

  async function handleDiscoverNamespace(namespace) {
    if (!accountId) return;
    await discoverNamespaceMetrics(accountId, namespace);
    await loadAccountMetrics();
  }

  async function handleDownloadYaceConfig(tier) {
    if (!accountId) return;
    try {
      // No manual auth header needed — the global fetch patch
      // (src/api/httpDefaults.js) already attaches the session cookie.
      const res = await fetch(downloadYaceConfig(accountId, tier));
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const suffix = tier ? `-${tier}` : "";
      a.download = `yace-config-${selectedAccount?.account_name || accountId}${suffix}.yml`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setMetricsSaveMsg(`YACE config${tier ? ` (${tier})` : ""}: ${e.message}`);
      setTimeout(() => setMetricsSaveMsg(null), 4000);
    }
  }

  // Load all accounts for the selector
  useEffect(() => {
    fetch(`${BASE}/api/admin/accounts`)
      .then(r => r.json())
      .then(data => {
        const list = Array.isArray(data) ? data : [];
        setAccounts(list);
        if (list.length > 0) setAccountId(list[0].id);
      })
      .catch(console.error);
  }, []);

  const load = useCallback(async () => {
    if (!accountId) return;
    setLoading(true);
    try {
      const t = await fetch(`${BASE}/api/settings/thresholds?account_id=${accountId}`).then(r => r.json());
      setThresholds(Array.isArray(t) ? t : []);
    } catch (e) {
      console.error("Settings load:", e);
    } finally {
      setLoading(false);
    }
  }, [accountId]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!loading && thresholds.length === 0 && accountId) {
      fetch(`${BASE}/api/settings/thresholds/seed?account_id=${accountId}`, { method: "POST" })
        .then(() => load())
        .catch(console.error);
    }
  }, [loading, thresholds.length, load, accountId]);

  async function saveThreshold(t) {
    setSaving(t.id);
    setSaveMsg(prev => ({ ...prev, [t.id]: null }));
    try {
      const res = await fetch(`${BASE}/api/settings/thresholds`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account_id:        accountId,
          metric_id:         t.metric_id,
          resource_type:     t.resource_type || t.service,
          warning_value:     parseFloat(t.warning_value),
          critical_value:    parseFloat(t.critical_value),
          comparison:        t.comparison || ">",
          evaluation_period: t.evaluation_period || 5,
          enabled:           t.enabled ? 1 : 0,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      setSaveMsg(prev => ({ ...prev, [t.id]: "ok" }));
      setTimeout(() => setSaveMsg(prev => ({ ...prev, [t.id]: null })), 3000);
    } catch (e) {
      setSaveMsg(prev => ({ ...prev, [t.id]: "err" }));
      console.error("Save failed:", e);
    } finally {
      setSaving(null);
    }
  }

  async function toggleThreshold(t) {
    const newEnabled = t.enabled ? 0 : 1;
    setThresholds(prev => prev.map(x => x.id === t.id ? { ...x, enabled: newEnabled } : x));
    try {
      const res = await fetch(`${BASE}/api/settings/thresholds/${t.id}/toggle`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: newEnabled }),
      });
      if (!res.ok) throw new Error();
    } catch {
      setThresholds(prev => prev.map(x => x.id === t.id ? { ...x, enabled: t.enabled } : x));
    }
  }

  function updateLocal(id, field, value) {
    setThresholds(prev => prev.map(t => t.id === id ? { ...t, [field]: value } : t));
  }

  async function runCheck() {
    if (!accountId) return;
    setChecking(true);
    try {
      const r = await fetch(`${BASE}/api/settings/check?account_id=${accountId}`).then(r => r.json());
      setCheckResult(r);
    } catch (e) {
      setCheckResult({ breaches: [], error: e.message });
    } finally {
      setChecking(false);
    }
  }

  async function clearAlerts() {
    if (!window.confirm("Clear all active alerts from DB?")) return;
    await fetch(`${BASE}/api/alerts/clear`, { method: "DELETE" }).catch(() => {});
    setCheckResult(null);
    alert("Alerts cleared.");
  }

  const grouped = thresholds.reduce((acc, t) => {
    const svc = t.service || t.resource_type || "other";
    if (!acc[svc]) acc[svc] = [];
    acc[svc].push(t);
    return acc;
  }, {});

  const selectedAccount = accounts.find(a => a.id === accountId);

  return (
    <div className="settings-page">
      <div className="page-header">
        <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: "-0.02em" }}>
          <span style={{display:"inline-flex",alignItems:"center",gap:8}}><SettingsIcon size={22}/> Settings</span> <span style={{ color: "var(--accent)" }}>& Thresholds</span>
        </h1>
        <p className="subtitle">Configure alert thresholds, notifications, and security for your NOC dashboard</p>
      </div>

      {/* Email Notifications */}
      <div className="settings-card">
        <div className="card-header">
          <div className="card-title-row">
            <span className="card-icon"><MailIcon size={16}/></span>
            <span className="card-title">EMAIL NOTIFICATIONS</span>
          </div>
          <label className="toggle">
            <input type="checkbox" checked={emailOn} onChange={e => setEmailOn(e.target.checked)} />
            <div className="toggle-track"><div className="toggle-thumb" /></div>
            <span className={`toggle-label ${emailOn ? "on" : "off"}`}>{emailOn ? "ON" : "OFF"}</span>
          </label>
        </div>
        <div className="email-fields">
          <div className="efield">
            <label>RECIPIENT EMAIL <span className="opt">*</span></label>
            <input type="email" placeholder="ops@yourcompany.com" value={email}
              onChange={e => setEmail(e.target.value)} disabled={!emailOn} />
          </div>
          <div className="efield">
            <label>WEBHOOK URL <span className="opt">(optional)</span></label>
            <input type="url" placeholder="https://hooks.zapier.com/…" value={webhook}
              onChange={e => setWebhook(e.target.value)} disabled={!emailOn} />
          </div>
        </div>
        <div className="delivery-notice">
          <InfoIcon size={13}/> <strong>3-tier delivery:</strong>{" "}
          <span className="tier">1</span> Backend /notify/email <ArrowRightIcon size={11}/>{" "}
          <span className="tier">2</span> Webhook (Zapier/Make) <ArrowRightIcon size={11}/>{" "}
          <span className="tier">3</span> Mail client fallback.
          Deduplication: same alert suppressed for 10 minutes.
        </div>
      </div>

      {/* Metric Thresholds */}
      <div className="settings-card">
        <div className="card-header">
          <div className="card-title-row">
            <span className="card-icon"><BarChartIcon size={16}/></span>
            <span className="card-title">METRIC THRESHOLDS</span>
            {/* Account selector — dynamic, no hardcoded ID shown */}
            {accounts.length > 0 && (
              <select
                value={accountId || ""}
                onChange={e => setAccountId(Number(e.target.value))}
                style={{
                  marginLeft: 10,
                  fontSize: 12,
                  fontFamily: "var(--font-mono)",
                  background: "var(--bg-card)",
                  border: "1px solid var(--border)",
                  color: "var(--text-primary)",
                  borderRadius: 6,
                  padding: "3px 8px",
                  cursor: "pointer",
                }}
              >
                {accounts.map(a => (
                  <option key={a.id} value={a.id}>
                    {a.account_name} — {a.default_region}
                  </option>
                ))}
              </select>
            )}
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn-check" onClick={runCheck} disabled={checking || !accountId}>
              {checking ? "⏳ Checking…" : "▶ Check Now"}
            </button>
            <button className="btn-clear" onClick={clearAlerts}><TrashIcon size={13}/> Clear Alerts</button>
          </div>
        </div>

        {/* Check result output */}
        {checkResult && (
          <div style={{
            margin: "12px 20px",
            padding: "12px 16px",
            background: (checkResult.breaches?.length > 0) ? "rgba(239,68,68,0.06)" : "rgba(34,197,94,0.06)",
            border: `1px solid ${(checkResult.breaches?.length > 0) ? "rgba(239,68,68,0.2)" : "rgba(34,197,94,0.2)"}`,
            borderRadius: 8,
            fontSize: 13,
          }}>
            {checkResult.error ? (
              <span style={{ color: "var(--red)", display:"inline-flex", alignItems:"center", gap:6 }}><AlertTriangleIcon size={13}/> Check failed: {checkResult.error}</span>
            ) : checkResult.breaches?.length > 0 ? (
              <>
                <div style={{ fontWeight: 700, color: "var(--red)", marginBottom: 8, display:"flex", alignItems:"center", gap:6 }}>
                  <RedDotIcon size={13}/> {checkResult.breaches.length} breach{checkResult.breaches.length !== 1 ? "es" : ""} detected
                </div>
                {checkResult.breaches.map((b, i) => (
                  <div key={i} style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginBottom: 3 }}>
                    <span style={{ color: b.severity === "CRITICAL" ? "var(--red)" : "var(--yellow)", marginRight: 6, display:"inline-flex" }}>
                      {b.severity === "CRITICAL" ? <RedDotIcon size={11}/> : <AlertTriangleIcon size={11}/>}
                    </span>
                    {b.service?.toUpperCase()} · {b.metric} · {b.resource} · value: {b.value} (threshold: {b.threshold})
                  </div>
                ))}
              </>
            ) : (
              <span style={{ color: "var(--green)", display:"inline-flex", alignItems:"center", gap:6 }}><CheckIcon size={13}/> All metrics within thresholds for {selectedAccount?.account_name}</span>
            )}
          </div>
        )}

        {loading ? (
          <div style={{ padding: 40, textAlign: "center", color: "var(--text-muted)" }}>Loading thresholds…</div>
        ) : thresholds.length === 0 ? (
          <div style={{ padding: 40, textAlign: "center", color: "var(--text-muted)" }}>
            No thresholds found.{" "}
            <button className="btn-check" onClick={() =>
              fetch(`${BASE}/api/settings/thresholds/seed?account_id=${accountId}`, { method: "POST" })
                .then(() => load())
            }>Seed defaults</button>
          </div>
        ) : (
          Object.entries(grouped).map(([svc, items]) => (
            <ServiceThresholdSection
              key={svc} svc={svc} items={items}
              onToggle={toggleThreshold}
              onUpdate={updateLocal}
              onSave={saveThreshold}
              saving={saving}
              saveMsg={saveMsg}
            />
          ))
        )}
      </div>

      {/* Metrics to Monitor */}
      <div className="settings-card">
        <div className="card-header">
          <div className="card-title-row">
            <span className="card-icon"><CompassIcon size={16}/></span>
            <span className="card-title">METRICS TO MONITOR</span>
            {selectedAccount && (
              <span style={{ marginLeft: 10, fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                {selectedAccount.account_name}
              </span>
            )}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {metricsSaveMsg && (
              <span style={{ fontSize: 12, color: metricsSaveMsg === "Save failed" ? "var(--red)" : "var(--green)" }}>
                {metricsSaveMsg}
              </span>
            )}
            <button className="btn-clear" onClick={resetToDefaultMetrics} disabled={metricsSaving || !accountId}>
              <RotateCcwIcon size={13}/> Reset to Recommended
            </button>
            <button className="btn-clear" onClick={() => handleDownloadYaceConfig("critical")} disabled={!accountId} title="60s poll — run as its own YACE instance">
              <DownloadIcon size={13}/> Critical (60s)
            </button>
            <button className="btn-clear" onClick={() => handleDownloadYaceConfig("standard")} disabled={!accountId} title="300s poll — run as its own YACE instance">
              <DownloadIcon size={13}/> Standard (300s)
            </button>
            <button className="btn-clear" onClick={() => handleDownloadYaceConfig("trend")} disabled={!accountId} title="900s poll — run as its own YACE instance">
              <DownloadIcon size={13}/> Trend (900s)
            </button>
            <button className="btn-check" onClick={saveMetricSelection} disabled={metricsSaving || !metricsDirty}>
              {metricsSaving ? "Saving…" : <span style={{display:"inline-flex",alignItems:"center",gap:6}}><SaveIcon size={13}/> Save Selection</span>}
            </button>
          </div>
        </div>

        {metricsLoading ? (
          <div style={{ padding: 40, textAlign: "center", color: "var(--text-muted)" }}>Loading metric catalog…</div>
        ) : !accountId ? (
          <div style={{ padding: 40, textAlign: "center", color: "var(--text-muted)" }}>Select an account above.</div>
        ) : (
          <div style={{ padding: "12px 20px 20px" }}>
            <p style={{ fontSize: 11, color: "var(--text-muted)", margin: "0 0 10px 0" }}>
              Each tier button generates a separate config.yml for that polling speed — deploy all three as
              separate YACE instances on this account/region's monitoring server (Critical/60s, Standard/300s,
              Trend/900s), each started with the matching <code>--scraping-interval</code> flag. This
              is what actually saves GetMetricData cost: one YACE process only has one global scrape interval,
              so splitting by tier is required for tiering to affect AWS call volume, not just query windows.
              Nothing is pushed automatically.
            </p>
            <MetricSelector
              catalog={metricCatalog}
              selectedIds={metricSelected}
              onChange={onMetricSelectionChange}
              onDiscover={handleDiscoverNamespace}
              provider={accounts.find(a => a.id === accountId)?.provider || "aws"}
            />
          </div>
        )}
      </div>

      {/* Security */}
      <div className="settings-card">
        <div className="card-header-simple">
          <div className="card-title-row">
            <span className="card-icon"><LockIcon size={16}/></span>
            <span className="card-title">SECURITY & VAPT</span>
          </div>
        </div>
        <div className="security-grid">
          {[
            { icon: ShieldIcon,    label: "CSRF Protection",   status: "Active — SameSite cookies enforced" },
            { icon: KeyIcon,       label: "JWT Auth",           status: "HS256 — tokens expire in 24h" },
            { icon: ZapIcon,       label: "Rate Limiting",      status: "100 req/min per IP" },
            { icon: LockIcon,      label: "HTTPS Enforcement",  status: "Redirect all HTTP → HTTPS" },
            { icon: PackageIcon,   label: "Input Sanitization", status: "Pydantic v2 schema validation" },
            { icon: EyeIcon,       label: "Audit Logging",       status: "All mutations logged to DB" },
            { icon: SlashIcon,     label: "SQL Injection Guard", status: "Parameterized queries only" },
            { icon: LockIcon,      label: "Secrets Management",  status: "Env vars — never in code" },
            { icon: ClipboardIcon, label: "Read-Only IAM",       status: "6 AWS ReadOnly policies — zero writes" },
          ].map(s => (
            <div key={s.label} className="sec-item">
              <span className="sec-icon"><s.icon size={16}/></span>
              <div className="sec-info">
                <div className="sec-label">{s.label}</div>
                <div className="sec-status">{s.status}</div>
              </div>
              <span className="sec-badge ok"><CheckIcon size={11}/></span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function ServiceThresholdSection({ svc, items, onToggle, onUpdate, onSave, saving, saveMsg }) {
  const Icon  = SVC_ICON[svc]  || BarChartIcon;
  const color = SVC_COLOR[svc] || "#2bb3ac";
  return (
    <div style={{ borderTop: "1px solid var(--border)" }}>
      <div style={{ padding: "10px 22px", background: "var(--bg-base)", display: "flex", alignItems: "center", gap: 8, borderBottom: "1px solid var(--border)" }}>
        <span style={{ fontSize: 16, display: "inline-flex" }}>{typeof Icon === "string" ? Icon : <Icon size={16} />}</span>
        <span style={{ fontSize: 12, fontWeight: 700, color, fontFamily: "var(--font-mono)", letterSpacing: "0.08em" }}>
          {svc.toUpperCase()}
        </span>
        <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: 4 }}>
          {items.length} metric{items.length !== 1 ? "s" : ""}
        </span>
      </div>
      <div className="thresholds-grid">
        {items.map(t => (
          <ThresholdItem
            key={t.id} t={t}
            onToggle={() => onToggle(t)}
            onUpdate={(field, val) => onUpdate(t.id, field, val)}
            onSave={() => onSave(t)}
            saving={saving === t.id}
            savedState={saveMsg[t.id]}
          />
        ))}
      </div>
    </div>
  );
}

function ThresholdItem({ t, onToggle, onUpdate, onSave, saving, savedState }) {
  const ideal      = IDEAL[t.metric_name] || {};
  const isBinary   = ONE_BOUNDARY.has(t.metric_name);
  const isInverted = INVERTED_WARN.has(t.metric_name);
  const unit       = ideal.unit || t.unit || "";
  const isPercent  = unit === "%";
  const maxVal     = isPercent ? 100 : undefined;
  const minVal     = 0;

  function clamp(val) {
    const n = parseFloat(val);
    if (isNaN(n)) return val;
    if (isPercent) return Math.min(100, Math.max(0, n));
    return Math.max(0, n);
  }

  return (
    <div className={`threshold-item ${t.enabled ? "" : "disabled"}`}>
      <div className="thresh-header">
        <div>
          <div className="thresh-label">{t.metric_name}</div>
          {ideal.desc && (
            <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2, lineHeight: 1.3 }}>
              {ideal.desc}
            </div>
          )}
        </div>
        <label className="toggle-sm" onClick={onToggle}>
          <div className={`sm-track ${t.enabled ? "on" : ""}`} />
          <span className={`sm-label ${t.enabled ? "enabled-txt" : "disabled-txt"}`}>
            {t.enabled ? "ON" : "OFF"}
          </span>
        </label>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <div className="thresh-input-row">
          <span style={{ fontSize: 10, color: "var(--green)", width: 16, display:"inline-flex" }}><CheckIcon size={12}/></span>
          <span className="thresh-hint">Healthy</span>
          <span style={{ fontFamily:"var(--font-mono)", fontSize:12, color:"var(--green)", padding:"2px 6px", background:"rgba(34,197,94,0.08)", borderRadius:4 }}>
            {isBinary ? "0" : isInverted ? `> ${ideal.warn ?? "—"}${ideal.unit ?? ""}` : `< ${ideal.warn ?? "—"}${ideal.unit ?? ""}`}
          </span>
          {ideal.warn != null && !isBinary && (
            <span style={{ fontSize:10, color:"var(--text-muted)", fontFamily:"var(--font-mono)", marginLeft:"auto" }}>
              ideal: {ideal.warn}{ideal.unit}
            </span>
          )}
        </div>

        {!isBinary && (
          <div className="thresh-input-row">
            <span style={{ fontSize: 10, color: "var(--yellow)", width: 16, display:"inline-flex" }}><AlertTriangleIcon size={12}/></span>
            <span className="thresh-hint">Warn</span>
            <input className="thresh-input" type="number" disabled={!t.enabled}
              value={t.warning_value} min={minVal} max={maxVal}
              onChange={e => onUpdate("warning_value", clamp(e.target.value))} />
            <span className="thresh-unit">{t.unit || ideal.unit || ""}</span>
            {ideal.warn != null && (
              <span style={{ fontSize:10, color:"var(--text-muted)", fontFamily:"var(--font-mono)" }}>
                ideal: {ideal.warn}{ideal.unit}
              </span>
            )}
          </div>
        )}

        {isBinary && (
          <div style={{ fontSize:10, color:"var(--text-muted)", background:"rgba(99,130,190,0.08)", border:"1px solid var(--border)", borderRadius:4, padding:"4px 8px", fontFamily:"var(--font-mono)" }}>
            2-STATE · NO WARNING · 0 = ok, ≥1 = critical
          </div>
        )}

        <div className="thresh-input-row">
          <span style={{ fontSize: 10, color: "var(--red)", width: 16, display:"inline-flex" }}><RedDotIcon size={12}/></span>
          <span className="thresh-hint">Crit</span>
          <input className="thresh-input" type="number" disabled={!t.enabled}
            value={t.critical_value} min={minVal} max={maxVal}
            onChange={e => onUpdate("critical_value", clamp(e.target.value))} />
          <span className="thresh-unit">{t.unit || ideal.unit || ""}</span>
          {ideal.crit != null && (
            <span style={{ fontSize:10, color:"var(--text-muted)", fontFamily:"var(--font-mono)" }}>
              ideal: {ideal.crit}{ideal.unit}
            </span>
          )}
        </div>
      </div>

      <button
        className="btn-save"
        style={{
          marginTop: 10, width: "100%", fontSize: 12, padding: "6px",
          background: savedState === "ok" ? "rgba(34,197,94,0.15)" : savedState === "err" ? "rgba(239,68,68,0.15)" : undefined,
          borderColor: savedState === "ok" ? "#22c55e" : savedState === "err" ? "#ef4444" : undefined,
          color: savedState === "ok" ? "#22c55e" : savedState === "err" ? "#ef4444" : undefined,
        }}
        onClick={onSave}
        disabled={saving || !t.enabled}
      >
        {saving ? "Saving…" : savedState === "ok"
          ? <span style={{display:"inline-flex",alignItems:"center",gap:5,justifyContent:"center"}}><CheckIcon size={12}/> Saved</span>
          : savedState === "err"
          ? <span style={{display:"inline-flex",alignItems:"center",gap:5,justifyContent:"center"}}><XIcon size={12}/> Failed</span>
          : <span style={{display:"inline-flex",alignItems:"center",gap:5,justifyContent:"center"}}><SaveIcon size={12}/> Save</span>}
      </button>
    </div>
  );
}
