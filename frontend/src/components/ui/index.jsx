// src/components/ui/index.jsx -- CloudOps UI primitives (UI Phase 1).
// Token-only styling (see styles/tokens.css + ui.css). No data fetching here.
import { useEffect, useMemo, useRef, useState } from "react";

const cx = (...a) => a.filter(Boolean).join(" ");

/* Page title block. `actions` is any node (buttons); `meta` sits under subtitle. */
export function PageHeader({ title, subtitle, actions, meta }) {
  return (
    <header className="ui-pagehead">
      <div className="ui-pagehead-text">
        <h1 className="ui-h1">{title}</h1>
        {subtitle && <p className="ui-sub">{subtitle}</p>}
        {meta && <div className="ui-meta">{meta}</div>}
      </div>
      {actions && <div className="ui-pagehead-actions">{actions}</div>}
    </header>
  );
}

/* Status dot. `pulse` only for firing-critical; motion is off under reduced-motion. */
export function StatusBeacon({ tone = "mute", pulse = false, label }) {
  return <span className={cx("ui-beacon", `t-${tone}`, pulse && "ui-pulse")} role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true} />;
}

/* Compact chip. mode="predicted" gives the dashed violet model-output look. */
export function Badge({ tone = "mute", mode = "actual", children, title }) {
  const t = mode === "predicted" ? "predicted" : tone;
  return <span className={cx("ui-badge", `t-${t}`, mode === "predicted" && "ui-dashed")} title={title}>{children}</span>;
}

const SEV_TONE = { critical: "crit", warning: "warn", info: "info" };
export function SeverityBadge({ severity }) {
  const s = String(severity || "").toLowerCase();
  return <Badge tone={SEV_TONE[s] || "mute"}><StatusBeacon tone={SEV_TONE[s] || "mute"} />{s || "unknown"}</Badge>;
}

/* Marks anything produced by a model. Always say which method when known. */
export function AiChip({ method = "AI" }) {
  return <Badge mode="predicted" title="Model-derived, not a direct measurement">{method}</Badge>;
}

/* Dependency-free sparkline. values: number[]; renders nothing under 2 points
   (we never draw a fake trend). */
export function Sparkline({ values, tone = "brand", width = 96, height = 26 }) {
  if (!Array.isArray(values) || values.length < 2) return null;
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * width},${height - 2 - ((v - min) / span) * (height - 4)}`).join(" ");
  return (
    <svg className={cx("ui-spark", `t-${tone}`)} width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

/* KPI tile. delta / spark are optional and only rendered when supplied. */
export function KpiCard({ label, value, tone, sub, delta, spark, onClick, hint, pulse, loading }) {
  const Tag = onClick ? "button" : "div";
  return (
    <Tag className={cx("ui-kpi", tone && `t-${tone}`, onClick && "ui-clickable")} onClick={onClick} title={hint} type={onClick ? "button" : undefined}>
      <span className="ui-kpi-label">{label}{pulse && <StatusBeacon tone={tone || "crit"} pulse />}</span>
      {loading ? <span className="ui-skel ui-skel-kpi" /> : <span className="ui-kpi-value">{value}</span>}
      <span className="ui-kpi-foot">
        {sub && <span className="ui-kpi-sub">{sub}</span>}
        {delta != null && <span className={cx("ui-delta", delta > 0 ? "t-warn" : "t-ok")}>{delta > 0 ? "+" : ""}{delta}</span>}
        {spark && <Sparkline values={spark} tone={tone || "brand"} />}
      </span>
    </Tag>
  );
}
export const KpiStrip = ({ children }) => <div className="ui-kpistrip">{children}</div>;

export function Panel({ title, subtitle, actions, footer, children, flush, tone }) {
  return (
    <section className={cx("ui-panel", tone && `t-${tone}`)}>
      {(title || actions) && (
        <div className="ui-panel-head">
          <div><h2 className="ui-h2">{title}</h2>{subtitle && <p className="ui-sub">{subtitle}</p>}</div>
          {actions && <div className="ui-panel-actions">{actions}</div>}
        </div>
      )}
      <div className={flush ? "ui-panel-body-flush" : "ui-panel-body"}>{children}</div>
      {footer && <div className="ui-panel-foot">{footer}</div>}
    </section>
  );
}

/* Status chips + search. chips: [{key,label,count?,tone?}] */
export function FilterBar({ chips, active, onChange, search, onSearch, placeholder = "Search", right }) {
  return (
    <div className="ui-filterbar" role="toolbar" aria-label="Filters">
      {chips && (
        <div className="ui-chips" role="group">
          {chips.map(c => (
            <button key={c.key} type="button" aria-pressed={active === c.key}
              className={cx("ui-chip", active === c.key && "is-on", c.tone && `t-${c.tone}`)} onClick={() => onChange?.(c.key)}>
              {c.tone && <StatusBeacon tone={c.tone} />}{c.label}{c.count != null && <span className="ui-chip-count">{c.count}</span>}
            </button>
          ))}
        </div>
      )}
      {onSearch && (
        <input className="ui-input" type="search" value={search || ""} placeholder={placeholder}
          aria-label={placeholder} onChange={e => onSearch(e.target.value)} />
      )}
      {right && <div className="ui-filterbar-right">{right}</div>}
    </div>
  );
}

export const Skeleton = ({ h = 14, w = "100%" }) => <span className="ui-skel" style={{ height: h, width: w }} />;

export function EmptyState({ title, body, action }) {
  return (
    <div className="ui-empty">
      <div className="ui-empty-title">{title}</div>
      {body && <div className="ui-sub">{body}</div>}
      {action}
    </div>
  );
}

export function SegmentedControl({ options, value, onChange, label }) {
  return (
    <div className="ui-seg" role="group" aria-label={label}>
      {options.map(o => (
        <button key={o.key} type="button" aria-pressed={value === o.key} className={cx("ui-seg-btn", value === o.key && "is-on")} onClick={() => onChange(o.key)}>{o.label}</button>
      ))}
    </div>
  );
}

/* Replaces window.confirm(). `typeToConfirm` (string) forces typing a target name. */
export function ConfirmDialog(props) {
  // Inner component mounts only while open, so typed text resets by remount.
  return props.open ? <ConfirmDialogInner {...props} /> : null;
}
function ConfirmDialogInner({ title, body, confirmLabel = "Confirm", danger, typeToConfirm, busy, onConfirm, onCancel }) {
  const [typed, setTyped] = useState("");
  const ref = useRef(null);
  useEffect(() => { ref.current?.focus(); }, []);
  useEffect(() => {
    const k = e => { if (e.key === "Escape") onCancel?.(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onCancel]);
  const ok = !typeToConfirm || typed === typeToConfirm;
  return (
    <div className="ui-scrim" onMouseDown={e => { if (e.target === e.currentTarget) onCancel?.(); }}>
      <div className="ui-dialog" role="alertdialog" aria-modal="true" aria-labelledby="ui-dlg-t">
        <h2 id="ui-dlg-t" className="ui-h2">{title}</h2>
        <div className="ui-dialog-body">{body}</div>
        {typeToConfirm && (
          <label className="ui-dialog-type">Type <code>{typeToConfirm}</code> to confirm
            <input ref={ref} className="ui-input" value={typed} onChange={e => setTyped(e.target.value)} />
          </label>
        )}
        <div className="ui-dialog-actions">
          <button type="button" className="ui-btn" onClick={onCancel} ref={typeToConfirm ? undefined : ref}>Cancel</button>
          <button type="button" className={cx("ui-btn", danger ? "ui-btn-danger" : "ui-btn-primary")} disabled={!ok || busy} onClick={onConfirm}>{busy ? "Working…" : confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}

/* Dense sortable table.
   columns: [{key,header,render?(row),num?,sort?(row)=>value,width?}]
   Numeric columns: right-aligned mono. Wrapped in .tbl-scroll (global) so wide
   rows scroll inside the card. */
export function DataTable({ columns, rows, rowKey, onRowClick, empty, loading, initialSort, dense }) {
  const [sort, setSort] = useState(initialSort || null);
  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = columns.find(c => c.key === sort.key);
    if (!col) return rows;
    const val = col.sort || (r => r[col.key]);
    const out = [...rows].sort((a, b) => {
      const x = val(a), y = val(b);
      if (x == null) return 1; if (y == null) return -1;
      return typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y));
    });
    return sort.dir === "desc" ? out.reverse() : out;
  }, [rows, columns, sort]);
  const toggle = c => { if (!(c.sort || c.sortable)) return; setSort(s => s?.key === c.key ? { key: c.key, dir: s.dir === "asc" ? "desc" : "asc" } : { key: c.key, dir: "asc" }); };
  return (
    <div className="tbl-scroll">
      <table className={cx("ui-table", dense && "is-dense")}>
        <thead>
          <tr>
            {columns.map(c => (
              <th key={c.key} style={c.width ? { width: c.width } : undefined} className={cx(c.num && "is-num", (c.sort || c.sortable) && "is-sortable")}
                aria-sort={sort?.key === c.key ? (sort.dir === "asc" ? "ascending" : "descending") : undefined}>
                {(c.sort || c.sortable)
                  ? <button type="button" className="ui-th-btn" onClick={() => toggle(c)}>{c.header}{sort?.key === c.key ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}</button>
                  : c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {loading && Array.from({ length: 4 }, (_, i) => <tr key={`s${i}`}>{columns.map(c => <td key={c.key}><Skeleton /></td>)}</tr>)}
          {!loading && sorted.map(r => (
            <tr key={rowKey(r)} className={onRowClick ? "ui-clickable" : undefined} onClick={onRowClick ? () => onRowClick(r) : undefined}
              tabIndex={onRowClick ? 0 : undefined} onKeyDown={onRowClick ? e => { if (e.key === "Enter") onRowClick(r); } : undefined}>
              {columns.map(c => <td key={c.key} className={c.num ? "is-num" : undefined}>{c.render ? c.render(r) : r[c.key]}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      {!loading && sorted.length === 0 && (empty || <EmptyState title="Nothing to show" />)}
    </div>
  );
}
