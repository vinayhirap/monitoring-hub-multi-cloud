// "How to read alerts" guide: severity colours, the priority table and how often metrics are checked.
// Plain static help, no dependency on alert data. The priority section only shows once priorities are switched on
// server-side (same endpoint and flag as PriorityStrip), so deploying this changes nothing for a site without them.
// The polling numbers mirror app/collector/polling_model.py TIER_SECONDS (docs/POLLING_AUDIT_2026-10-19.md).
import { useEffect, useState } from "react";

const SEVERITY = [
  { cls: "ag-dot-crit", name: "Critical (red)", text: "A critical line was crossed, or a core availability signal failed. Act now." },
  { cls: "ag-dot-warn", name: "Warning (amber)", text: "A warning line was crossed on a PROD resource, or on one that has not been placed in an environment yet. Look at it soon." },
  { cls: "ag-dot-info", name: "Info (blue)", text: "The same kind of breach on a NON-PROD resource. It is a heads-up, not a page: test systems are allowed to misbehave. If a resource shows Info and should be PROD, set its Environment on the Environments page." },
  { cls: "ag-dot-ok", name: "Resolved (green)", text: "The metric came back inside its limits and the alert closed." },
];

const PRIORITY = [
  { env: "PROD", crit: "P1", other: "P2" },
  { env: "NON-PROD", crit: "P3", other: "P4" },
];

const POLLING = [
  { tier: "Critical", every: "2 min", what: "Availability and error signals (database CPU and connections, load balancer 5xx, VM and service availability).", eg: "RDS connections, ALB 5xx" },
  { tier: "Standard", every: "5 min", what: "Alertable signals, and anything the cloud itself only publishes every 5 minutes.", eg: "EC2 CPU, Lambda errors, queue depth" },
  { tier: "Low", every: "15 min", what: "Trends, capacity and slower-moving failure signals.", eg: "EBS I/O, replication lag, API latency" },
  { tier: "Extended", every: "60 min", what: "Informational numbers of less critical services (AWS and GCP; Azure uses 15 min).", eg: "Message counts, storage sizes" },
  { tier: "Daily", every: "24 h", what: "Values the cloud only publishes once a day.", eg: "S3 bucket size, certificate expiry" },
];

export default function AlertGuide({ accountId = "" }) {
  const [prio, setPrio] = useState(false);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const res = await fetch(`/api/alerts/priority-summary${accountId ? `?account_id=${accountId}` : ""}`, { credentials: "include" });
        if (res.ok && live) setPrio(Boolean((await res.json()).enabled));
      } catch {
        // help text only: stay hidden on error
      }
    })();
    return () => { live = false; };
  }, [accountId]);

  return (
    <details className="ag">
      <summary>How to read alerts: colours, priority and how often data is checked</summary>

      <h3>What the colours mean</h3>
      <ul className="ag-list">
        {SEVERITY.map(s => (
          <li key={s.name}><i className={`ag-dot ${s.cls}`} /><span><strong>{s.name}.</strong> {s.text}</span></li>
        ))}
      </ul>

      {prio && (
        <>
          <h3>How urgent is the alert (priority)</h3>
          <p className="ag-note">Priority comes from the environment and the severity together. A resource tagged Criticality=critical goes up one level (never above P1). Resources not yet placed in an environment are ranked like PROD.</p>
          <table className="ag-table">
            <thead><tr><th>Environment</th><th>Critical alert</th><th>Warning or info alert</th></tr></thead>
            <tbody>
              {PRIORITY.map(r => (
                <tr key={r.env}>
                  <td>{r.env}</td>
                  <td><span className={`prio-chip prio-p${r.crit.slice(1)}`}>{r.crit}</span></td>
                  <td><span className={`prio-chip prio-p${r.other.slice(1)}`}>{r.other}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <h3>How often CloudOps checks a metric</h3>
      <p className="ag-note">
        {prio ? "Priority says who is told first; it does not change how often data is collected. " : ""}
        How often a metric is checked depends on the metric itself: urgent signals are checked every few minutes, trends and
        informational numbers less often, because checking them faster would not find a problem sooner. An alert can take a few checks
        in a row to confirm before it opens, so that one stray reading does not raise a false alarm. The polling shown on each metric chart is
        the exact interval for that metric.
      </p>
      <table className="ag-table">
        <thead><tr><th>Group</th><th>Checked every</th><th>What is in it</th><th>Examples</th></tr></thead>
        <tbody>
          {POLLING.map(p => (
            <tr key={p.tier}><td>{p.tier}</td><td>{p.every}</td><td>{p.what}</td><td>{p.eg}</td></tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}
