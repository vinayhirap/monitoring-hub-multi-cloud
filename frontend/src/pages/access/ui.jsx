// src/pages/access/ui.jsx
//
// Small shared primitives for the Access Control page. Everything the old
// UserManagement.jsx and RbacAdmin.jsx each re-implemented (modal, field,
// badge, avatar, confirm, toast) lives here exactly once.
import { useEffect, useRef, useState, useCallback, createContext, useContext } from "react";
import { XIcon, AlertTriangleIcon, CheckCircleIcon } from "../../components/icons";

/* ── time helpers ─────────────────────────────────────────────── */
export function timeAgo(iso) {
  if (!iso) return "Never";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "Just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}
export function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}
export function expiryLabel(iso) {
  if (!iso) return { text: "No expiry", tone: "muted" };
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return { text: "Expired", tone: "red" };
  const d = Math.ceil(ms / 86400000);
  return { text: d <= 1 ? "Expires today" : `Expires in ${d}d`, tone: d <= 7 ? "yellow" : "muted" };
}

/* ── Toasts ───────────────────────────────────────────────────── */
const ToastCtx = createContext(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }) {
  const [items, setItems] = useState([]);
  const push = useCallback((message, tone = "ok") => {
    const id = Math.random().toString(36).slice(2);
    setItems((cur) => [...cur, { id, message, tone }]);
    setTimeout(() => setItems((cur) => cur.filter((t) => t.id !== id)), tone === "err" ? 6000 : 3200);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="ac-toasts" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`ac-toast ${t.tone}`}>
            {t.tone === "err" ? <AlertTriangleIcon size={14} /> : <CheckCircleIcon size={14} />}
            <span>{t.message}</span>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

/* ── Modal (Esc to close, focus trap-lite, click-outside) ─────── */
export function Modal({ title, onClose, children, footer, width = 480, busy = false }) {
  const ref = useRef(null);
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape" && !busy) onClose(); };
    document.addEventListener("keydown", onKey);
    const first = ref.current?.querySelector("input,select,textarea,button:not(.ac-x)");
    first?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, busy]);
  return (
    <div className="ac-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div className="ac-modal" style={{ maxWidth: width }} role="dialog" aria-modal="true" aria-label={title} ref={ref}>
        <div className="ac-modal-head">
          <h3>{title}</h3>
          <button className="ac-x" onClick={onClose} disabled={busy} aria-label="Close"><XIcon size={16} /></button>
        </div>
        <div className="ac-modal-body">{children}</div>
        {footer && <div className="ac-modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

// NOTE (delete-confirm label): .ac-label upper-cases its text, which used to display
// the username as "TESTVIEWER" while the check below is case-sensitive -- so typing what
// was shown left Delete disabled. The name is a literal span (.ac-literal) in its real case.
export function ConfirmDialog({ title, body, confirmLabel = "Confirm", danger = false, onConfirm, onClose, requireText }) {
  const [busy, setBusy] = useState(false);
  const [typed, setTyped] = useState("");
  const blocked = requireText && typed.trim() !== requireText;
  async function go() {
    setBusy(true);
    try { await onConfirm(); onClose(); } finally { setBusy(false); }
  }
  return (
    <Modal title={title} onClose={onClose} busy={busy} width={440}
      footer={<>
        <button className="ac-btn ghost" onClick={onClose} disabled={busy}>Cancel</button>
        <button className={`ac-btn ${danger ? "danger" : "primary"}`} onClick={go} disabled={busy || blocked}>
          {busy ? "Working…" : confirmLabel}
        </button>
      </>}>
      <div className="ac-confirm-body">{body}</div>
      {requireText && (
        <Field label={<>Type "<span className="ac-literal">{requireText}</span>" to confirm</>}>
          <input value={typed} onChange={(e) => setTyped(e.target.value)}
            autoComplete="off" autoCapitalize="off" autoCorrect="off" spellCheck={false} />
        </Field>
      )}
    </Modal>
  );
}

/* ── Form bits ────────────────────────────────────────────────── */
export function Field({ label, hint, error, children }) {
  return (
    <label className={`ac-field${error ? " has-err" : ""}`}>
      <span className="ac-label">{label}</span>
      {children}
      {error ? <span className="ac-err">{error}</span> : hint ? <span className="ac-hint">{hint}</span> : null}
    </label>
  );
}

export function Banner({ tone = "info", children, action }) {
  return (
    <div className={`ac-banner ${tone}`}>
      <span>{children}</span>
      {action}
    </div>
  );
}

export function Badge({ tone = "muted", children, title }) {
  return <span className={`ac-badge ${tone}`} title={title}>{children}</span>;
}

export const ROLE_TONE = { admin: "orange", editor: "purple", viewer: "blue" };
export function RoleBadge({ role }) {
  return <Badge tone={ROLE_TONE[role] || "teal"}>{String(role || "").toUpperCase()}</Badge>;
}

export function Avatar({ name, dim = false }) {
  const letters = (name || "?").replace(/[^A-Za-z0-9 ]/g, " ").trim().split(/\s+/).slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "?";
  let h = 0;
  for (const c of String(name)) h = (h * 31 + c.charCodeAt(0)) % 360;
  return <span className={`ac-avatar${dim ? " dim" : ""}`} style={{ "--h": h }}>{letters}</span>;
}

export function Empty({ icon, title, hint, action }) {
  return (
    <div className="ac-empty">
      {icon}
      <strong>{title}</strong>
      {hint && <p>{hint}</p>}
      {action}
    </div>
  );
}

export function SkeletonRows({ n = 4 }) {
  return <div className="ac-skel">{Array.from({ length: n }).map((_, i) => <div key={i} className="ac-skel-row" />)}</div>;
}

/* ── Row action menu ──────────────────────────────────────────── */
export function KebabMenu({ items, label = "Actions" }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const off = (e) => { if (!ref.current?.contains(e.target)) setOpen(false); };
    const esc = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", off);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", off); document.removeEventListener("keydown", esc); };
  }, [open]);
  const visible = items.filter(Boolean);
  if (!visible.length) return null;
  return (
    <div className="ac-menu" ref={ref}>
      <button className="ac-btn ghost icon" onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }} aria-haspopup="menu" aria-expanded={open} aria-label={label}>⋯</button>
      {open && (
        <div className="ac-menu-pop" role="menu">
          {visible.map((it) => it.divider
            ? <hr key={it.key} />
            : (
              <button key={it.key} role="menuitem" className={it.danger ? "danger" : ""} disabled={it.disabled}
                onClick={(e) => { e.stopPropagation(); setOpen(false); it.onClick(); }}>
                {it.label}
              </button>
            ))}
        </div>
      )}
    </div>
  );
}

/* ── misc hooks ───────────────────────────────────────────────── */
export function useAsync(fn, deps = []) {
  const [state, setState] = useState({ data: null, loading: true, error: null });
  const run = useCallback(async () => {
    setState((s) => ({ ...s, loading: true, error: null }));
    try { setState({ data: await fn(), loading: false, error: null }); }
    catch (e) { setState({ data: null, loading: false, error: e }); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => { run(); }, [run]);
  return { ...state, reload: run };
}

export function useDebounced(value, ms = 200) {
  const [v, setV] = useState(value);
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t); }, [value, ms]);
  return v;
}
