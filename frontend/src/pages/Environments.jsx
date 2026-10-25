// src/pages/Environments.jsx
// Environments and tags. Two separate things live here, and the page keeps them apart:
//   1. Environment: which environment (PROD / NON-PROD) CloudOps treats each resource as. Decides alert strength.
//      Resources nothing can place are "Not placed" and wait in the first tab; they are alerted like prod meanwhile.
//   2. AWS tags: whether the resource carries the standard Environment tag in AWS. CloudOps only reads AWS; the
//      second tab hands you a script to run with your own credentials.
// Writes need environments.manage; everything is scoped to the caller's accounts server-side.
import { useState, useEffect, useCallback, useRef, Fragment } from "react";
import {
  getEnvironmentSummary, getEnvironmentResources, classifyResources, classifyGroup, setAccountDefaultEnv, getTagPlan,
  previewAccountDefaultEnv, getEnvironmentHistory, getEnvironmentCompliance, getEnvironmentTrend,
  downloadComplianceCsv, downloadTaggingGapsCsv,
} from "../api/api";
import { copyText } from "../utils/copyText";
import { useAuth } from "../auth/AuthContext";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import "./Environments.css";

const PAGE = 50;
const REFRESH_MS = 60000;
const QUICK_GROUPS = 6;
const PROVIDER_LABEL = { aws: "AWS", azure: "Azure", gcp: "GCP" };
// the tags CloudOps can use (server-side ESSENTIAL_TAGS); cost tags are deliberately not asked for
const CLOUDOPS_TAGS = [
  ["Environment", "decides alert strength and polling (required)"],
  ["Criticality", "critical / high / medium / low; reserved for a priority bump"],
  ["Owner", "the team that answers for the resource"],
  ["Application", "what the resource belongs to"],
];

// Two environments. Older audit-history rows may still carry the pre-2026 values (uat, dev, unknown, ignore), so
// those keys are kept for display only: uat/dev read as NON-PROD, unknown/ignore as PROD (the fail-safe).
const ENV_LABEL = { prod: "PROD", nonprod: "NON-PROD", uat: "NON-PROD", dev: "NON-PROD", unknown: "Not placed", ignore: "PROD" };
const ENV_LONG = { prod: "PROD", nonprod: "NON-PROD", uat: "NON-PROD", dev: "NON-PROD" };
const ENVS = ["prod", "nonprod"];
const envClass = e => (e === "uat" || e === "dev") ? "nonprod" : (e === "ignore" ? "prod" : e);
const SOURCE_LABEL = {
  manual: "Set by a person", tag: "From the AWS tag", name: "From the name", related: "From the attached resource",
  account: "From the account default", unknown: "Nothing identifies it",
};
const TAG_LABEL = {
  ok: "Correct tag", nonstandard: "Tag needs fixing", invalid: "Unrecognised value",
  missing: "No Environment tag", not_checked: "Cannot be checked",
};
const TAG_HINT = {
  ok: "The Environment tag is there and says exactly prod or nonprod.",
  nonstandard: "A tag exists but its name or value is not the standard one (for example Env=Production).",
  invalid: "An environment tag exists but its value is not prod or nonprod.",
  missing: "The resource has no Environment tag in AWS.",
  not_checked: "CloudOps has no reading of this resource's own AWS tags (for example EBS volumes, or any type the tag sync does not cover or while it is off), so it cannot tell.",
};
const WARNING_LABEL = {
  manual_vs_tag: "Placed here, tag says otherwise", tag_vs_name: "Tag and name disagree",
  manual_vs_name: "Placed here, name says otherwise", volume_vs_instance: "Differs from its instance",
};
const CONF_LABEL = { high: "High confidence", medium: "Medium confidence", low: "Low confidence" };
const SRC_ORDER = ["tag", "manual", "name", "related", "account", "unknown", "pending"];
const SRC_NAME = { ...SOURCE_LABEL, pending: "Not evaluated yet" };
const TAG_ORDER = ["ok", "nonstandard", "invalid", "missing", "not_checked"];
const TAG_PROBLEMS = ["missing", "nonstandard", "invalid"];

function CloudBadge({ provider }) {
  const p = provider || "aws";
  return <span className={`env-cloud env-cloud-${p}`}>{PROVIDER_LABEL[p] || p}</span>;
}

function EnvBadge({ env }) {
  if (!env) return <span className="env-badge env-none">Not placed</span>;
  return <span className={`env-badge env-${envClass(env)}`}>{ENV_LABEL[env] || env}</span>;
}

function TagBadge({ state }) {
  if (!state) return <span className="env-badge env-none">Pending</span>;
  return <span className={`env-badge tag-${state}`} title={TAG_HINT[state]}>{TAG_LABEL[state] || state}</span>;
}

function saveText(name, text) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/x-shellscript" }));
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// One plain sentence per row saying what the person should do about it.
function nextStep(r, view, canManage) {
  const other = r.provider && r.provider !== "aws";
  if (view === "tagging" && other) {
    return `Add the ${r.provider === "gcp" ? "label" : "tag"} environment = ${r.env || "prod or nonprod"} in the ${PROVIDER_LABEL[r.provider]} console. The script covers AWS only.`;
  }
  if (view === "tagging") {
    if (!ENV_LONG[r.env]) {
      return canManage ? "Mark an environment first (buttons above the table), then add the tag in AWS."
        : "Ask someone with edit access to mark its environment, then add the tag in AWS.";
    }
    if (r.source === "account") {
      return `Add the tag Environment = ${r.env} in AWS yourself, or mark it here first so the script includes it.`;
    }
    if (r.tag_state === "missing") return `In AWS, add the tag Environment = ${r.env}.`;
    if (r.tag_state === "nonstandard") return `In AWS, change the tag to exactly Environment = ${r.env}.`;
    if (r.tag_state === "invalid") return `In AWS, set the Environment tag value to ${r.env}.`;
    return "Nothing to do.";
  }
  if (view === "needs_classification") {
    return canManage ? "Mark it PROD or NON-PROD." : "Ask someone with edit access to place it.";
  }
  // the stored reason is a lowercase fragment ("account default", "attached to ec2 X ..."); show it as a sentence
  const why = r.reason || SOURCE_LABEL[r.source] || "";
  return why ? why.charAt(0).toUpperCase() + why.slice(1) : "";
}

function when(v) { return v ? new Date(v).toLocaleString() : ""; }

function HistoryPanel({ state }) {
  if (state.loading) return <div className="env-muted">Loading history</div>;
  if (state.error) return <div className="env-error">{state.error}</div>;
  const { current, events, incomplete, audit_lookback_days } = state.data;
  return (
    <div className="env-history">
      <div>
        Now: <EnvBadge env={current.env} /> {SOURCE_LABEL[current.source] || "not evaluated yet"}
        {current.override_by ? `, set by ${current.override_by}${current.override_at ? ` on ${when(current.override_at)}` : ""}` : ""}
        {current.reason ? `. ${current.reason.charAt(0).toUpperCase()}${current.reason.slice(1)}.` : ""}
      </div>
      {events.length === 0 && <div className="env-muted">No change by a person is recorded in the last {audit_lookback_days} days.</div>}
      <ul>
        {events.map((e, i) => (
          <li key={i}>
            {when(e.at)}: {e.actor || "someone"}{" "}
            {e.kind === "classified"
              ? <>set it to {ENV_LABEL[e.to_env] || e.to_env}{e.from_env ? <>, was {ENV_LABEL[e.from_env] || e.from_env} ({SOURCE_LABEL[e.from_source] || e.from_source})</> : null}{e.via === "suggestion" ? ", by accepting a suggestion" : ""}</>
              : <>changed the account default from {e.from_env ? ENV_LABEL[e.from_env] : "none"} to {e.to_env ? ENV_LABEL[e.to_env] : "none"}. This resource was affected only if it had no tag, name or choice of its own.</>}
          </li>
        ))}
      </ul>
      {incomplete && <div className="env-muted">A large batch change touching this account stores only its first 100 resources, so this list may be incomplete.</div>}
    </div>
  );
}

function DefaultPreview({ preview, accounts, busy, onConfirm, onCancel }) {
  const d = preview.data;
  const name = accounts.find(a => a.account_id === preview.accountId)?.account_name || d.account_name;
  const al = d.alerts || {};
  useEffect(() => {
    const onKey = e => { if (e.key === "Escape") onCancel(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);
  return (
    <div className="env-modal" role="dialog" aria-modal="true" aria-label="Confirm account default" onClick={e => { if (e.target === e.currentTarget) onCancel(); }}>
      <div className="env-modal-box env-preview">
        <div className="env-modal-head">
          <strong>{preview.env ? `Set ${name} default to ${ENV_LONG[preview.env]}?` : `Clear ${name} default?`}</strong>
          <span>Was: {d.current_default ? ENV_LONG[d.current_default] : "none"}</span>
        </div>
        <p>
          <strong>{d.environment_changes}</strong> of {d.total} resources in this account change environment or the way it was decided.
          {" "}{d.rows_rewritten} records are re-evaluated in total. Ones with a tag, a recognised name, an attached parent or a manual choice are not affected.
        </p>
        {d.moves.filter(m => m.from !== m.to).length > 0 && (
          <table className="env-plan-table">
            <thead><tr><th>From</th><th>To</th><th>Resources</th></tr></thead>
            <tbody>{d.moves.filter(m => m.from !== m.to).map(m => (
              <tr key={`${m.from}>${m.to}`}><td>{ENV_LABEL[m.from] || "Not evaluated"}</td><td>{ENV_LABEL[m.to] || m.to}</td><td>{m.count}</td></tr>
            ))}</tbody>
          </table>
        )}
        <div className="env-chips">
          {[...ENVS, "unknown"].filter(k => d.by_env[k]).map(k => <span key={k} className={`env-chip env-${k}`}>{ENV_LABEL[k]} after: {d.by_env[k]}</span>)}
        </div>
        {!al.flag_on && <div className="env-modal-note">Alerting is unchanged: environment-aware classification is switched off, so this only changes labels.</div>}
        {al.flag_on && (
          <div className="env-modal-note">
            Environment-aware classification is on. Of the alerts open now: {al.open_alerts_affected} would change.
            {" "}{al.warning_to_info} warning-level would drop to info, {al.info_to_warning} info-level would rise to warning (critical keeps its severity).
            {al.priority_on && (al.priority_shifts.length
              ? ` Priority shifts: ${al.priority_shifts.map(s => `${s.from} to ${s.to} x${s.count}`).join(", ")} (a Criticality tag can raise one level further).`
              : " No alert changes priority.")}
          </div>
        )}
        <div className="env-modal-actions">
          <button disabled={busy} onClick={onConfirm}>{d.environment_changes ? `Apply to ${d.environment_changes} resource(s)` : "Apply"}</button>
          <button onClick={onCancel}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

function pctText(b) { return b.pct == null ? "-" : `${b.pct}%`; }

function CompliancePanel({ data, trend, accountId, onError }) {
  const [busy, setBusy] = useState(false);
  if (!data) return <div className="env-muted">Loading the report</div>;
  const t = data.totals;
  const exp = async () => { setBusy(true); try { await downloadComplianceCsv(accountId); } catch (e) { onError(e.message); } setBusy(false); };
  const rowFor = (label, b, key, strong) => (
    <tr key={key} className={strong ? "env-comp-total" : ""}>
      <td>{label}</td><td>{b.total}</td>
      {TAG_ORDER.map(k => <td key={k}>{b.by_tag_state[k].n} <span className="env-muted">{pctText(b.by_tag_state[k])}</span></td>)}
      {SRC_ORDER.map(k => <td key={k}>{b.by_source[k].n} <span className="env-muted">{pctText(b.by_source[k])}</span></td>)}
      <td>{b.warnings}</td>
    </tr>
  );
  const pts = trend?.points || [];
  return (
    <section className="env-compliance">
      <p className="env-lead">
        Percentages are of all resources in the row, so each group adds up to 100. "Cannot be checked" is its own column: CloudOps does not
        read the AWS tags of those types, so they are not counted as untagged. {t.checkable
          ? `${t.ok_of_checkable_pct}% of the ${t.checkable} resources that can be checked carry the correct Environment tag.` : ""}
      </p>
      <div><button disabled={busy} onClick={exp}>Export CSV</button></div>
      <div className="env-table-wrap">
        <table className="env-table env-comp">
          <thead>
            <tr><th rowSpan={2}>Account and type</th><th rowSpan={2}>Total</th>
              <th colSpan={TAG_ORDER.length}>Cloud tag</th><th colSpan={SRC_ORDER.length}>How the environment was decided</th><th rowSpan={2}>Mismatches</th></tr>
            <tr>{TAG_ORDER.map(k => <th key={k}>{TAG_LABEL[k]}</th>)}{SRC_ORDER.map(k => <th key={k}>{SRC_NAME[k]}</th>)}</tr>
          </thead>
          <tbody>
            {rowFor("All accounts", t, "all", true)}
            {data.accounts.map(a => (
              <Fragment key={a.account_id}>
                {rowFor(`${a.account_name} (${PROVIDER_LABEL[a.provider] || "AWS"})`, a, `a${a.account_id}`, true)}
                {a.types.map(ty => rowFor(`   ${ty.resource_type}`, ty, `a${a.account_id}-${ty.resource_type}`, false))}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      <h2>Trend (daily snapshots)</h2>
      {trend && trend.available === false && <div className="env-muted">The snapshot table is not set up yet (migration 099).</div>}
      {trend && trend.available !== false && pts.length === 0 && <div className="env-muted">No snapshot yet. One is saved after each discovery run, once a day is kept.</div>}
      {pts.length > 0 && (
        <div className="env-table-wrap">
          <table className="env-table">
            <thead><tr><th>Date</th><th>Resources</th><th>Correct tag</th><th>Correct, of those checkable</th><th>Cannot be checked</th><th>Not placed</th><th>Mismatches</th></tr></thead>
            <tbody>{pts.slice().reverse().map(p => (
              <tr key={p.date}><td>{p.date}</td><td>{p.total}</td><td>{p.tag_ok}</td>
                <td>{p.ok_of_checkable_pct == null ? "-" : `${p.ok_of_checkable_pct}%`}</td><td>{p.tag_not_checked}</td><td>{p.src_unknown}</td><td>{p.warnings}</td></tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export default function Environments() {
  const { hasPermission } = useAuth();
  const canManage = hasPermission("environments.manage");
  const [view, setView] = useState("needs_classification");
  const [summary, setSummary] = useState(null);
  const [data, setData] = useState({ items: [], total: 0 });
  const [offset, setOffset] = useState(0);
  const [filters, setFilters] = useState({ account_id: "", q: "", env: "", source: "", tag_state: "", resource_type: "", region: "" });
  const [showAllGroups, setShowAllGroups] = useState(false);
  const [plan, setPlan] = useState(null);
  const [copied, setCopied] = useState(false);
  const [selected, setSelected] = useState(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [qText, setQText] = useState("");
  const reqId = useRef(0);
  const [preview, setPreview] = useState(null);        // { accountId, env, data } while the default-change dialog is open
  const [openHistory, setOpenHistory] = useState({});  // resource pk -> { loading, data, error }
  const [compliance, setCompliance] = useState(null);
  const [trend, setTrend] = useState(null);

  // Only the newest request may update the screen: typing in the search box or paging quickly sends several,
  // and an older (slower) answer must not overwrite a newer one.
  const load = useCallback(() => {
    const mine = ++reqId.current;
    getEnvironmentSummary().then(d => { if (reqId.current === mine) setSummary(d); })
      .catch(e => { if (reqId.current === mine) setError(e.message); });
    if (view === "compliance") {
      getEnvironmentCompliance(filters.account_id).then(d => { if (reqId.current === mine) { setCompliance(d); setError(null); } })
        .catch(e => { if (reqId.current === mine) setError(e.message); });
      getEnvironmentTrend(filters.account_id).then(d => { if (reqId.current === mine) setTrend(d); }).catch(() => {});
      return;
    }
    getEnvironmentResources({ view, ...filters, suggest: view === "needs_classification" ? "true" : undefined, limit: PAGE, offset })
      .then(d => {
        if (reqId.current !== mine) return;
        // the page we were on emptied out (everything on it was just classified): go back to the last real page
        if (!d.items.length && offset > 0 && d.total > 0) {
          setOffset((Math.ceil(d.total / PAGE) - 1) * PAGE);
          return;
        }
        setData(d); setError(null);
      })
      .catch(e => { if (reqId.current === mine) setError(e.message); });
  }, [view, filters, offset]);

  useEffect(() => { load(); }, [load]);
  // keep counts and rows current, but never while rows are selected, a request is running or the script dialog is
  // open: a refresh must not move rows out from under someone who is mid-selection
  useAutoRefresh(() => { if (!selected.size && !busy && !plan && !preview) load(); }, REFRESH_MS);
  useEffect(() => { setSelected(new Set()); setOpenHistory({}); }, [view, filters, offset]);
  // Escape closes the script dialog
  useEffect(() => {
    if (!plan) return undefined;
    const onKey = e => { if (e.key === "Escape") setPlan(null); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [plan]);

  const setFilter = (k, v) => { setOffset(0); setFilters(f => ({ ...f, [k]: v })); };
  // search runs 300 ms after the last keystroke instead of once per key
  useEffect(() => {
    if (qText === filters.q) return undefined;
    const t = setTimeout(() => setFilter("q", qText), 300);
    return () => clearTimeout(t);
  }, [qText, filters.q]);
  const switchView = v => { setView(v); setOffset(0); setFilters(f => ({ ...f, env: "", source: "", tag_state: "", resource_type: "", region: "" })); };
  // a tag tile opens the full list filtered to that tag status
  const showTagState = k => { setView("all"); setOffset(0); setFilters(f => ({ ...f, env: "", source: "", tag_state: k, resource_type: "", region: "" })); };

  const openPlan = async () => {
    setBusy(true); setNotice(null);
    try {
      setPlan(await getTagPlan({
        account_id: filters.account_id,
        env: ENVS.includes(filters.env) ? filters.env : "",
        region: filters.region,
        resource_type: ["ec2", "ebs"].includes(filters.resource_type) ? filters.resource_type : "",
      }));
      setCopied(false);
    }
    catch (e) { setError(e.message); }
    setBusy(false);
  };

  const toggle = id => setSelected(prev => {
    const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n;
  });
  const allOnPage = data.items.length > 0 && data.items.every(r => selected.has(r.id));
  const togglePage = () => setSelected(allOnPage ? new Set() : new Set(data.items.map(r => r.id)));

  const apply = async env => {
    if (!selected.size) return;
    if (selected.size > 1 && env !== "clear" &&
        !window.confirm(`Mark ${selected.size} resources as ${ENV_LABEL[env] || env}?`)) return;
    setBusy(true); setNotice(null);
    try {
      const r = await classifyResources([...selected], env);
      setNotice((env === "clear"
        ? `Cleared the manual choice on ${r.updated} resource(s).`
        : `Marked ${r.updated} resource(s) as ${ENV_LABEL[env] || env}.`) + (r.warning ? ` ${r.warning}` : ""));
      setSelected(new Set());
      load();
    } catch (e) { setError(e.message); }
    setBusy(false);
  };

  // every resource of one type in one account that is still waiting
  const applyGroup = async (g, env) => {
    if (!window.confirm(
      `Mark all ${g.n} ${g.resource_type} resource(s) in ${g.account_name} that are still waiting as ${ENV_LONG[env]}?\n\n`
      + "Only resources that nothing else identifies are changed. Ones already placed, tagged or recognised by name are left alone. "
      + "You can undo any of them later: select it and use Clear manual choice.")) return;
    setBusy(true); setNotice(null);
    try {
      const r = await classifyGroup({ account_id: g.account_id, resource_type: g.resource_type }, env);
      setNotice(`Placed ${r.updated} ${g.resource_type} resource(s) in ${g.account_name} as ${ENV_LONG[env]}.${r.warning ? ` ${r.warning}` : ""}`);
      load();
    } catch (e) { setError(e.message); }
    setBusy(false);
  };

  // Changing an account default re-labels every resource in it that has no other evidence, so the person first sees the
  // exact counts (a dry run of the same code) and confirms; nothing is written until then.
  const changeDefault = async (accountId, env) => {
    setBusy(true); setNotice(null);
    try {
      const data = await previewAccountDefaultEnv(accountId, env || null);
      if (data.unchanged) { setNotice("That is already this account's default."); }
      else setPreview({ accountId, env: env || null, data });
    } catch (e) { setError(e.message); }
    setBusy(false);
  };

  const confirmDefault = async () => {
    const { accountId, env } = preview;
    setPreview(null); setBusy(true); setNotice(null);
    try {
      const r = await setAccountDefaultEnv(accountId, env);
      setNotice(`Account default ${env ? `set to ${ENV_LONG[env]}` : "cleared"}; ${r.reclassified} resource(s) re-evaluated.${r.warning ? ` ${r.warning}` : ""}`);
      load();
    } catch (e) { setError(e.message); }
    setBusy(false);
  };

  // accept proposed environments: one audited classify call per environment, never automatic
  const acceptSuggestions = async rows => {
    const byEnv = {};
    rows.forEach(r => { if (r.suggestion) (byEnv[r.suggestion.env] = byEnv[r.suggestion.env] || []).push(r.id); });
    const envs = Object.keys(byEnv);
    if (!envs.length) return;
    const n = rows.filter(r => r.suggestion).length;
    if (n > 1 && !window.confirm(`Accept ${n} suggestions? Each resource is marked with the environment shown next to it. You can undo any of them with Clear manual choice.`)) return;
    setBusy(true); setNotice(null);
    try {
      let done = 0, warn = "";
      for (const env of envs) { const r = await classifyResources(byEnv[env], env, "suggestion"); done += r.updated; warn = r.warning || warn; }
      setNotice(`Accepted ${done} suggestion(s).${warn ? ` ${warn}` : ""}`);
      setSelected(new Set()); load();
    } catch (e) { setError(e.message); }
    setBusy(false);
  };

  const toggleHistory = async pk => {
    if (openHistory[pk]) { setOpenHistory(h => { const n = { ...h }; delete n[pk]; return n; }); return; }
    setOpenHistory(h => ({ ...h, [pk]: { loading: true } }));
    try { const data = await getEnvironmentHistory(pk); setOpenHistory(h => ({ ...h, [pk]: { data } })); }
    catch (e) { setOpenHistory(h => ({ ...h, [pk]: { error: e.message } })); }
  };

  const exportGaps = async () => {
    setBusy(true);
    try { await downloadTaggingGapsCsv({ account_id: filters.account_id, resource_type: filters.resource_type }); }
    catch (e) { setError(e.message); }
    setBusy(false);
  };

  const pages = Math.max(1, Math.ceil(data.total / PAGE));
  const page = Math.floor(offset / PAGE) + 1;

  // figures for the two overview cards
  const accounts = summary?.accounts || [];
  const totalResources = accounts.reduce((n, a) => n + (a.total || 0), 0);
  const needs = summary?.needs_classification ?? 0;
  const byEnv = summary?.by_env || {};
  const bt = summary?.by_tag_state || {};
  const fixable = TAG_PROBLEMS.reduce((n, k) => n + (bt[k] || 0), 0);
  const checkable = fixable + (bt.ok || 0);
  const pct = checkable ? Math.round(100 * (bt.ok || 0) / checkable) : 0;
  const mismatches = summary?.mismatches ?? 0;
  const onboarding = accounts.filter(a => a.recently_onboarded && a.needs_classification > 0);
  const waitingSuggestions = view === "needs_classification" ? data.items.filter(r => r.suggestion) : [];
  const quickGroups = (summary?.queue_groups || [])
    .filter(g => !filters.account_id || String(g.account_id) === String(filters.account_id));

  return (
    <div className="env-page">
      <div className="c-header">
        <div>
          <h1>Environments <span className="hl">and tags</span></h1>
          <div className="sub">
            CloudOps alerts a Production resource more firmly than a test one, so it needs to know which tier every
            resource belongs to. It reads that from the Environment tag on the resource in AWS. Where there is no tag,
            you can place the resource here instead.
          </div>
        </div>
      </div>

      <details className="env-help">
        <summary>What do these terms mean?</summary>
        <dl>
          <dt>Environment</dt>
          <dd>Which of the two environments a resource belongs to: PROD (live) or NON-PROD (everything else: dev, test, SIT, UAT, QA, staging).</dd>
          <dt>Not placed</dt>
          <dd>CloudOps could not tell the tier from a tag, the name or what the resource is attached to. It is alerted as if it were Prod until someone places it.</dd>
          <dt>Environment tag</dt>
          <dd>A label named Environment, with the value prod or nonprod, on the resource in AWS. It is the most reliable source, and other teams can see it too.</dd>
          <dt>Tags CloudOps uses</dt>
          <dd>
            Environment (required: it sets alert strength and polling), then Criticality, Owner and Application, which say how
            important a resource is and who answers for it. Cost tags such as CostCenter are not needed by CloudOps and are not
            checked here; keep them if finance asks for them.
          </dd>
          <dt>Account default</dt>
          <dd>A fallback for resources with no tag and no name hint. Use it only when everything untagged in an account is the same tier.</dd>
        </dl>
      </details>

      {error && <div className="env-error" role="alert">{error}</div>}
      {notice && <div className="env-notice" role="status">{notice}</div>}

      {onboarding.length > 0 && (
        <div className="env-callout" role="status">
          {onboarding.map(a => (
            <div key={a.account_id}>
              <strong>{a.needs_classification}</strong> resource(s) in <strong>{a.account_name}</strong>, onboarded recently, need an
              environment. Until you place them they are alerted as if they were Prod.{" "}
              <button className="env-link" onClick={() => { setFilter("account_id", String(a.account_id)); switchView("needs_classification"); }}>Place them</button>
            </div>
          ))}
        </div>
      )}

      {summary && (
        <div className="env-overview">
          <section className="env-card">
            <h2><span className="env-step-no">1</span> Environment</h2>
            <div className="env-card-main">
              {needs > 0
                ? <><strong>{needs}</strong> of {totalResources} resources are not placed in an environment yet.</>
                : <>All {totalResources} resources have an environment.</>}
            </div>
            <div className="env-chips">
              {ENVS.map(k => <span key={k} className={`env-chip env-${k}`}>{ENV_LABEL[k]} {byEnv[k] || 0}</span>)}
              <span className="env-chip env-unknown">Not placed {needs}</span>
            </div>
            {needs > 0 && <button className="env-link" onClick={() => switchView("needs_classification")}>Place them</button>}
          </section>

          <section className="env-card">
            <h2><span className="env-step-no">2</span> AWS tags</h2>
            <div className="env-card-main">
              {checkable
                ? <><strong>{bt.ok || 0}</strong> of {checkable} resources that can be checked carry the correct Environment tag.</>
                : <>No resource that can be checked was found yet.</>}
            </div>
            <div className="env-bar" role="img" aria-label={`${pct}% correctly tagged`}><span style={{ width: `${pct}%` }} /></div>
            {fixable > 0 && <button className="env-link" onClick={() => switchView("tagging")}>Fix the {fixable} that need a tag</button>}
          </section>
        </div>
      )}

      {summary && (
        <div className="env-tagtiles">
          {TAG_ORDER.map(k => (
            <button key={k} className={`env-tagtile tag-${k}`} onClick={() => showTagState(k)}
                    title="Show these resources" aria-label={`${TAG_LABEL[k]}: ${bt[k] ?? 0}. Show these resources.`}>
              <div className="env-tagtile-n">{bt[k] ?? 0}</div>
              <div className="env-tagtile-l">{TAG_LABEL[k]}</div>
              <div className="env-tagtile-h">{TAG_HINT[k]}</div>
            </button>
          ))}
          {mismatches > 0 && (
            <button className="env-tagtile tag-conflict" onClick={() => switchView("mismatches")} aria-label={`Mismatches: ${mismatches}. Show these resources.`}>
              <div className="env-tagtile-n">{mismatches}</div>
              <div className="env-tagtile-l">Evidence disagrees</div>
              <div className="env-tagtile-h">A tag, a name, a choice made here or an attached resource point at different environments. Nothing was changed; take a look.</div>
            </button>
          )}
          {summary.conflicts > 0 && (
            <div className="env-tagtile tag-conflict">
              <div className="env-tagtile-n">{summary.conflicts}</div>
              <div className="env-tagtile-l">Disagree with AWS</div>
              <div className="env-tagtile-h">A choice made here contradicts the resource's own AWS tag. The AWS tag is the one other teams see.</div>
            </div>
          )}
        </div>
      )}

      {summary && (
        <section className="env-defaults">
          <h2>Account defaults (optional)</h2>
          <p>A fallback for resources with no tag and no name hint. Leave it on None unless everything untagged in the account is the same tier.</p>
          <div className="env-accounts">
            {accounts.map(a => (
              <div key={a.account_id} className="env-account">
                <div className="env-account-name">{a.account_name} <CloudBadge provider={a.provider} /></div>
                <div className="env-account-meta">{a.total} resources, {a.needs_classification} not placed</div>
                <div className="env-chips">
                  {ENVS.filter(k => a.by_env?.[k]).map(k => (
                    <span key={k} className={`env-chip env-${k}`}>{ENV_LABEL[k]} {a.by_env[k]}</span>
                  ))}
                </div>
                <label className="env-field">
                  <span>Default environment</span>
                  <select value={a.default_env || ""} disabled={!canManage || busy}
                          onChange={e => changeDefault(a.account_id, e.target.value)}>
                    <option value="">None</option>
                    <option value="prod">PROD</option>
                    <option value="nonprod">NON-PROD</option>
                  </select>
                </label>
              </div>
            ))}
          </div>
        </section>
      )}

      <div className="env-tabs" role="tablist">
        <button role="tab" aria-selected={view === "needs_classification"} className={view === "needs_classification" ? "active" : ""} onClick={() => switchView("needs_classification")}>
          1. Place resources{summary ? ` (${needs})` : ""}
        </button>
        <button role="tab" aria-selected={view === "tagging"} className={view === "tagging" ? "active" : ""} onClick={() => switchView("tagging")}>
          2. Fix AWS tags{summary ? ` (${fixable})` : ""}
        </button>
        <button role="tab" aria-selected={view === "mismatches"} className={view === "mismatches" ? "active" : ""} onClick={() => switchView("mismatches")}>
          Mismatches{summary ? ` (${mismatches})` : ""}
        </button>
        <button role="tab" aria-selected={view === "compliance"} className={view === "compliance" ? "active" : ""} onClick={() => switchView("compliance")}>Tag compliance</button>
        <button role="tab" aria-selected={view === "all"} className={view === "all" ? "active" : ""} onClick={() => switchView("all")}>All resources</button>
      </div>

      {view === "needs_classification" && (
        <div className="env-lead">
          These resources have no Environment tag and nothing in their name says which tier they are. They are alerted
          as if they were Prod until you place them. Placing a resource here changes CloudOps only, not AWS.
        </div>
      )}

      {view === "mismatches" && (
        <div className="env-lead">
          Each resource here has two pieces of evidence that disagree. CloudOps still uses the environment shown in the row; a
          warning never changes it. Check which piece is wrong: fix the AWS tag or the name, or place the resource here.
        </div>
      )}

      {view === "needs_classification" && canManage && waitingSuggestions.length > 0 && (
        <section className="env-quick">
          <h2>Suggestions for this page</h2>
          <p>{waitingSuggestions.length} of these resources look like siblings that are already placed (same Application tag, stack or name
            pattern). Nothing is applied until you accept. Resources with no suggestion stay in the list.</p>
          <div className="env-quick-actions">
            <button disabled={busy} onClick={() => acceptSuggestions(waitingSuggestions.filter(r => r.suggestion.confidence !== "low"))}>
              Accept the high and medium ones ({waitingSuggestions.filter(r => r.suggestion.confidence !== "low").length})
            </button>
            <button disabled={busy || !selected.size} onClick={() => acceptSuggestions(waitingSuggestions.filter(r => selected.has(r.id)))}>
              Accept for the {waitingSuggestions.filter(r => selected.has(r.id)).length} selected
            </button>
          </div>
        </section>
      )}

      {view === "needs_classification" && canManage && quickGroups.length > 0 && (
        <section className="env-quick">
          <h2>Fastest way: place a whole group at once</h2>
          <p>Each line is every resource of one type in one account that is still waiting. Types such as certificates and backups
            usually belong to a single tier, so one click can place dozens of them.</p>
          {(showAllGroups ? quickGroups : quickGroups.slice(0, QUICK_GROUPS)).map(g => (
            <div key={`${g.account_id}:${g.resource_type}`} className="env-quick-row">
              <div className="env-quick-what">
                <strong>{g.resource_type}</strong> in {g.account_name}
                <span className="env-quick-n">{g.n} waiting</span>
              </div>
              <div className="env-quick-actions">
                {ENVS.map(e => (
                  <button key={e} disabled={busy} onClick={() => applyGroup(g, e)}>Mark all {ENV_LABEL[e]}</button>
                ))}
              </div>
            </div>
          ))}
          {quickGroups.length > QUICK_GROUPS && (
            <button className="env-link" onClick={() => setShowAllGroups(v => !v)}>
              {showAllGroups ? "Show fewer groups" : `Show all ${quickGroups.length} groups`}
            </button>
          )}
          {summary.queue_groups.length >= 40 && (
            <div className="env-quick-more">Only the 40 largest groups are listed. Place some, and the rest appear.</div>
          )}
        </section>
      )}

      {view === "tagging" && (
        <ol className="env-steps">
          <li>
            <strong>Check the list below.</strong> The last column says what to change in AWS for each resource. If it says
            to mark an environment first, do that with the buttons above the table. Resources placed only by an account
            default are not included in the script until you mark them here.
          </li>
          <li>
            <strong>Generate the script.</strong> It adds the Environment tag and leaves every other tag alone. It covers EC2
            instances and EBS volumes; tag other types in the AWS console or Tag Editor.
            <div className="env-scope">
              Script scope: {filters.account_id ? (accounts.find(a => String(a.account_id) === String(filters.account_id))?.account_name || "one account") : "all accounts"}
              {ENVS.includes(filters.env) ? `, ${ENV_LONG[filters.env]} only` : ""}
              {filters.region ? `, ${filters.region}` : ""}
              {["ec2", "ebs"].includes(filters.resource_type) ? `, ${filters.resource_type} only` : ""}
              . Change the filters below to narrow it.
            </div>
            <div>
              {canManage
                ? <button disabled={busy} onClick={openPlan}>Generate AWS tagging script</button>
                : <span className="env-muted">Generating the script needs edit access to environments.</span>}
              {" "}<button disabled={busy} onClick={exportGaps}>Export tagging gaps (CSV)</button>
              <span className="env-muted"> Lists every checkable resource missing any of the seven standard tags, with the tags missing.</span>
            </div>
          </li>
          <li>
            <strong>Run it in AWS</strong>: sign in to the AWS console of the account the script names, open CloudShell, paste the
            script and run it once per account (add DRY_RUN=1 first to preview). CloudOps never changes anything in AWS itself.
            The counts above update after the next discovery run.
          </li>
        </ol>
      )}

      <div className="env-filters">
        <select aria-label="Account" value={filters.account_id} onChange={e => setFilter("account_id", e.target.value)}>
          <option value="">All accounts</option>
          {accounts.map(a => <option key={a.account_id} value={a.account_id}>{a.account_name} ({PROVIDER_LABEL[a.provider] || "AWS"})</option>)}
        </select>
        {view !== "needs_classification" && view !== "compliance" && (
          <select aria-label="Environment" value={filters.env} onChange={e => setFilter("env", e.target.value)}>
            <option value="">Any environment</option>
            {(view === "tagging" ? ENVS : [...ENVS, "unclassified"]).map(v =>
              <option key={v} value={v}>{ENV_LABEL[v] || "Not yet evaluated"}</option>)}
          </select>
        )}
        {view !== "compliance" && <select aria-label="Resource type" value={filters.resource_type} onChange={e => setFilter("resource_type", e.target.value)}>
          <option value="">Any resource type</option>
          {(summary?.facets?.resource_types || []).map(t => <option key={t.value} value={t.value}>{t.value} ({t.n})</option>)}
        </select>}
        {view !== "compliance" && <select aria-label="Region" value={filters.region} onChange={e => setFilter("region", e.target.value)}>
          <option value="">Any region</option>
          {(summary?.facets?.regions || []).map(t => <option key={t.value} value={t.value}>{t.value} ({t.n})</option>)}
        </select>}
        {view === "all" && (
          <select aria-label="How it was decided" value={filters.source} onChange={e => setFilter("source", e.target.value)}>
            <option value="">Any source</option>
            {Object.entries(SOURCE_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
        )}
        {(view === "all" || view === "tagging") && (
          <select aria-label="Tag status" value={filters.tag_state} onChange={e => setFilter("tag_state", e.target.value)}>
            <option value="">{view === "tagging" ? "Any tag problem" : "Any tag status"}</option>
            {(view === "tagging" ? TAG_PROBLEMS : TAG_ORDER).map(k => <option key={k} value={k}>{TAG_LABEL[k]}</option>)}
          </select>
        )}
        {view !== "compliance" && <input placeholder="Search name or ID" aria-label="Search name or ID" value={qText} onChange={e => setQText(e.target.value)} />}
        {(filters.account_id || filters.env || filters.resource_type || filters.region || filters.source || filters.tag_state || qText) && (
          <button className="env-link" onClick={() => { setQText(""); setOffset(0); setFilters({ account_id: "", q: "", env: "", source: "", tag_state: "", resource_type: "", region: "" }); }}>
            Clear filters
          </button>
        )}
      </div>

      {canManage && view !== "compliance" && (
        <div className="env-bulk">
          <span>{selected.size} selected</span>
          {ENVS.map(e => (
            <button key={e} disabled={!selected.size || busy} onClick={() => apply(e)}
                    title={e === "prod" ? "Confirm as PROD. Unplaced resources are already alerted like PROD; this takes them off the waiting list." : undefined}>
              Mark {ENV_LABEL[e]}
            </button>
          ))}
          <button disabled={!selected.size || busy} onClick={() => apply("clear")}>Clear manual choice</button>
        </div>
      )}

      {view === "compliance" && (
        <CompliancePanel data={compliance} trend={trend} accountId={filters.account_id} onError={setError} />
      )}

      {view !== "compliance" && <table className="env-table">
        <thead>
          <tr>
            {canManage && <th><input type="checkbox" checked={allOnPage} onChange={togglePage} aria-label="Select page" /></th>}
            <th>Resource</th><th>Type</th><th>Account</th>
            <th>Environment</th>{view === "needs_classification" && <th>Suggestion</th>}<th>Cloud tag</th><th>{view === "all" ? "Why" : "What to do"}</th>
          </tr>
        </thead>
        <tbody>
          {data.items.map(r => (
            <Fragment key={r.id}>
            <tr>
              {canManage && <td><input type="checkbox" checked={selected.has(r.id)} onChange={() => toggle(r.id)} aria-label={`Select ${r.name || r.resource_id}`} /></td>}
              <td>
                <div className="env-name">{r.name || r.resource_id}</div>
                <div className="env-id">{r.name ? `${r.resource_id}` : ""}{r.region ? `${r.name ? " | " : ""}${r.region}` : ""}</div>
              </td>
              <td>{r.resource_type}</td>
              <td>{r.account_name} <CloudBadge provider={r.provider} /></td>
              <td>
                <EnvBadge env={r.env} />
                <div className="env-source">{SOURCE_LABEL[r.source] || "Not evaluated yet"}{r.override_by ? ` (${r.override_by})` : ""}</div>
                {r.conflict && <div className="env-conflict">AWS tag says {ENV_LONG[r.tag_env] || r.tag_env}</div>}
                {r.warning && <div className="env-conflict" title={r.warning}>{WARNING_LABEL[r.warning_kind] || "Evidence disagrees"}: {r.warning.replace(/^[a-z_]+:\s*/, "")}</div>}
                <button className="env-link" onClick={() => toggleHistory(r.id)} aria-expanded={!!openHistory[r.id]}>
                  {openHistory[r.id] ? "Hide history" : "History"}
                </button>
              </td>
              {view === "needs_classification" && (
                <td className="env-suggest">
                  {r.suggestion ? (
                    <>
                      <EnvBadge env={r.suggestion.env} /> <span className={`env-conf env-conf-${r.suggestion.confidence}`}>{CONF_LABEL[r.suggestion.confidence]}</span>
                      <div className="env-source">{r.suggestion.reason}</div>
                      {canManage && <button disabled={busy} onClick={() => acceptSuggestions([r])}>Accept</button>}
                    </>
                  ) : <span className="env-muted">No suggestion</span>}
                </td>
              )}
              <td><TagBadge state={r.tag_state} /></td>
              <td className="env-next">
                {nextStep(r, view, canManage)}
                {view === "tagging" && r.missing_tags?.length > 0 && (
                  r.missing_tags.filter(t => t !== "Environment").length > 0 &&
                  <div className="env-missing">Also add for CloudOps: {r.missing_tags.filter(t => t !== "Environment").join(", ")}</div>
                )}
              </td>
            </tr>
            {openHistory[r.id] && (
              <tr className="env-history-row"><td colSpan={(canManage ? 1 : 0) + (view === "needs_classification" ? 6 : 5)}>
                <HistoryPanel state={openHistory[r.id]} />
              </td></tr>
            )}
            </Fragment>
          ))}
          {!data.items.length && (
            <tr><td colSpan={(canManage ? 1 : 0) + (view === "needs_classification" ? 6 : 5)} className="env-empty">
              {view === "needs_classification" ? "Every resource has an environment. Nothing to place."
                : view === "tagging" ? "Every resource that can be checked carries a correct Environment tag."
                  : view === "mismatches" ? "No resource has evidence that disagrees."
                  : "No resources match these filters."}
            </td></tr>
          )}
        </tbody>
      </table>}

      {preview && (
        <DefaultPreview preview={preview} accounts={accounts} busy={busy} onConfirm={confirmDefault} onCancel={() => setPreview(null)} />
      )}

      {plan && (
        <div className="env-modal" role="dialog" aria-modal="true" aria-label="AWS tagging script" onClick={e => { if (e.target === e.currentTarget) setPlan(null); }}>
          <div className="env-modal-box">
            <div className="env-modal-head">
              <strong>AWS tagging script</strong>
              <span>{plan.resources} resource(s), {plan.commands} command(s), {plan.accounts} account(s)</span>
            </div>
            <div className="env-plan-envs">
              {ENVS.filter(k => plan.by_env?.[k]).map(k => (
                <span key={k} className={`env-chip env-${k}`}>{ENV_LABEL[k]} {plan.by_env[k]}</span>
              ))}
            </div>
            {plan.groups?.length > 0 && (
              <table className="env-plan-table">
                <thead><tr><th>Account</th><th>Region</th><th>Environment</th><th>Resources</th></tr></thead>
                <tbody>
                  {plan.groups.map(g => (
                    <tr key={`${g.account_id}${g.region}${g.env}`}>
                      <td>{g.account_name} <span className="env-id">{g.account_id}</span></td>
                      <td>{g.region}</td><td><EnvBadge env={g.env} /></td><td>{g.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <ol className="env-modal-note env-plan-how">
              <li>Download or copy the script. Read it: each resource is listed in a comment above its command.</li>
              <li>Sign in to the AWS console of <strong>each account in the table</strong> and open CloudShell (any region).</li>
              <li>Paste or upload the script and run <code>DRY_RUN=1 bash cloudops-tagging-plan.sh</code> to preview, then <code>bash cloudops-tagging-plan.sh</code>.</li>
              <li>Repeat for the next account. A block only runs in its own account, and the script stops if the account does not match.</li>
            </ol>
            <div className="env-modal-note">
              Needs the ec2:CreateTags permission. It adds or overwrites only the Environment tag and covers EC2 instances and EBS
              volumes. Azure and GCP resources are not included, and tags on other types are done in the console or Tag Editor.
              {Object.keys(plan.not_covered || {}).length > 0 &&
                ` Not covered: ${Object.entries(plan.not_covered).map(([t, n]) => `${t} x${n}`).join(", ")}.`}
            </div>
            <pre className="env-modal-pre">{plan.script}</pre>
            <div className="env-modal-actions">
              <button onClick={async () => { setCopied(await copyText(plan.script)); }}>{copied ? "Copied" : "Copy"}</button>
              <button onClick={() => saveText("cloudops-tagging-plan.sh", plan.script)}>Download</button>
              <button onClick={() => setPlan(null)}>Close</button>
            </div>
          </div>
        </div>
      )}

      {view !== "compliance" && <div className="env-pager">
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
        <span>Page {page} of {pages} ({data.total} resources)</span>
        <button disabled={page >= pages} onClick={() => setOffset(offset + PAGE)}>Next</button>
      </div>}
    </div>
  );
}
