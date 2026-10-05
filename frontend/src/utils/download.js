// Download + error-message helpers (pure, unit-tested).
//
// Downloads used to be <a href=... target="_blank">: the browser opened a NEW TAB for the URL, saw an attachment,
// downloaded it and closed the tab, so the person saw a tab flash open and shut. They are now fetched in the page and
// saved directly under the name the server chose (Content-Disposition).

/** 'attachment; filename="a b.pdf"' / "filename*=UTF-8''a%20b.pdf" / 'filename=a.pdf' -> the file name, or null. */
export function filenameFromDisposition(header) {
  if (!header) return null;
  const star = /filename\*\s*=\s*(?:[\w-]+'[^']*')?([^;]+)/i.exec(header);
  if (star) {
    try { return decodeURIComponent(star[1].trim().replace(/^"|"$/g, "")); } catch { /* fall through to the plain form */ }
  }
  const plain = /filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;]+)/i.exec(header);
  const name = plain ? (plain[1] || plain[2] || "").trim() : "";
  return name || null;
}

/** Never let a server-supplied name carry a path or characters a file system rejects. */
export function safeFileName(name, fallback = "download") {
  const base = String(name || "").split(/[\\/]/).pop();                       // keep only the last path component
  const cleaned = base.replace(/[:*?"<>|\u0000-\u001f]+/g, "-").replace(/^\.+/, "").trim();
  return cleaned || fallback;
}

/** One human sentence for a failed request. The raw "API /path -> 500" text is for logs, not for people. */
export function describeApiError(e, doing = "completing that request") {
  if (!e) return `Something went wrong while ${doing}.`;
  if (e.detail) return e.detail;
  if (e.status === 403) return "You do not have permission to do that.";
  if (e.status === 404) return "That item no longer exists.";
  if (e.status === 429) return "Too many requests. Please wait a moment and try again.";
  if (e.status >= 500) {
    return `The server hit a problem while ${doing}. Please try again` +
      (e.requestId ? `; if it keeps happening, quote reference ${e.requestId}.` : ".");
  }
  if (e instanceof TypeError || /Failed to fetch|NetworkError|Load failed/i.test(e.message || "")) {
    return "Cannot reach the server. Check your connection and try again.";
  }
  return e.message || `Something went wrong while ${doing}.`;
}
