// Search / filter / paginate for the Security Findings list (audit B8). The page used to render every finding at once
// (hundreds of rows, ~4,900 DOM nodes on a phone) with no way to search. Pure functions, unit-tested.
export const PAGE_SIZE = 50;

/** Case-insensitive match over the text a person would actually look for. */
export function filterFindings(rows, { q = "", checkId = "", label = (id) => id } = {}) {
  const needle = String(q || "").trim().toLowerCase();
  return (rows || []).filter(f => {
    if (checkId && f.check_id !== checkId) return false;
    if (!needle) return true;
    const hay = [label(f.check_id), f.check_id, f.title, f.description, f.resource_id, f.account_name, f.region]
      .filter(Boolean).join(" ").toLowerCase();
    return hay.includes(needle);
  });
}

export function pageCount(total, size = PAGE_SIZE) {
  return Math.max(1, Math.ceil((total || 0) / size));
}

/** page is 1-based and clamped, so shrinking the list from a late page never shows an empty one. */
export function paginate(rows, page, size = PAGE_SIZE) {
  const list = rows || [];
  const pages = pageCount(list.length, size);
  const p = Math.min(Math.max(1, page || 1), pages);
  const start = (p - 1) * size;
  return { page: p, pages, total: list.length, from: list.length ? start + 1 : 0, to: Math.min(start + size, list.length),
           rows: list.slice(start, start + size) };
}

/** Distinct check ids present in the data, for the "Type" filter, sorted by label. */
export function checkTypes(rows, label = (id) => id) {
  return [...new Set((rows || []).map(f => f.check_id).filter(Boolean))]
    .map(id => ({ id, label: label(id) }))
    .sort((a, b) => a.label.localeCompare(b.label));
}
