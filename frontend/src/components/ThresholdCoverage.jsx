// How many enabled metrics can actually raise an alert, and a one-click fix for the ones that are blank only
// because their row predates the shipped default (audit F1). Never overwrites a value a person set.
import { useCallback, useEffect, useState } from "react";
import { Badge, ConfirmDialog } from "./ui";
import { getThresholdCoverage, applyRecommendedThresholds } from "../api/api";

const cmp = c => (c === "<" ? "below" : c === ">=" ? "at or above" : "above");

export default function ThresholdCoverage({ accountId, canConfigure, onApplied }) {
  const [cov, setCov] = useState(null);
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    if (!accountId) return;
    try { setCov(await getThresholdCoverage(accountId)); } catch { setCov(null); }
  }, [accountId]);
  useEffect(() => { setMsg(""); setOpen(false); load(); }, [load]);

  async function apply() {
    setBusy(true);
    try {
      const res = await applyRecommendedThresholds(accountId, false);
      setMsg(`Applied recommended thresholds to ${res.applied} metric${res.applied === 1 ? "" : "s"}.`);
      setConfirm(false); setOpen(false);
      await load(); onApplied?.();
    } catch (e) { setMsg(e.message || "Could not apply"); setConfirm(false); }
    finally { setBusy(false); }
  }

  if (!cov || !cov.enabled_metrics) return null;
  const n = cov.upgradable.length;
  const collectOnly = cov.totals.collect_only;
  return (
    <div style={{ padding: "8px 12px", fontSize: 12, display: "grid", gap: 6 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <strong>Alert coverage:</strong>
        <span>{cov.alerting} of {cov.enabled_metrics} enabled metrics can raise an alert ({cov.coverage_percent}%)</span>
        {n > 0 && <Badge tone="warn">{n} blank with a recommended default</Badge>}
        {collectOnly > 0 && <Badge tone="mute" title="Volume or byte-count metrics with no honest fixed limit. They are watched by anomaly detection only.">{collectOnly} collected for anomaly detection only</Badge>}
        {n > 0 && <button type="button" className="ui-btn" onClick={() => setOpen(o => !o)}>{open ? "Hide" : "Review"}</button>}
      </div>
      {msg && <div role="status" style={{ color: "var(--ok-fg, #34d399)" }}>{msg}</div>}
      {open && n > 0 && (
        <div style={{ border: "1px solid var(--line-1, #1f2937)", borderRadius: 6, padding: 8 }}>
          <div style={{ maxHeight: 220, overflow: "auto" }}>
            {cov.upgradable.map(u => (
              <div key={u.id} style={{ display: "flex", gap: 10, padding: "2px 0" }}>
                <span className="mono" style={{ minWidth: 220 }}>{u.metric}</span>
                <span style={{ minWidth: 90, opacity: .7 }}>{u.service}</span>
                <span>warn {cmp(u.comparison)} {u.warning}, critical {cmp(u.comparison)} {u.critical}</span>
              </div>
            ))}
          </div>
          {canConfigure
            ? <button type="button" className="ui-btn ui-btn-primary" style={{ marginTop: 8 }} onClick={() => setConfirm(true)}>Apply these {n} defaults</button>
            : <div style={{ marginTop: 8, opacity: .7 }}>You need the alerts.configure permission to apply these.</div>}
        </div>
      )}
      <ConfirmDialog open={confirm} busy={busy} title={`Apply ${n} recommended thresholds?`} confirmLabel="Apply"
        body={<>These metrics are collected but have no alert line today. This sets the shipped default for each one. Values you already set are not changed, and you can edit any of them afterwards.</>}
        onConfirm={apply} onCancel={() => !busy && setConfirm(false)} />
    </div>
  );
}
