// SMTP tab: read-only view of the .env SMTP configuration, what is wrong with it, delivery counts, the last failure,
// and a box to send one test email. The username and password are never returned by the API, so never shown.
import { useCallback, useEffect, useState } from "react";
import { Badge, KpiCard, KpiStrip, Panel, Skeleton } from "../../components/ui";
import { useTimezone } from "../../contexts/TimezoneContext";
import { formatStamp } from "../../utils/timeFormat";
import { smtpProblems } from "../../utils/emailCenterView";
import { getStatus, sendTestEmail, errMsg } from "../../api/emailCenter";

export default function SmtpTab({ canManage }) {
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

  return (
    <div className="ec-stack">
      {st.migration_pending && <div className="ec-warn" role="alert">{st.migration_note}</div>}
      <Panel title="SMTP" subtitle="Set in the server's .env file; change it there and restart the service"
        actions={<Badge tone={smtp.configured ? "ok" : "crit"}>{smtp.configured ? "Configured" : "Not configured"}</Badge>}>
        <dl className="ec-facts">
          <dt>Server</dt><dd>{smtp.host ? `${smtp.host}:${smtp.port ?? "?"}` : "—"}</dd>
          <dt>Security</dt><dd>{smtp.security}</dd>
          <dt>Sign-in</dt><dd>{smtp.authenticated ? "Username and password set" : "No username"}</dd>
          <dt>From</dt><dd>{smtp.from_name} &lt;{smtp.from_address || "—"}&gt;</dd>
          <dt>Mail timezone</dt><dd>{smtp.timezone}</dd>
          <dt>Links in email point to</dt><dd>{smtp.public_app_url}</dd>
          <dt>Allowed recipient domains</dt>
          <dd>{smtp.allowed_recipient_domains.length ? smtp.allowed_recipient_domains.map(d => `@${d}`).join(", ") : "Any (REPORT_EMAIL_ALLOWED_DOMAINS is not set)"}</dd>
          <dt>Report generation</dt><dd>{st.reports_enabled ? "Enabled" : "Off on this environment (REPORTS_ENABLED=false): scheduled runs stop before the PDF"}</dd>
        </dl>
        {urlWarning && <div className="ec-warn" role="alert">{urlWarning}</div>}
        {problems.length > 0 && (
          <ul className="ec-problems" aria-label="Configuration problems">{problems.map(p => <li key={p}>{p}</li>)}</ul>
        )}
        {!problems.length && !urlWarning && smtp.configured && <p className="ec-dim">No configuration problems found.</p>}
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
          ? <p className="ec-lastfail"><b>{stamp(st.last_failure.at)}</b> · {st.last_failure.category} to {st.last_failure.to}<br /><span className="ec-dim">{st.last_failure.error || "No reason recorded"}</span></p>
          : <p className="ec-dim">No failed deliveries on record.</p>}
      </Panel>

      {canManage && (
        <Panel title="Send a test email" subtitle="Sends one real message now and shows the mail server's answer">
          <form className="ec-inline" onSubmit={test}>
            <input className="ui-input" type="email" required placeholder="name@example.com" value={to} onChange={e => setTo(e.target.value)} aria-label="Test recipient" />
            <button className="ui-btn ui-btn-primary" disabled={busy || !to.trim() || !smtp.configured}>{busy ? "Sending…" : "Send test email"}</button>
          </form>
          {!smtp.configured && <p className="ec-dim">Set SMTP_HOST (and the other SMTP_ values) in .env and restart the service first.</p>}
          {result && <div className={result.ok ? "ec-ok" : "ec-error"} role="status">{result.detail}</div>}
        </Panel>
      )}
    </div>
  );
}
