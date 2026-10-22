// Pure helpers for the Help Center: text search over the sections the server returned, group ordering,
// reader-facing labels, and the in-page table of contents.
// Nothing here fetches anything, so the search can only ever see sections the user is allowed to read.

export const GROUP_ORDER = [
  "Getting started", "Configuration", "Alerts and incidents", "Monitoring", "Intelligence", "Reports",
  "Services and resource pages", "Administration", "Reference",
];

// Groups that start collapsed because they hold dozens of pages.
export const COLLAPSED_BY_DEFAULT = ["Services and resource pages"];

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

// "For viewer" read like a role name; say who the page is actually written for.
const AUDIENCE_LABEL = {
  viewer: "Everyone",
  editor: "Editors and admins",
  admin: "Admins only",
};
export function audienceLabel(audience) {
  return AUDIENCE_LABEL[String(audience || "").toLowerCase()] || "Everyone";
}

// Stable DOM id for the heading block at `index` of a section (used by the table of contents).
export function headingId(sectionId, index) {
  return `${sectionId}-h${index}`;
}

// [{id, text}] for every heading block. The page shows "On this page" only when there are enough to be worth it.
export function tocOf(section) {
  if (!section) return [];
  const out = [];
  (section.blocks || []).forEach((b, i) => {
    if (b && b.type === "heading" && b.text) out.push({ id: headingId(section.id, i), text: b.text });
  });
  return out;
}

// The sections before and after `id` in the list as shown (which is already grouped and filtered).
export function neighbours(ordered, id) {
  const i = ordered.findIndex(s => s.id === id);
  if (i < 0) return { prev: null, next: null };
  return { prev: ordered[i - 1] || null, next: ordered[i + 1] || null };
}
