// Right-hand slide-over for a resource's detail (same pattern as AlertInvestigation):
// dimmed backdrop, click outside / Esc closes, focus moves in, Tab stays inside, focus returns on close.
import { useEffect, useRef } from "react";
import "./ResourceDrawer.css";

export default function ResourceDrawer({ onClose, label, children }) {
  const panel = useRef(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const prev = document.activeElement;
    panel.current?.focus();
    const h = e => {
      if (e.key === "Escape") { closeRef.current(); return; }
      if (e.key !== "Tab" || !panel.current) return;
      const f = [...panel.current.querySelectorAll("button:not([disabled]),a[href],select,input,[tabindex]:not([tabindex='-1'])")].filter(x => x.offsetParent !== null);
      if (!f.length) { e.preventDefault(); return; }
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", h);
    return () => { document.removeEventListener("keydown", h); if (prev && prev.focus && document.contains(prev)) prev.focus(); };
  }, []);
  return (
    <>
      <div className="rd-scrim" onClick={onClose} />
      <aside className="rd" ref={panel} tabIndex={-1} role="dialog" aria-modal="true" aria-label={label || "Resource details"}>
        <div className="rd-body">{children}</div>
      </aside>
    </>
  );
}
