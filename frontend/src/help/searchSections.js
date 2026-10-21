// Pure helpers for the Help Center: text search over the sections the server returned, and group ordering.
// Nothing here fetches anything, so the search can only ever see sections the user is allowed to read.

export const GROUP_ORDER = [
  "Getting started", "Configuration", "Alerts and incidents", "Monitoring", "Intelligence", "Reports",
  "Services and resource pages", "Administration", "Reference",
];

export function blockText(b) {
  const parts = [];
  if (b.text) parts.push(b.text);
  if (Array.isArray(b.items)) parts.push(...b.items);
  if (Array.isArray(b.columns)) parts.push(...b.columns);
  if (Array.isArray(b.rows)) for (const r of b.rows) parts.push(...r);
  return parts.join(" ");
}

export function sectionText(s) {
  return [s.title, s.summary || "", ...(s.blocks || []).map(blockText)].join(" ").toLowerCase();
}

// Every whitespace-separated term must appear somewhere in the section. Empty query keeps everything.
export function filterSections(sections, query) {
  const terms = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return sections;
  return sections.filter(s => {
    const hay = sectionText(s);
    return terms.every(t => hay.includes(t));
  });
}

export function groupSections(sections) {
  const map = new Map();
  for (const s of sections) {
    if (!map.has(s.group)) map.set(s.group, []);
    map.get(s.group).push(s);
  }
  const rank = g => { const i = GROUP_ORDER.indexOf(g); return i < 0 ? GROUP_ORDER.length : i; };
  return [...map.entries()].sort((a, b) => rank(a[0]) - rank(b[0]));
}
