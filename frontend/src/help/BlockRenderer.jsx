// Hand-rolled renderer for Help Center content blocks (no markdown library, no new dependency).
// Block types: heading, paragraph, list, table, note, code, link. Any block may carry unverified: true.
// Text is always rendered as React text, never as HTML.
import { Link } from "react-router-dom";

function Unverified() {
  return <span className="hc-unverified" title="Could not be confirmed in the code when this guide was written">unverified</span>;
}

function HelpLink({ block, onSelectSection, hasSection }) {
  const { href, text } = block;
  if (href.startsWith("#")) {
    const id = href.slice(1);
    // A section the user may not read is not in the list the server returned: show plain text, not a dead link.
    if (!hasSection(id)) return <span>{text}</span>;
    return <a href={href} onClick={e => { e.preventDefault(); onSelectSection(id); }}>{text}</a>;
  }
  if (href.startsWith("/")) return <Link to={href}>{text}</Link>;
  return <a href={href} target="_blank" rel="noopener noreferrer">{text}</a>;
}

export default function BlockRenderer({ blocks, onSelectSection, hasSection }) {
  return (
    <>
      {blocks.map((b, i) => {
        const badge = b.unverified ? <Unverified /> : null;
        switch (b.type) {
          case "heading": {
            const Tag = b.level === 2 ? "h3" : "h4";
            return <Tag key={i} className="hc-h">{b.text}{badge}</Tag>;
          }
          case "paragraph":
            return <p key={i} className="hc-p">{b.text}{badge}</p>;
          case "list": {
            const Tag = b.ordered ? "ol" : "ul";
            return <Tag key={i} className="hc-list">{b.items.map((it, j) => <li key={j}>{it}</li>)}{badge && <li className="hc-badge-row">{badge}</li>}</Tag>;
          }
          case "table":
            return (
              <div key={i} className="hc-table-wrap">
                <table className="hc-table">
                  <thead><tr>{b.columns.map((c, j) => <th key={j}>{c}</th>)}</tr></thead>
                  <tbody>{b.rows.map((r, j) => <tr key={j}>{r.map((c, k) => <td key={k}>{c}</td>)}</tr>)}</tbody>
                </table>
                {badge}
              </div>
            );
          case "note":
            return <div key={i} className={`hc-note hc-note-${b.tone === "warn" ? "warn" : "info"}`} role="note">{b.text}{badge}</div>;
          case "code":
            return <pre key={i} className="hc-code"><code>{b.text}</code></pre>;
          case "link":
            return <p key={i} className="hc-p"><HelpLink block={b} onSelectSection={onSelectSection} hasSection={hasSection} />{badge}</p>;
          default:
            return null;   // an unknown block type from a newer server is skipped, never crashes the page
        }
      })}
    </>
  );
}
