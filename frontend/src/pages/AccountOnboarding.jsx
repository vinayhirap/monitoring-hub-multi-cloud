// monitoring-hub/frontend/src/pages/AccountOnboarding.jsx
import { useState, useEffect, useRef } from "react";
import MetricSelector from "../components/MetricSelector";
import { getMetricCatalog, testAzureCredentials, testGcpCredentials } from "../api/api";
import "./AccountOnboarding.css";

const BASE = "";

const AWS_REGIONS = [
  { id: "ap-south-1",     label: "ap-south-1 (Mumbai)" },
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

const AZURE_LOCATIONS = [
  { id: "centralindia",     label: "Central India (Pune)" },
  { id: "southindia",       label: "South India (Chennai)" },
  { id: "westindia",        label: "West India (Mumbai)" },
  { id: "eastasia",         label: "East Asia (Hong Kong)" },
  { id: "southeastasia",    label: "Southeast Asia (Singapore)" },
  { id: "japaneast",        label: "Japan East (Tokyo)" },
  { id: "australiaeast",    label: "Australia East (Sydney)" },
  { id: "eastus",           label: "East US (Virginia)" },
  { id: "eastus2",          label: "East US 2 (Virginia)" },
  { id: "westus2",          label: "West US 2 (Washington)" },
  { id: "centralus",        label: "Central US (Iowa)" },
  { id: "northeurope",      label: "North Europe (Ireland)" },
  { id: "westeurope",       label: "West Europe (Netherlands)" },
  { id: "uksouth",          label: "UK South (London)" },
  { id: "uaenorth",         label: "UAE North (Dubai)" },
];

const GCP_REGIONS = [
  { id: "asia-south1",       label: "asia-south1 (Mumbai)" },
  { id: "asia-south2",       label: "asia-south2 (Delhi)" },
  { id: "asia-southeast1",   label: "asia-southeast1 (Singapore)" },
  { id: "asia-east1",        label: "asia-east1 (Taiwan)" },
  { id: "asia-northeast1",   label: "asia-northeast1 (Tokyo)" },
  { id: "australia-southeast1", label: "australia-southeast1 (Sydney)" },
  { id: "us-central1",       label: "us-central1 (Iowa)" },
  { id: "us-east1",          label: "us-east1 (S. Carolina)" },
  { id: "us-west1",          label: "us-west1 (Oregon)" },
  { id: "europe-west1",      label: "europe-west1 (Belgium)" },
  { id: "europe-west2",      label: "europe-west2 (London)" },
  { id: "europe-central2",   label: "europe-central2 (Warsaw)" },
];

const ENVIRONMENTS = ["Production", "Staging", "Development", "QA"];

const PROVIDER_TABS = [
  { id: "aws",   label: "AWS" },
  { id: "azure", label: "Azure" },
  { id: "gcp",   label: "GCP" },
];

const PROVIDER_REGIONS = { aws: AWS_REGIONS, azure: AZURE_LOCATIONS, gcp: GCP_REGIONS };
const PROVIDER_REGION_LABEL = { aws: "Primary Region", azure: "Location", gcp: "Region" };

const INITIAL_FORM = {
  // shared
  account_name:   "",
  primary_region: "",
  environment:    "Production",
  owner_team:     "",
  alias:          "",
  description:    "",
  // aws
  account_id:     "",
  iam_role_arn:   "",
  external_id:    "",
  access_key:     "",
  secret_key:     "",
  auth_method:    "iam_role",
  // azure
  tenant_id:       "",
  subscription_id: "",
  client_id:       "",
  client_secret:   "",
  // gcp
  project_id:            "",
  service_account_key:   "",
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

function TestConnectionButton({ onTest, status, resultText }) {
  return (
    <div className="ob-test-conn">
      <button type="button" className="ob-btn-ghost ob-test-btn" onClick={onTest} disabled={status === "loading"}>
        {status === "loading" ? (<><span className="ob-spinner" /> Testing…</>) : "Test Connection"}
      </button>
      {status === "success" && (
        <span className="ob-test-ok">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <polyline points="20 6 9 17 4 12"/>
          </svg>
          {resultText || "Verified"}
        </span>
      )}
      {status === "error" && <span className="ob-test-fail">{resultText || "Validation failed"}</span>}
    </div>
  );
}

async function refreshQueue(setQueue) {
  try {
    const r = await fetch(`${BASE}/api/admin/accounts`);
    if (!r.ok) throw new Error();
    const all = await r.json();
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

  const [testStatus, setTestStatus] = useState(null); // null | loading | success | error
  const [testMsg,    setTestMsg]    = useState("");

  const [catalog,        setCatalog]        = useState([]);
  const [selectedIds,    setSelectedIds]    = useState(new Set());
  const [catalogLoading, setCatalogLoading] = useState(true);
  const defaultsAppliedRef = useRef(false);

  useEffect(() => {
    refreshQueue(setQueue);
    const t = setInterval(() => refreshQueue(setQueue), 15000);
    return () => clearInterval(t);
  }, []);

  // Re-fetch the metric catalog whenever the provider tab changes — each
  // cloud has its own curated services/metrics (see app/providers/*/metric_catalog_data.py).
  useEffect(() => {
    setCatalogLoading(true);
    defaultsAppliedRef.current = false;
    setSelectedIds(new Set());
    getMetricCatalog({ provider })
      .then(data => setCatalog(Array.isArray(data) ? data : []))
      .catch(() => setCatalog([]))
      .finally(() => setCatalogLoading(false));
  }, [provider]);

  useEffect(() => {
    if (catalog.length > 0 && !defaultsAppliedRef.current) {
      const defaults = new Set();
      catalog.forEach(g => g.metrics.forEach(m => { if (m.is_default) defaults.add(m.id); }));
      setSelectedIds(defaults);
      defaultsAppliedRef.current = true;
    }
  }, [catalog]);

  function setProviderTab(p) {
    setProvider(p);
    setErrors({});
    setApiErr(null);
    setTestStatus(null);
    setTestMsg("");
  }

  function validate() {
    const e = {};
    if (!form.account_name.trim()) e.account_name = "Required";
    if (!form.primary_region)      e.primary_region = "Select a region";
    if (!form.owner_team.trim())   e.owner_team = "Required";

    if (provider === "aws") {
      if (!form.account_id.trim()) e.account_id = "Required";
      else if (!/^\d{12}$/.test(form.account_id.trim())) e.account_id = "Must be 12 digits";
      if (form.auth_method === "iam_role" && !form.iam_role_arn.trim())
        e.iam_role_arn = "IAM Role ARN is required";
      if (form.auth_method === "access_keys") {
        if (!form.access_key.trim()) e.access_key = "Required";
        if (!form.secret_key.trim()) e.secret_key = "Required";
      }
    } else if (provider === "azure") {
      if (!form.tenant_id.trim())       e.tenant_id = "Required";
      if (!form.subscription_id.trim()) e.subscription_id = "Required";
      if (!form.client_id.trim())       e.client_id = "Required";
      if (!form.client_secret.trim())   e.client_secret = "Required";
    } else if (provider === "gcp") {
      if (!form.project_id.trim())          e.project_id = "Required";
      if (!form.service_account_key.trim()) e.service_account_key = "Required";
      else {
        try { JSON.parse(form.service_account_key); }
        catch { e.service_account_key = "Must be valid JSON (paste the SA key file contents)"; }
      }
    }
    return e;
  }

  async function handleTestConnection() {
    setTestStatus("loading");
    setTestMsg("");
    try {
      if (provider === "azure") {
        const r = await testAzureCredentials({
          tenant_id: form.tenant_id.trim(),
          subscription_id: form.subscription_id.trim(),
          client_id: form.client_id.trim(),
          client_secret: form.client_secret.trim(),
        });
        setTestStatus("success");
        setTestMsg(`Verified — ${r.resource_groups_visible} resource group(s) visible`);
      } else if (provider === "gcp") {
        const r = await testGcpCredentials({
          project_id: form.project_id.trim(),
          service_account_key: form.service_account_key.trim(),
        });
        setTestStatus("success");
        setTestMsg(`Verified — project "${r.project_display_name || form.project_id}"`);
      }
    } catch (err) {
      setTestStatus("error");
      setTestMsg(err.message || "Validation failed");
    }
  }

  async function handleSubmit(e) {
    e.preventDefault();
    const errs = validate();
    if (Object.keys(errs).length) { setErrors(errs); return; }
    setSaving(true);
    setApiErr(null);

    const accountName = form.account_name.trim();
    setQueue(prev => prev.filter(q => q.account_name !== accountName));

    let body = {
      provider,
      account_name:   accountName,
      default_region: form.primary_region,
      environment:    form.environment,
      owner_team:     form.owner_team.trim(),
      alias:          form.alias.trim(),
      description:    form.description.trim(),
      selected_metric_ids: Array.from(selectedIds),
    };

    if (provider === "aws") {
      body = {
        ...body,
        account_id:   form.account_id.trim(),
        iam_role_arn: form.auth_method === "iam_role"    ? form.iam_role_arn.trim() : "",
        external_id:  form.auth_method === "iam_role"    ? form.external_id.trim()  : "",
        access_key:   form.auth_method === "access_keys" ? form.access_key.trim()   : "",
        secret_key:   form.auth_method === "access_keys" ? form.secret_key.trim()   : "",
      };
    } else if (provider === "azure") {
      body = {
        ...body,
        tenant_id:       form.tenant_id.trim(),
        subscription_id: form.subscription_id.trim(),
        client_id:       form.client_id.trim(),
        client_secret:   form.client_secret.trim(),
      };
    } else if (provider === "gcp") {
      body = {
        ...body,
        project_id:           form.project_id.trim(),
        service_account_key:  form.service_account_key.trim(),
      };
    }

    try {
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
      setTestStatus(null);
      setTestMsg("");
      setTimeout(() => refreshQueue(setQueue), 1000);
      setTimeout(() => refreshQueue(setQueue), 4000);
    } catch (err) {
      setApiErr(err.message);
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
    setTestStatus(null);
    setTestMsg("");
    const defaults = new Set();
    catalog.forEach(g => g.metrics.forEach(m => { if (m.is_default) defaults.add(m.id); }));
    setSelectedIds(defaults);
  }

  const regionOptions = PROVIDER_REGIONS[provider];
  const regionLabel = PROVIDER_REGION_LABEL[provider];
  const heroLabel = provider === "aws" ? "AWS" : provider === "azure" ? "Azure" : "GCP";

  return (
    <div className="onboard-page">
      <div className="onboard-main">
        <div className="ob-provider-tabs">
          {PROVIDER_TABS.map(t => (
            <button
              type="button"
              key={t.id}
              className={`ob-provider-tab ${provider === t.id ? "ob-provider-tab-active" : ""}`}
              onClick={() => setProviderTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="onboard-hero">
          <h1>Onboard <span className="hl">{heroLabel} Account</span></h1>
          <p>
            {provider === "aws"   && "Register a new AWS account for centralized CloudWatch monitoring"}
            {provider === "azure" && "Register a new Azure subscription via a Service Principal for centralized Azure Monitor metrics"}
            {provider === "gcp"   && "Register a new GCP project via a Service Account for centralized Cloud Monitoring metrics"}
          </p>
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

              {provider === "aws" && (
                <Field id="account_id" label="AWS Account ID" required error={errors.account_id}>
                  <input
                    id="account_id"
                    value={form.account_id}
                    placeholder="123456789012"
                    maxLength={12}
                    onChange={e => setForm(f => ({ ...f, account_id: e.target.value.replace(/\D/g, "") }))}
                  />
                </Field>
              )}
              {provider === "azure" && (
                <Field id="subscription_id" label="Subscription ID" required error={errors.subscription_id}>
                  <input
                    id="subscription_id"
                    value={form.subscription_id}
                    placeholder="00000000-0000-0000-0000-000000000000"
                    onChange={e => setForm(f => ({ ...f, subscription_id: e.target.value.trim() }))}
                  />
                </Field>
              )}
              {provider === "gcp" && (
                <Field id="project_id" label="GCP Project ID" required error={errors.project_id}>
                  <input
                    id="project_id"
                    value={form.project_id}
                    placeholder="my-project-123456"
                    onChange={e => setForm(f => ({ ...f, project_id: e.target.value.trim() }))}
                  />
                </Field>
              )}
            </div>
            <div className="ob-grid-2">
              <Field id="primary_region" label={regionLabel} required error={errors.primary_region}>
                <select
                  id="primary_region"
                  value={form.primary_region}
                  onChange={e => setForm(f => ({ ...f, primary_region: e.target.value }))}
                >
                  <option value="">Select…</option>
                  {regionOptions.map(r => (
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

          {/* Credentials — provider specific */}
          {provider === "aws" && (
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
          )}

          {provider === "azure" && (
            <div className="ob-section">
              <div className="ob-section-title">SERVICE PRINCIPAL (READER ROLE)</div>
              <div className="ob-cred-notice">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <rect x="3" y="11" width="18" height="11" rx="2"/>
                  <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
                </svg>
                Client secret is encrypted at rest and never returned by any API response. Grant this
                Service Principal the built-in "Reader" role on the subscription.
              </div>
              <div className="ob-grid-2">
                <Field id="tenant_id" label="Tenant (Directory) ID" required error={errors.tenant_id}>
                  <input
                    id="tenant_id"
                    value={form.tenant_id}
                    placeholder="00000000-0000-0000-0000-000000000000"
                    onChange={e => setForm(f => ({ ...f, tenant_id: e.target.value.trim() }))}
                  />
                </Field>
                <Field id="client_id" label="Application (Client) ID" required error={errors.client_id}>
                  <input
                    id="client_id"
                    value={form.client_id}
                    placeholder="00000000-0000-0000-0000-000000000000"
                    onChange={e => setForm(f => ({ ...f, client_id: e.target.value.trim() }))}
                  />
                </Field>
              </div>
              <Field id="client_secret" label="Client Secret" required error={errors.client_secret}>
                <input
                  id="client_secret"
                  type="password"
                  value={form.client_secret}
                  placeholder="••••••••••••••••"
                  onChange={e => setForm(f => ({ ...f, client_secret: e.target.value }))}
                />
              </Field>
              <TestConnectionButton onTest={handleTestConnection} status={testStatus} resultText={testMsg} />
            </div>
          )}

          {provider === "gcp" && (
            <div className="ob-section">
              <div className="ob-section-title">SERVICE ACCOUNT (VIEWER ROLE)</div>
              <div className="ob-cred-notice">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <rect x="3" y="11" width="18" height="11" rx="2"/>
                  <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
                </svg>
                Paste the full contents of a Service Account JSON key with "Viewer" (or equivalent
                read-only) IAM role on the project. The key is encrypted at rest and never returned
                by any API response.
              </div>
              <Field id="service_account_key" label="Service Account Key (JSON)" required error={errors.service_account_key}>
                <textarea
                  id="service_account_key"
                  className="ob-json-key"
                  value={form.service_account_key}
                  rows={8}
                  placeholder={`{\n  "type": "service_account",\n  "project_id": "…",\n  "private_key": "…",\n  "client_email": "…"\n  …\n}`}
                  onChange={e => setForm(f => ({ ...f, service_account_key: e.target.value }))}
                />
              </Field>
              <TestConnectionButton onTest={handleTestConnection} status={testStatus} resultText={testMsg} />
            </div>
          )}

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
