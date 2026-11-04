// Delivery log tab: every email handed to SMTP, newest first, filterable, paged with before_id (the id of the last row
// shown). Timestamps arrive in UTC with a Z suffix and are shown in the zone chosen in the top bar.
import { useCallback, useEffect, useState } from "react";
import { Badge, DataTable, EmptyState, Panel, Skeleton } from "../../components/ui";
import { useTimezone } from "../../contexts/TimezoneContext";
import { formatStamp } from "../../utils/timeFormat";
import { deliveryTone, nextBeforeId } from "../../utils/emailCenterView";
import { errMsg, getDeliveryLog } from "../../api/emailCenter";

const PAGE = 50;
const STATUSES = ["", "sent", "failed", "skipped"];
const CATEGORIES = ["", "alert", "report", "escalation", "test", "system"];

export default function DeliveryLogTab() {
  const { ianaName, timezone } = useTimezone();
  const [status, setStatus] = useState("");
  const [category, setCategory] = useState("");
  const [rows, setRows] = useState(null);
  const [more, setMore] = useState(null);          // before_id for the next page, or null
  const [error, setError] = useState("");
  const [loadingMore, setLoadingMore] = useState(false);

  const load = useCallback(() => {
    setRows(null); setError("");
    getDeliveryLog({ status, category, limit: PAGE })
      .then(r => { setRows(r); setMore(nextBeforeId(r, PAGE)); })
      .catch(e => setError(errMsg(e)));
  }, [status, category]);
  useEffect(() => { load(); }, [load]);

  const older = async () => {
    setLoadingMore(true);
    try {
      const r = await getDeliveryLog({ status, category, beforeId: more, limit: PAGE });
      setRows(prev => [...prev, ...r]); setMore(nextBeforeId(r, PAGE));
    } catch (e) { setError(errMsg(e)); } finally { setLoadingMore(false); }
  };

  const columns = [
    { key: "created_at", header: "When", render: r => formatStamp(r.created_at, ianaName, timezone) },
    { key: "status", header: "Status", render: r => <Badge tone={deliveryTone(r.status)}>{r.status}</Badge> },
    { key: "category", header: "Category" },
    { key: "to_addr", header: "To" },
    { key: "subject", header: "Subject", render: r => r.subject || <span className="ec-dim">—</span> },
    { key: "error", header: "Reason", render: r => r.error ? <span className="ec-reason">{r.error}</span> : <span className="ec-dim">—</span> },
  ];

  return (
    <Panel title="Delivery log" subtitle="Newest first. Rows are kept for the configured retention period (90 days by default)"
      actions={<button className="ui-btn" onClick={load}>Refresh</button>}>
      <div className="ec-filters">
        <label>Status
          <select className="ui-input" value={status} onChange={e => setStatus(e.target.value)}>
            {STATUSES.map(s => <option key={s} value={s}>{s || "All"}</option>)}
          </select>
        </label>
        <label>Category
          <select className="ui-input" value={category} onChange={e => setCategory(e.target.value)}>
            {CATEGORIES.map(c => <option key={c} value={c}>{c || "All"}</option>)}
          </select>
        </label>
      </div>
      {error && <div className="ec-error" role="alert">{error}</div>}
      {!rows && !error && <Skeleton h={120} />}
      {rows && (
        <>
          <DataTable columns={columns} rows={rows} rowKey={r => r.id} dense
            empty={<EmptyState title="Nothing in the log" body="No email matches these filters." />} />
          {more && <div className="ec-more"><button className="ui-btn" onClick={older} disabled={loadingMore}>{loadingMore ? "Loading…" : "Older"}</button></div>}
        </>)}
    </Panel>
  );
}
