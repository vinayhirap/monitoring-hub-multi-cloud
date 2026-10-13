// src/pages/Environments.jsx
// Environments and tags. Two separate things live here, and the page keeps them apart:
//   1. Environment: which tier (Prod / UAT / Dev) CloudOps treats each resource as. Decides alert strength.
//      Resources nothing can place are "Not placed" and wait in the first tab; they are alerted like prod meanwhile.
//   2. AWS tags: whether the resource carries the standard Environment tag in AWS. CloudOps only reads AWS; the
//      second tab hands you a script to run with your own credentials.
// Writes need environments.manage; everything is scoped to the caller's accounts server-side.
import { useState, useEffect, useCallback, useRef } from "react";
import {
  getEnvironmentSummary, getEnvironmentResources, classifyResources, classifyGroup, setAccountDefaultEnv, getTagPlan,
} from "../api/api";
import { copyText } from "../utils/copyText";
import { useAuth } from "../auth/AuthContext";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import "./Environments.css";

const PAGE = 50;
const REFRESH_MS = 60000;
const QUICK_GROUPS = 8;

const ENV_LABEL = { prod: "Prod", uat: "UAT", dev: "Dev", unknown: "Not placed", ignore: "Ignored" };
const ENV_LONG = { prod: "Production", uat: "UAT", dev: "Development" };
const SOURCE_LABEL = {
  manual: "Set by a person", tag: "From the AWS tag", name: "From the name", related: "From the attached resource",
  account: "From the account default", unknown: "Nothing identifies it",
};
const TAG_LABEL = {
  ok: "Correct tag", nonstandard: "Tag needs fixing", invalid: "Unrecognised value",
  missing: "No Environment tag", not_checked: "Cannot be checked",
};
const TAG_HINT = {
  ok: "The Environment tag is there and says exactly prod, uat or dev.",
  nonstandard: "A tag exists but its name or value is not the standard one (for example Env=Production).",
  invalid: "An environment tag exists but its value is not prod, uat or dev.",
  missing: "The resource has no Environment tag in AWS.",
  not_checked: "CloudOps does not read AWS tags for this type of resource (certificates and backups, for example), so it cannot tell.",
};
const TAG_ORDER = ["ok", "nonstandard", "invalid", "missing", "not_checked"];
const TAG_PROBLEMS = ["missing", "nonstandard", "invalid"];

function EnvBadge({ env }) {
  if (!env) return <span className="env-badge env-none">Not placed</span>;
  return <span className={`env-badge env-${env}`}>{ENV_LABEL[env] || env}</span>;
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
    return canManage ? "Mark it Prod, UAT or Dev." : "Ask someone with edit access to place it.";
  }
  return r.reason || "";
}

export default function Environments() {
  const { hasPermission } = useAuth();
  const canManage = hasPermission("environments.manage");
  const [view, setView] = useState("needs_classification");
  const [summary, setSummary] = useState(null);
  const [data, setData] = useState({ items: [], total: 0 });
  const [offset, setOffset] = useState(0);
  const [filters, setFilters] = useState({ account_id: "", q: "", env: "", source: "", tag_state: "" });
  const [plan, setPlan] = useState(null);
  const [copied, setCopied] = useState(false);
  const [selected, setSelected] = useState(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [qText, setQText] = useState("");
  const reqId = useRef(0);

  // Only the newest request may update the screen: typing in the search box or paging quickly sends several,
  // and an older (slower) answer must not overwrite a newer one.
  const load = useCallback(() => {
    const mine = ++reqId.current;
    getEnvironmentSummary().then(d => { if (reqId.current === mine) setSummary(d); })
      .catch(e => { if (reqId.current === mine) setError(e.message); });
    getEnvironmentResources({ view, ...filters, limit: PAGE, offset })
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
  useAutoRefresh(() => { if (!selected.size && !busy && !plan) load(); }, REFRESH_MS);
  useEffect(() => { setSelected(new Set()); }, [view, filters, offset]);
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
  const switchView = v => { setView(v); setOffset(0); setFilters(f => ({ ...f, env: "", source: "", tag_state: "" })); };
  // a tag tile opens the full list filtered to that tag status
  const showTagState = k => { setView("all"); setOffset(0); setFilters(f => ({ ...f, env: "", source: "", tag_state: k })); };

  const openPlan = async () => {
    setBusy(true); setNotice(null);
    try { setPlan(await getTagPlan({ account_id: filters.account_id })); setCopied(false); }
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

  const changeDefault = async (accountId, env) => {
    const acct = summary?.accounts.find(a => a.account_id === accountId);
    const name = acct?.account_name || "this account";
    const was = acct?.default_env ? ENV_LONG[acct.default_env] : null;
    const msg = env
      ? `Set ${name}'s default environment to ${ENV_LONG[env]}?\n\n`
        + `Every resource in this account that has no manual choice, tag or recognised name will be labelled ${ENV_LONG[env]} `
        + `and leave the "Not placed" list (${acct?.needs_classification ?? 0} are in it now). `
        + (was ? `Ones that were relying on the current default (${was}) change to ${ENV_LONG[env]}. ` : "")
        + "Alerting is unchanged unless environment-aware classification is switched on."
      : `Clear ${name}'s default environment?\n\n`
        + `Resources that only had the default${was ? ` (${was})` : ""} go back to "Not placed" and into the first tab.`;
    if (!window.confirm(msg)) return;
    setBusy(true); setNotice(null);
    try {
      const r = await setAccountDefaultEnv(accountId, env || null);
      setNotice(`Account default ${env ? `set to ${ENV_LONG[env]}` : "cleared"}; ${r.reclassified} resource(s) re-evaluated.${r.warning ? ` ${r.warning}` : ""}`);
      load();
    } catch (e) { setError(e.message); }
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
          <dd>The tier a resource belongs to: Prod (live), UAT (final testing, also pre-prod, staging and QA) or Dev (building and testing).</dd>
          <dt>Not placed</dt>
          <dd>CloudOps could not tell the tier from a tag, the name or what the resource is attached to. It is alerted as if it were Prod until someone places it.</dd>
          <dt>Environment tag</dt>
          <dd>A label named Environment, with the value prod, uat or dev, on the resource in AWS. It is the most reliable source, and other teams can see it too.</dd>
          <dt>Account default</dt>
          <dd>A fallback for resources with no tag and no name hint. Use it only when everything untagged in an account is the same tier.</dd>
        </dl>
      </details>

      {error && <div className="env-error" role="alert">{error}</div>}
      {notice && <div className="env-notice" role="status">{notice}</div>}

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
              {["prod", "uat", "dev"].map(k => <span key={k} className={`env-chip env-${k}`}>{ENV_LABEL[k]} {byEnv[k] || 0}</span>)}
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
                <div className="env-account-name">{a.account_name}</div>
                <div className="env-account-meta">{a.total} resources, {a.needs_classification} not placed</div>
                <label className="env-field">
                  <span>Default environment</span>
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
        </section>
      )}

      <div className="env-tabs" role="tablist">
        <button role="tab" aria-selected={view === "needs_classification"} className={view === "needs_classification" ? "active" : ""} onClick={() => switchView("needs_classification")}>
          1. Place resources{summary ? ` (${needs})` : ""}
        </button>
        <button role="tab" aria-selected={view === "tagging"} className={view === "tagging" ? "active" : ""} onClick={() => switchView("tagging")}>
          2. Fix AWS tags{summary ? ` (${fixable})` : ""}
        </button>
        <button role="tab" aria-selected={view === "all"} className={view === "all" ? "active" : ""} onClick={() => switchView("all")}>All resources</button>
      </div>

      {view === "needs_classification" && (
        <div className="env-lead">
          These resources have no Environment tag and nothing in their name says which tier they are. They are alerted
          as if they were Prod until you place them. Placing a resource here changes CloudOps only, not AWS.
        </div>
      )}

      {view === "needs_classification" && canManage && quickGroups.length > 0 && (
        <section className="env-quick">
          <h2>Fastest way: place a whole group at once</h2>
          <p>Each line is every resource of one type in one account that is still waiting. Types such as certificates and backups
            usually belong to a single tier, so one click can place dozens of them.</p>
          {quickGroups.slice(0, QUICK_GROUPS).map(g => (
            <div key={`${g.account_id}:${g.resource_type}`} className="env-quick-row">
              <div className="env-quick-what">
                <strong>{g.resource_type}</strong> in {g.account_name}
                <span className="env-quick-n">{g.n} waiting</span>
              </div>
              <div className="env-quick-actions">
                {["prod", "uat", "dev"].map(e => (
                  <button key={e} disabled={busy} onClick={() => applyGroup(g, e)}>Mark all {ENV_LABEL[e]}</button>
                ))}
              </div>
            </div>
          ))}
          {quickGroups.length > QUICK_GROUPS && (
            <div className="env-quick-more">{quickGroups.length - QUICK_GROUPS} more group(s) are in the list below.</div>
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
            <div><button disabled={busy} onClick={openPlan}>Generate AWS tagging script</button></div>
          </li>
          <li>
            <strong>Run it in AWS</strong> with credentials for that account (AWS CloudShell works). CloudOps never changes anything
            in AWS itself. The counts above update after the next discovery run.
          </li>
        </ol>
      )}

      <div className="env-filters">
        <select aria-label="Account" value={filters.account_id} onChange={e => setFilter("account_id", e.target.value)}>
          <option value="">All accounts</option>
          {accounts.map(a => <option key={a.account_id} value={a.account_id}>{a.account_name}</option>)}
        </select>
        {view === "all" && (
          <>
            <select aria-label="Environment" value={filters.env} onChange={e => setFilter("env", e.target.value)}>
              <option value="">Any environment</option>
              {["prod", "uat", "dev", "unknown", "ignore", "unclassified"].map(v =>
                <option key={v} value={v}>{ENV_LABEL[v] || "Not yet evaluated"}</option>)}
            </select>
            <select aria-label="How it was decided" value={filters.source} onChange={e => setFilter("source", e.target.value)}>
              <option value="">Any source</option>
              {Object.entries(SOURCE_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
          </>
        )}
        {(view === "all" || view === "tagging") && (
          <select aria-label="Tag status" value={filters.tag_state} onChange={e => setFilter("tag_state", e.target.value)}>
            <option value="">{view === "tagging" ? "Any tag problem" : "Any tag status"}</option>
            {(view === "tagging" ? TAG_PROBLEMS : TAG_ORDER).map(k => <option key={k} value={k}>{TAG_LABEL[k]}</option>)}
          </select>
        )}
        <input placeholder="Search name or ID" aria-label="Search name or ID" value={qText} onChange={e => setQText(e.target.value)} />
      </div>

      {canManage && (
        <div className="env-bulk">
          <span>{selected.size} selected</span>
          {["prod", "uat", "dev", "ignore"].map(e => (
            <button key={e} disabled={!selected.size || busy} onClick={() => apply(e)}
                    title={e === "ignore" ? "Stop listing it as waiting. It is still alerted like Prod." : undefined}>
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
            <th>Resource</th><th>Type</th><th>Account</th>
            <th>Environment</th><th>AWS tag</th><th>{view === "all" ? "Why" : "What to do"}</th>
          </tr>
        </thead>
        <tbody>
          {data.items.map(r => (
            <tr key={r.id}>
              {canManage && <td><input type="checkbox" checked={selected.has(r.id)} onChange={() => toggle(r.id)} aria-label={`Select ${r.name || r.resource_id}`} /></td>}
              <td>
                <div className="env-name">{r.name || r.resource_id}</div>
                <div className="env-id">{r.name ? `${r.resource_id}` : ""}{r.region ? `${r.name ? " | " : ""}${r.region}` : ""}</div>
              </td>
              <td>{r.resource_type}</td>
              <td>{r.account_name}</td>
              <td>
                <EnvBadge env={r.env} />
                <div className="env-source">{SOURCE_LABEL[r.source] || "Not evaluated yet"}{r.override_by ? ` (${r.override_by})` : ""}</div>
                {r.conflict && <div className="env-conflict">AWS tag says {ENV_LONG[r.tag_env] || r.tag_env}</div>}
              </td>
              <td><TagBadge state={r.tag_state} /></td>
              <td className="env-next">
                {nextStep(r, view, canManage)}
                {view === "tagging" && r.missing_tags?.length > 0 && (
                  <div className="env-missing">Other standard tags missing: {r.missing_tags.filter(t => t !== "Environment").join(", ") || "none"}</div>
                )}
              </td>
            </tr>
          ))}
          {!data.items.length && (
            <tr><td colSpan={canManage ? 7 : 6} className="env-empty">
              {view === "needs_classification" ? "Every resource has an environment. Nothing to place."
                : view === "tagging" ? "Every resource that can be checked carries a correct Environment tag."
                  : "No resources match these filters."}
            </td></tr>
          )}
        </tbody>
      </table>

      {plan && (
        <div className="env-modal" role="dialog" aria-modal="true" aria-label="AWS tagging script" onClick={e => { if (e.target === e.currentTarget) setPlan(null); }}>
          <div className="env-modal-box">
            <div className="env-modal-head">
              <strong>AWS tagging script</strong>
              <span>{plan.resources} resource(s), {plan.commands} command(s), {plan.accounts} account(s)</span>
            </div>
            <div className="env-modal-note">
              How to use it: review the script, open AWS CloudShell (or a terminal) signed in to the account named in each
              block, then paste and run that block. Each block first checks that you are in the right account and stops if not.
              Each command adds or overwrites the Environment tag and leaves all other tags alone. It covers EC2 instances and
              EBS volumes only.
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

      <div className="env-pager">
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
        <span>Page {page} of {pages} ({data.total} resources)</span>
        <button disabled={page >= pages} onClick={() => setOffset(offset + PAGE)}>Next</button>
      </div>
    </div>
  );
}
