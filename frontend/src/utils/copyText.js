// utils/copyText.js
// Copy text to the clipboard and say whether it worked.
// navigator.clipboard only exists in secure contexts (https or localhost). The DEV/PROD servers are reached
// over plain http://<ip>, where navigator.clipboard is undefined, so a bare `navigator.clipboard?.writeText()`
// silently does nothing. Fall back to a hidden textarea + execCommand("copy"), which works on http.
export async function copyText(text) {
  const value = String(text ?? "");
  try {
    if (window.isSecureContext && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch { /* permission denied etc: fall through to the legacy path */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = value;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none";
    document.body.appendChild(ta);
    const prev = document.activeElement;
    ta.select();
    ta.setSelectionRange(0, value.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    if (prev && prev.focus) prev.focus();      // keep focus inside the drawer's focus trap
    return !!ok;
  } catch { return false; }
}
