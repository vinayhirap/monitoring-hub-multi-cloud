// monitoring-hub/frontend/src/pages/AccountOnboarding.jsx
import { useState, useEffect, useRef } from "react";
import MetricSelector from "../components/MetricSelector";
import { getMetricCatalog } from "../api/api";
import "./AccountOnboarding.css";

const BASE = "";

const ALL_REGIONS = [
  { id: "ap-south-1",     label: "ap-south-1 (Mumbai)" },
  { id: "",     label: " (Hyderabad)" },
  { id: "ap-southeast-1", label: "ap-southeast-1 (Singapore)" },
  { id: "ap-southeast-2", label: "ap-southeast-2 (Sydney)" },
  { id: "ap-northeast-1", label: "ap-northeast-1 (Tokyo)" },
  { id: "ap-northeast-2", label: "ap-northeast-2 (Seoul)" },
  { id: "ap-east-1",      label: "ap-east-1 (Hong Kong)" },
  { id: "us-east-1",      label: "us-east-1 (N. Virginia)" },
  { id: "us-east-2",      label: "us-east-2 (Ohio)" },
  { id: "us-west-1",      label: "us-west-1 (N. California)" },
  { id: "us-west-2",      label: "us-west-2 (Oregon)" },
  { id: "eu-central-1",   label: "eu-central-1 (Frankfurt)" },
  { id: "eu-west-1",      label: "eu-west-1 (Ireland)" },
  { id: "eu-west-2",      label: "eu-west-2 (London)" },
  { id: "eu-west-3",      label: "eu-west-3 (Paris)" },
  { id: "eu-north-1",     label: "eu-north-1 (Stockholm)" },
  { id: "me-south-1",     label: "me-south-1 (Bahrain)" },
  { id: "me-central-1",   label: "me-central-1 (UAE)" },
  { id: "af-south-1",     label: "af-south-1 (Cape Town)" },
  { id: "ca-central-1",   label: "ca-central-1 (Canada)" },
  { id: "sa-east-1",      label: "sa-east-1 (São Paulo)" },
];

const ENVIRONMENTS = ["Production", "Staging", "Development", "QA"];

const PROVIDER_TABS = [
  { id: "aws",   label: "AWS" },
  { id: "azure", label: "Azure" },
  { id: "gcp",   label: "GCP" },
];

const INITIAL_FORM = {
  account_name:   "",
  account_id:     "",
  primary_region: "",
  environment:    "Production",
  owner_team:     "",
  alias:          "",
  description:    "",
  iam_role_arn:   "",
  external_id:    "",
  access_key:     "",
  secret_key:     "",
  auth_method:    "iam_role",
};

function Field({ id, label, required, error, children }) {
  return (
    <div className={`ob-field ${error ? "ob-field-err" : ""}`}>
      <label htmlFor={id}>
        {label}{required && <span className="ob-req"> *</span>}
      </label>
      {children}
      {error && <span className="ob-err-msg">{error}</span>}
    </div>
  );
}

function ComingSoon({ provider }) {
  const label = provider === "azure" ? "Azure" : "GCP";
  return (
    <div className="ob-section" style={{ textAlign: "center", padding: "48px 24px" }}>
      <div className="ob-section-title">{label.toUpperCase()} — COMING SOON</div>
      <p className="ob-metrics-hint" style={{ marginTop: 12 }}>
        {label} account onboarding isn't available yet. AWS accounts can be
        onboarded today from the AWS tab.
      </p>
    </div>
  );
}

async function refreshQueue(setQueue) {
  try {
    const r = await fetch(`${BASE}/api/admin/accounts`);
    if (!r.ok) throw new Error();
    const all = await r.json();
    // Only show truly pending/processing — not active ones
    const pending = Array.isArray(all)
      ? all.filter(a => a.status && a.status !== "active" && a.status !== "healthy")
      : [];
    setQueue(pending.map(a => ({
      account_name: a.account_name,
      account_id: a.account_id,
      status: a.status,
    })));
  } catch {}
}

export default function AccountOnboarding() {
  const [provider, setProvider] = useState("aws");
  const [form,    setForm]    = useState(INITIAL_FORM);
  const [errors,  setErrors]  = useState({});
  const [queue,   setQueue]   = useState([]);
  const [saving,  setSaving]  = useState(false);
  const [success, setSuccess] = useState(null);
  const [apiErr,  setApiErr]  = useState(null);

  const [catalog,        setCatalog]        = useState([]);
  const [selectedIds,    setSelectedIds]    = useState(new Set());
  const [catalogLoading, setCatalogLoading] = useState(true);
  const defaultsAppliedRef = useRef(false);

  useEffect(() => {
    refreshQueue(setQueue);
    // Poll queue every 15s to catch backend-side updates
    const t = setInterval(() => refreshQueue(setQueue), 15000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    getMetricCatalog()
      .then(data => setCatalog(Array.isArray(data) ? data : []))
      .catch(() => setCatalog([]))
      .finally(() => setCatalogLoading(false));
  }, []);

  // Applies recommended defaults once the catalog actually arrives — kept as
  // its own effect (rather than inline in the fetch above) so it reliably
  // fires whenever `catalog` changes, and only the first time so it never
  // clobbers a selection the user has already started editing.
  useEffect(() => {
    if (catalog.length > 0 && !defaultsAppliedRef.current) {
      const defaults = new Set();
      catalog.forEach(g => g.metrics.forEach(m => { if (m.is_default) defaults.add(m.id); }));
      setSelectedIds(defaults);
      defaultsAppliedRef.current = true;
    }
  }, [catalog]);

  function validate() {
    const e = {};
    if (!form.account_name.trim())   e.account_name   = "Required";
    if (!form.account_id.trim())     e.account_id     = "Required";
    if (!/^\d{12}$/.test(form.account_id.trim())) e.account_id = "Must be 12 digits";
    if (!form.primary_region)        e.primary_region = "Select a region";
    if (!form.owner_team.trim())     e.owner_team     = "Required";
    if (form.auth_method === "iam_role" && !form.iam_role_arn.trim())
      e.iam_role_arn = "IAM Role ARN is required";
    if (form.auth_method === "access_keys") {
      if (!form.access_key.trim()) e.access_key = "Required";
      if (!form.secret_key.trim()) e.secret_key = "Required";
    }
    return e;
  }

  async function handleSubmit(e) {
    e.preventDefault();
    const errs = validate();
    if (Object.keys(errs).length) { setErrors(errs); return; }
    setSaving(true);
    setApiErr(null);

    const accountName = form.account_name.trim();

    // Optimistically add to queue immediately
    // Remove from queue immediately on success — it's now active
    setQueue(prev => prev.filter(q => q.account_name !== accountName));

    try {
      const body = {
        account_name:   accountName,
        account_id:     form.account_id.trim(),
        default_region: form.primary_region,
        environment:    form.environment,
        owner_team:     form.owner_team.trim(),
        alias:          form.alias.trim(),
        description:    form.description.trim(),
        iam_role_arn:   form.auth_method === "iam_role"    ? form.iam_role_arn.trim() : "",
        external_id:    form.auth_method === "iam_role"    ? form.external_id.trim()  : "",
        access_key:     form.auth_method === "access_keys" ? form.access_key.trim()   : "",
        secret_key:     form.auth_method === "access_keys" ? form.secret_key.trim()   : "",
        selected_metric_ids: Array.from(selectedIds),
      };
      const res = await fetch(`${BASE}/api/admin/accounts`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(body),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || `Server error ${res.status}`);
      }
      setSuccess(accountName);
      setForm(INITIAL_FORM);
      setErrors({});
      // Update queue with real status from server
      setTimeout(() => refreshQueue(setQueue), 1000);
      setTimeout(() => refreshQueue(setQueue), 4000);
    } catch (err) {
      setApiErr(err.message);
      // Remove the optimistic entry on failure
      setQueue(prev => prev.filter(q => !(q.account_name === accountName && q.status === "pending")));
    } finally {
      setSaving(false);
    }
  }

  function handleReset() {
    setForm(INITIAL_FORM);
    setErrors({});
    setApiErr(null);
    setSuccess(null);
    const defaults = new Set();
    catalog.forEach(g => g.metrics.forEach(m => { if (m.is_default) defaults.add(m.id); }));
    setSelectedIds(defaults);
  }

  return (
    <div className="onboard-page">
      <div className="onboard-main">
        <div className="ob-provider-tabs">
          {PROVIDER_TABS.map(t => (
            <button
              type="button"
              key={t.id}
              className={`ob-provider-tab ${provider === t.id ? "ob-provider-tab-active" : ""}`}
              onClick={() => setProvider(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>

        {provider !== "aws" && <ComingSoon provider={provider} />}

        {provider === "aws" && (
        <>
        <div className="onboard-hero">
          <h1>Onboard <span className="hl">AWS Account</span></h1>
          <p>Register a new AWS account for centralized CloudWatch monitoring</p>
        </div>

        {success && (
          <div className="ob-success">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polyline points="20 6 9 17 4 12"/>
            </svg>
            <strong>{success}</strong> onboarded successfully!
            <button onClick={() => setSuccess(null)}>✕</button>
          </div>
        )}
        {apiErr && (
          <div className="ob-api-err">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10"/>
              <line x1="12" y1="8" x2="12" y2="12"/>
              <line x1="12" y1="16" x2="12.01" y2="16"/>
            </svg>
            {apiErr}
            <button onClick={() => setApiErr(null)}>✕</button>
          </div>
        )}

        <form className="onboard-form" onSubmit={handleSubmit} noValidate>

          {/* Account Identity */}
          <div className="ob-section">
            <div className="ob-section-title">ACCOUNT IDENTITY</div>
            <div className="ob-grid-2">
              <Field id="account_name" label="Account Name" required error={errors.account_name}>
                <input
                  id="account_name"
                  value={form.account_name}
                  placeholder="e.g. ProductionEast"
                  onChange={e => setForm(f => ({ ...f, account_name: e.target.value }))}
                />
              </Field>
              <Field id="account_id" label="AWS Account ID" required error={errors.account_id}>
                <input
                  id="account_id"
                  value={form.account_id}
                  placeholder="123456789012"
                  maxLength={12}
                  onChange={e => setForm(f => ({ ...f, account_id: e.target.value.replace(/\D/g, "") }))}
                />
              </Field>
            </div>
            <div className="ob-grid-2">
              <Field id="primary_region" label="Primary Region" required error={errors.primary_region}>
                <select
                  id="primary_region"
                  value={form.primary_region}
                  onChange={e => setForm(f => ({ ...f, primary_region: e.target.value }))}
                >
                  <option value="">Select region…</option>
                  {ALL_REGIONS.map(r => (
                    <option key={r.id} value={r.id}>{r.label}</option>
                  ))}
                </select>
              </Field>
              <Field id="environment" label="Environment" required error={errors.environment}>
                <select
                  id="environment"
                  value={form.environment}
                  onChange={e => setForm(f => ({ ...f, environment: e.target.value }))}
                >
                  {ENVIRONMENTS.map(env => <option key={env}>{env}</option>)}
                </select>
              </Field>
            </div>
          </div>

          {/* Metadata */}
          <div className="ob-section">
            <div className="ob-section-title">METADATA</div>
            <div className="ob-grid-2">
              <Field id="owner_team" label="Owner Team" required error={errors.owner_team}>
                <input
                  id="owner_team"
                  value={form.owner_team}
                  placeholder="e.g. HCS, CloudOps"
                  onChange={e => setForm(f => ({ ...f, owner_team: e.target.value }))}
                />
              </Field>
              <Field id="alias" label="Alias / Alias Tag">
                <input
                  id="alias"
                  value={form.alias}
                  placeholder="e.g. prod-eu"
                  onChange={e => setForm(f => ({ ...f, alias: e.target.value }))}
                />
              </Field>
            </div>
            <Field id="description" label="Description">
              <textarea
                id="description"
                value={form.description}
                rows={3}
                placeholder="Brief description of this account's purpose…"
                onChange={e => setForm(f => ({ ...f, description: e.target.value }))}
              />
            </Field>
          </div>

          {/* IAM Credentials */}
          <div className="ob-section">
            <div className="ob-section-title">IAM CREDENTIALS (CLOUDWATCH READONLY)</div>
            <div className="ob-cred-notice">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <rect x="3" y="11" width="18" height="11" rx="2"/>
                <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
              </svg>
              Credentials stored encrypted at rest. IAM Role ARN preferred over Access Keys.
            </div>
            <div className="ob-auth-toggle">
              {["iam_role", "access_keys"].map(m => (
                <button
                  type="button"
                  key={m}
                  className={`ob-auth-btn ${form.auth_method === m ? "ob-auth-active" : ""}`}
                  onClick={() => setForm(f => ({ ...f, auth_method: m }))}
                >
                  {m === "iam_role" ? "IAM Role ARN" : "Access Keys"}
                </button>
              ))}
            </div>

            {form.auth_method === "iam_role" ? (
              <div className="ob-grid-2">
                <Field id="iam_role_arn" label="IAM Role ARN" required error={errors.iam_role_arn}>
                  <input
                    id="iam_role_arn"
                    value={form.iam_role_arn}
                    placeholder="arn:aws:iam::123…:role/CloudOps"
                    onChange={e => setForm(f => ({ ...f, iam_role_arn: e.target.value }))}
                  />
                </Field>
                <Field id="external_id" label="External ID">
                  <input
                    id="external_id"
                    value={form.external_id}
                    placeholder="Optional STS ExternalId"
                    onChange={e => setForm(f => ({ ...f, external_id: e.target.value }))}
                  />
                </Field>
              </div>
            ) : (
              <div className="ob-grid-2">
                <Field id="access_key" label="Access Key ID" required error={errors.access_key}>
                  <input
                    id="access_key"
                    value={form.access_key}
                    placeholder="AKIAIOSFODNN7EXAMPLE"
                    onChange={e => setForm(f => ({ ...f, access_key: e.target.value }))}
                  />
                </Field>
                <Field id="secret_key" label="Secret Access Key" required error={errors.secret_key}>
                  <input
                    id="secret_key"
                    type="password"
                    value={form.secret_key}
                    placeholder="••••••••••••••••"
                    onChange={e => setForm(f => ({ ...f, secret_key: e.target.value }))}
                  />
                </Field>
              </div>
            )}
          </div>

          {/* Metrics to Monitor */}
          <div className="ob-section">
            <div className="ob-section-title">METRICS TO MONITOR</div>
            <p className="ob-metrics-hint">
              Recommended cost-optimized defaults are pre-selected. Add or remove any
              metric now, or come back later from Settings → Metrics for this account.
            </p>
            {catalogLoading ? (
              <div className="ob-metrics-loading">Loading metric catalog…</div>
            ) : (
              <MetricSelector
                catalog={catalog}
                selectedIds={selectedIds}
                onChange={setSelectedIds}
                compact
              />
            )}
          </div>

          <div className="ob-actions">
            <button type="button" className="ob-btn-ghost" onClick={handleReset}>
              Clear Form
            </button>
            <button type="submit" className="ob-btn-primary" disabled={saving}>
              {saving ? (
                <><span className="ob-spinner" /> Onboarding…</>
              ) : (
                "Onboard Account →"
              )}
            </button>
          </div>
        </form>
        </>
        )}
      </div>

      {/* Sidebar queue */}
      <aside className="onboard-sidebar">
        <div className="obs-header">
          <span className="obs-title">ONBOARDING QUEUE</span>
          <span className={`obs-count ${queue.length > 0 ? "obs-count-orange" : ""}`}>
            {queue.length} PENDING
          </span>
        </div>
        {queue.length === 0 ? (
          <div className="obs-empty">
            <div>No accounts queued.</div>
            <div className="obs-empty-sub">Fill the form and click Onboard.</div>
          </div>
        ) : (
          <div className="obs-list">
            {queue.map((q, i) => (
              <div key={i} className="obs-item">
                <div className="obs-item-name">{q.account_name}</div>
                <div className="obs-item-id">{q.account_id}</div>
                <span className={`obs-item-status obs-status-${q.status || "pending"}`}>{q.status || "pending"}</span>
              </div>
            ))}
          </div>
        )}
      </aside>
    </div>
  );
}