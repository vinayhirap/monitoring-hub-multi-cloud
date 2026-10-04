// Small "⋯" overflow menu for table rows (audit B6: eight buttons per alert row). Keyboard- and screen-reader-friendly:
// the trigger is a button with aria-haspopup/aria-expanded, items are role=menuitem, Escape or an outside click
// closes it, and focus returns to the trigger. Clicks never bubble to the row (rows expand on click).
import { useEffect, useRef, useState } from "react";
import "./RowMenu.css";

export default function RowMenu({ label, items }) {
  const [open, setOpen] = useState(false);
  const root = useRef(null);
  const trigger = useRef(null);
  const visible = (items || []).filter(Boolean);

  useEffect(() => {
    if (!open) return undefined;
    const onDown = e => { if (root.current && !root.current.contains(e.target)) setOpen(false); };
    const onKey = e => { if (e.key === "Escape") { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);

  if (visible.length === 0) return null;
  return (
    <span className="rowmenu" ref={root} onClick={e => e.stopPropagation()}>
      <button type="button" ref={trigger} className="rowmenu-btn" aria-haspopup="menu" aria-expanded={open}
              aria-label={label} title={label} onClick={() => setOpen(o => !o)}>
        <span aria-hidden="true">{"\u22EF"}</span>
      </button>
      {open && (
        <ul className="rowmenu-list" role="menu" aria-label={label}>
          {visible.map(it => (
            <li key={it.key} role="none">
              <button type="button" role="menuitem" className={`rowmenu-item${it.danger ? " is-danger" : ""}`}
                      disabled={it.disabled} title={it.title}
                      onClick={() => { setOpen(false); it.onClick?.(); }}>
                {it.label}
              </button>
            </li>
          ))}
        </ul>
      )}
    </span>
  );
}
