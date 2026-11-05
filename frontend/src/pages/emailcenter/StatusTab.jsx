// Status tab: how many mails went out, anything that needs attention, the last failure, and a test box.
// It deliberately does NOT repeat the SMTP settings (server, port, sender, link URL, sign-in): nobody can change them
// here, and the API no longer sends them. A healthy mail setup shows nothing but the numbers; a card appears only when
// something is wrong or worth knowing.
import { useCallback, useEffect, useState } from "react";
import { KpiCard, KpiStrip, Panel, Skeleton } from "../../components/ui";
import { useAuth } from "../../auth/AuthContext";
import { useTimezone } from "../../contexts/TimezoneContext";
import { formatStamp } from "../../utils/timeFormat";
import { smtpProblems } from "../../utils/emailCenterView";
import { getStatus, sendTestEmail, errMsg } from "../../api/emailCenter";

export default function StatusTab() {
  const { hasPermission } = useAuth();
  const canTest = hasPermission("emailcenter.test");
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
  const { problems } = smtpProblems(smtp);      // the plain-http link warning is filtered out: it is a deployment matter, not mail health
  const stamp = v => formatStamp(v, ianaName, timezone);
  const d24 = delivery["24h"], d7 = delivery["7d"];
  const notConfigured = !smtp.configured;
  const needsAttention = notConfigured || problems.length > 0;
  const anySkipped = d24.skipped > 0 || d7.skipped > 0;

  return (
    <div className="ec-stack">
      {st.migration_pending && <div className="ec-warn" role="alert">{st.migration_note}</div>}

      {needsAttention && (
        <Panel title={notConfigured ? "Email is not set up" : "Email needs attention"} tone={notConfigured ? "crit" : "warn"}>
          {notConfigured && <p className="ec-dim">No mail server is set up, so nothing can be sent. Ask the person who runs the server to configure SMTP.</p>}
          {problems.length > 0 && (
            <ul className="ec-problems" aria-label="Configuration problems">{problems.map(p => <li key={p}>{p}</li>)}</ul>
          )}
        </Panel>
      )}

      <KpiStrip>
        <KpiCard label="Sent (24 h)" value={d24.sent} tone="ok" sub={`7 days: ${d7.sent}`} />
        <KpiCard label="Failed (24 h)" value={d24.failed} tone={d24.failed ? "crit" : undefined} sub={`7 days: ${d7.failed}`} />
        {anySkipped && <KpiCard label="Skipped (24 h)" value={d24.skipped} sub={`7 days: ${d7.skipped}`} hint="Mails that were not sent because email or reports are switched off on this environment" />}
      </KpiStrip>

      {smtp.configured && !st.reports_enabled && <p className="ec-dim">Report generation is off on this environment, so scheduled reports stop before the PDF.</p>}

      {st.last_failure && (
        <Panel title="Last failure">
          <p className="ec-lastfail"><b>{stamp(st.last_failure.at)}</b> · {st.last_failure.category} mail failed
            {st.last_failure.to && <> to {st.last_failure.to}<br /><span className="ec-dim">{st.last_failure.error || "No reason recorded"}</span></>}
            {!st.last_failure.to && <><br /><span className="ec-dim">The address and the reason are in the delivery log for people who may read it.</span></>}</p>
        </Panel>
      )}

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
