// src/pages/access/ReviewsTab.jsx
//
// Access reviews (periodic attestation). A review is a signed record that
// someone looked at a user's/group's access and decided retain / revoke /
// modify. It never changes access by itself; the optional "also revoke the
// binding" box does that in the same audited action.
import { useMemo, useState } from "react";
import { useAuth } from "../../auth/AuthContext";
import * as api from "../../api/access";
import { PlusIcon, DownloadIcon, ClipboardIcon } from "../../components/icons";
import { Badge, Modal, Field, Banner, Empty, SkeletonRows, useToast, useAsync, timeAgo } from "./ui";

const DECISION_TONE = { retain: "green", revoke: "red", modify: "yellow" };

function ReviewModal({ users, groups, bindings, onClose, onSaved }) {
  const toast = useToast();
  const [p, setP] = useState({ type: "user", id: "" });
  const [f, setF] = useState({ binding: "", decision: "retain", notes: "", revoke: false });
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const mine = useMemo(() => bindings.filter((b) => b.principal_type === p.type && String(b.principal_id) === String(p.id)), [bindings, p]);
  const canRevoke = f.decision === "revoke" && f.binding;

  async function submit() {
    if (!p.id) return setErr("Choose who this review is about");
    setBusy(true);
    try {
      const r = await api.createReview({
        principal_type: p.type, principal_id: Number(p.id), binding_id: f.binding ? Number(f.binding) : undefined,
        decision: f.decision, notes: f.notes.trim() || undefined, revoke_binding: canRevoke && f.revoke,
      });
      toast(r.binding_revoked ? "Review recorded and binding revoked" : "Review recorded");
      onSaved(); onClose();
    } catch (e) { setErr(api.errMsg(e)); setBusy(false); }
  }
  return (
    <Modal title="Record an access review" onClose={onClose} busy={busy}
      footer={<><button className="ac-btn ghost" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="ac-btn primary" onClick={submit} disabled={busy}>Record review</button></>}>
      {err && <Banner tone="err">{err}</Banner>}
      <div className="ac-grid2">
        <Field label="Reviewing"><select value={p.type} onChange={(e) => { setP({ type: e.target.value, id: "" }); setF({ ...f, binding: "" }); }}>
          <option value="user">A user</option><option value="group">A group</option></select></Field>
        <Field label={p.type === "user" ? "User" : "Group"}><select value={p.id} onChange={(e) => { setP({ ...p, id: e.target.value }); setF({ ...f, binding: "" }); }}>
          <option value="">Select…</option>
          {(p.type === "user" ? users : groups).map((x) => <option key={x.id} value={x.id}>{x.username || x.name}</option>)}</select></Field>
      </div>
      <Field label="Specific binding (optional)" hint={p.id && mine.length === 0 ? "This principal has no role bindings — the review covers their overall access." : undefined}>
        <select value={f.binding} onChange={(e) => setF({ ...f, binding: e.target.value, revoke: false })} disabled={!mine.length}>
          <option value="">Overall access</option>
          {mine.map((b) => <option key={b.id} value={b.id}>{b.role_name} @ {b.scope_label}</option>)}</select></Field>
      <Field label="Decision"><select value={f.decision} onChange={(e) => setF({ ...f, decision: e.target.value, revoke: false })}>
        <option value="retain">Retain — access is still appropriate</option><option value="modify">Modify — access should change</option><option value="revoke">Revoke — access should be removed</option></select></Field>
      {canRevoke && (
        <label className="ac-check"><input type="checkbox" checked={f.revoke} onChange={(e) => setF({ ...f, revoke: e.target.checked })} />
          Also remove this binding now</label>
      )}
      <Field label="Notes (optional)" hint="Up to 500 characters"><textarea rows={3} maxLength={500} value={f.notes} onChange={(e) => setF({ ...f, notes: e.target.value })} /></Field>
      {f.decision !== "retain" && !f.revoke && <Banner tone="info">Recording this decision doesn't change access by itself. Apply the change from Users, Groups or Advanced.</Banner>}
    </Modal>
  );
}

function toCsv(rows) {
  const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const head = ["Reviewed at", "Reviewer", "Principal type", "Principal", "Binding", "Decision", "Notes"];
  return [head, ...rows.map((r) => [r.reviewed_at, r.reviewed_by_username, r.principal_type, r.principal_name, r.binding_label, r.decision, r.notes])]
    .map((r) => r.map(esc).join(",")).join("\n");
}

export default function ReviewsTab() {
  const { hasPermission } = useAuth();
  const canReview = hasPermission("rbac.review.conduct");
  const reviews = useAsync(api.listReviews, []);
  const users = useAsync(() => api.listUsers().catch(() => []), []);
  const groups = useAsync(() => api.listGroups().catch(() => []), []);
  const bindings = useAsync(() => api.listBindings().catch(() => []), []);
  const [add, setAdd] = useState(false);
  const [filter, setFilter] = useState("all");

  const rows = (reviews.data || []).filter((r) => filter === "all" || r.decision === filter);
  function download() {
    const blob = new Blob([toCsv(rows)], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `access-reviews-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  return (
    <div>
      <div className="ac-toolbar">
        <p className="ac-lede">A review is an attestation log — it records a decision about someone's access; it doesn't remove access on its own.</p>
        <select value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter by decision">
          <option value="all">All decisions</option><option value="retain">Retain</option><option value="modify">Modify</option><option value="revoke">Revoke</option></select>
        <button className="ac-btn ghost" onClick={download} disabled={!rows.length}><DownloadIcon size={13} /> Export CSV</button>
        {canReview && <button className="ac-btn primary" onClick={() => setAdd(true)}><PlusIcon size={14} /> Record review</button>}
      </div>
      {reviews.error && <Banner tone="err" action={<button className="ac-link" onClick={reviews.reload}>Retry</button>}>{api.errMsg(reviews.error)}</Banner>}
      {reviews.loading && !reviews.data && <SkeletonRows n={4} />}
      {reviews.data && (rows.length === 0
        ? <Empty icon={<ClipboardIcon size={28} />} title="No reviews recorded" hint="Record periodic reviews so you can show who attested to whose access, and when." />
        : <div className="ac-table-wrap"><table className="ac-table">
            <thead><tr><th>When</th><th>Reviewer</th><th>Principal</th><th>Scope of review</th><th>Decision</th><th>Notes</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.id}>
                <td className="muted" title={r.reviewed_at}>{timeAgo(r.reviewed_at)}</td>
                <td>{r.reviewed_by_username || <span className="muted">unknown</span>}</td>
                <td><Badge tone={r.principal_type === "user" ? "teal" : "purple"}>{r.principal_type}</Badge> {r.principal_name || <span className="muted">removed</span>}</td>
                <td className="small">{r.binding_label || (r.binding_id ? "Binding removed" : "Overall access")}</td>
                <td><Badge tone={DECISION_TONE[r.decision]}>{r.decision.toUpperCase()}</Badge></td>
                <td className="muted small">{r.notes || "—"}</td>
              </tr>))}</tbody></table></div>)}
      {add && <ReviewModal users={users.data || []} groups={groups.data || []} bindings={bindings.data || []} onClose={() => setAdd(false)} onSaved={() => { reviews.reload(); bindings.reload(); }} />}
    </div>
  );
}
