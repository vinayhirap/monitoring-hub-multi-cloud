// src/hooks/useSearchHotkey.js
import { useEffect } from "react";

/** Ctrl/Cmd+K, or "/" outside form fields, calls onOpen (opens the command palette). */
export function useSearchHotkey(onOpen) {
  useEffect(() => {
    const h = e => {
      const t = e.target, typing = t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable);
      if (((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") || (e.key === "/" && !typing && !e.ctrlKey && !e.metaKey)) {
        e.preventDefault(); onOpen();
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onOpen]);
}
