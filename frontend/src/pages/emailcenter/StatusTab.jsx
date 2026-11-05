// Status tab: is email working, what is wrong with it, how many mails went out, the last failure, and a test box.
// It deliberately does NOT repeat the SMTP settings (server, port, sender, link URL, sign-in): they live in the server's
// .env, nobody can change them here, and the API no longer sends them. The username and password were never sent.
import { useCallback, useEffect, useState } from "react";
import { Badge, KpiCard, KpiStrip, Panel, Skeleton } from "../../components/ui";
import { useAuth } from "../../auth/AuthContext";
import { useTimezone } from "../../contexts/TimezoneContext";
import { formatStamp } from "../../utils/timeFormat";
import { smtpProblems } from "../../utils/emailCenterView";
import { getStatus, sendTestEmail, errMsg } from "../../api/emailCenter";

export default function StatusTab() {
  const { hasPermission } = useAuth();
  const canTest = hasPermission("emailcenter.test");
  const canManage = hasPermission("emailcenter.manage");
  const { ianaName, timezone } = useTimezone();
  const [st, setSt] = useState(null);
  const [error, setError] = useState("");
  const [to, setTo] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);

  const load = useCallback(() => {
    getStatus().then(s => { setSt(s); setError(""); }).catch(e => setError(errMsg(e)));
  }, []);
  useEffect(() => { load(); }, [load]);

  const test = async e => {
    e.preventDefault();
    setBusy(true); setResult(null);
    try { setResult(await sendTestEmail(to.trim())); load(); }
    catch (err) { setResult({ ok: false, detail: errMsg(err) }); }
    finally { setBusy(false); }
  };

  if (error) return <div className="ec-error" role="alert">{error}</div>;
  if (!st) return <Skeleton h={160} />;
  const { smtp, delivery } = st;
  const { problems, urlWarning } = smtpProblems(smtp);
  const stamp = v => formatStamp(v, ianaName, timezone);
  const healthy = smtp.configured && !problems.length;

  return (
    <div className="ec-stack">
      {st.migration_pending && <div className="ec-warn" role="alert">{st.migration_note}</div>}
      <Panel title="Email"
        subtitle="Mail settings are kept in the server's .env file and are not shown here"
        actions={<Badge tone={!smtp.configured ? "crit" : healthy ? "ok" : "warn"}>{!smtp.configured ? "Not configured" : healthy ? "Working" : "Needs attention"}</Badge>}>
        {!smtp.configured && <p className="ec-dim">No mail server is set up, so nothing can be sent. Ask the person who runs the server to configure SMTP.</p>}
        {smtp.configured && !st.reports_enabled && <p className="ec-dim">Report generation is off on this environment, so scheduled reports stop before the PDF.</p>}
        {urlWarning && <div className="ec-warn" role="alert">{urlWarning}</div>}
        {canManage && smtp.configured && !smtp.recipient_domains_restricted && (
          <div className="ec-warn" role="alert">Mail can go to any address. Limit it to your company's domains so alert and report content cannot be sent outside (REPORT_EMAIL_ALLOWED_DOMAINS in .env).</div>)}
        {problems.length > 0 && (
          <ul className="ec-problems" aria-label="Configuration problems">{problems.map(p => <li key={p}>{p}</li>)}</ul>
        )}
        {healthy && !urlWarning && <p className="ec-dim">No configuration problems found.</p>}
      </Panel>

      <KpiStrip>
        <KpiCard label="Sent, 24 h" value={delivery["24h"].sent} tone="ok" />
        <KpiCard label="Failed, 24 h" value={delivery["24h"].failed} tone={delivery["24h"].failed ? "crit" : undefined} />
        <KpiCard label="Skipped, 24 h" value={delivery["24h"].skipped} />
        <KpiCard label="Sent, 7 days" value={delivery["7d"].sent} tone="ok" />
        <KpiCard label="Failed, 7 days" value={delivery["7d"].failed} tone={delivery["7d"].failed ? "crit" : undefined} />
        <KpiCard label="Skipped, 7 days" value={delivery["7d"].skipped} />
      </KpiStrip>

      <Panel title="Last failure">
        {st.last_failure
          ? <p className="ec-lastfail"><b>{stamp(st.last_failure.at)}</b> · {st.last_failure.category} mail failed
              {st.last_failure.to && <> to {st.last_failure.to}<br /><span className="ec-dim">{st.last_failure.error || "No reason recorded"}</span></>}
              {!st.last_failure.to && <><br /><span className="ec-dim">The address and the reason are in the delivery log for people who may read it.</span></>}</p>
          : <p className="ec-dim">No failed deliveries on record.</p>}
      </Panel>

      {canTest && (
        <Panel title="Send a test email" subtitle="Sends one real message now and shows the mail server's answer">
          <form className="ec-inline" onSubmit={test}>
            <input className="ui-input" type="email" required placeholder="name@example.com" value={to} onChange={e => setTo(e.target.value)} aria-label="Test recipient" />
            <button className="ui-btn ui-btn-primary" disabled={busy || !to.trim() || !smtp.configured}>{busy ? "Sending…" : "Send test email"}</button>
          </form>
          {!smtp.configured && <p className="ec-dim">Configure SMTP on the server first.</p>}
          {result && <div className={result.ok ? "ec-ok" : "ec-error"} role="status">{result.detail}</div>}
        </Panel>
      )}
    </div>
  );
}
