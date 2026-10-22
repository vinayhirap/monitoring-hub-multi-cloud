// Hand-rolled renderer for Help Center content blocks (no markdown library, no new dependency).
// Block types: heading, paragraph, list, table, note, code, link. Any block may carry unverified: true.
// Text is always rendered as React text, never as HTML.
import { useState } from "react";
import { Link } from "react-router-dom";
import { headingId } from "./searchSections";

function Unverified() {
  return (
    <span className="hc-unverified" title="This statement could not be confirmed in the product code when the guide was written. Treat it with care.">
      Not verified
    </span>
  );
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
  return <a href={href} target="_blank" rel="noopener noreferrer">{text}<span className="hc-ext" aria-hidden="true"> ↗</span></a>;
}

function CodeBlock({ text }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch { /* clipboard blocked (insecure origin): the text stays selectable */ }
  };
  return (
    <div className="hc-code-wrap">
      <button type="button" className="ui-btn hc-copy" onClick={copy} aria-label="Copy this code to the clipboard">
        {copied ? "Copied" : "Copy"}
      </button>
      <pre className="hc-code"><code>{text}</code></pre>
    </div>
  );
}

const NOTE_LABEL = { info: "Note", warn: "Important" };
const NOTE_ICON = { info: "i", warn: "!" };

export default function BlockRenderer({ blocks, sectionId, onSelectSection, hasSection }) {
  return (
    <>
      {blocks.map((b, i) => {
        const badge = b.unverified ? <Unverified /> : null;
        switch (b.type) {
          case "heading": {
            const Tag = b.level === 2 ? "h3" : "h4";
            return <Tag key={i} id={sectionId ? headingId(sectionId, i) : undefined} className={b.level === 2 ? "hc-h hc-h-major" : "hc-h"}>{b.text}{badge}</Tag>;
          }
          case "paragraph":
            return <p key={i} className="hc-p">{b.text}{badge}</p>;
          case "list": {
            const Tag = b.ordered ? "ol" : "ul";
            return <Tag key={i} className="hc-list">{b.items.map((it, j) => <li key={j}>{it}</li>)}{badge && <li className="hc-badge-row">{badge}</li>}</Tag>;
          }
          case "table":
            return (
              <div key={i} className="hc-table-wrap" tabIndex={0} role="region" aria-label="Table, scrolls sideways on small screens">
                <table className="hc-table">
                  <thead><tr>{b.columns.map((c, j) => <th key={j} scope="col">{c}</th>)}</tr></thead>
                  <tbody>{b.rows.map((r, j) => <tr key={j}>{r.map((c, k) => <td key={k}>{c}</td>)}</tr>)}</tbody>
                </table>
                {badge}
              </div>
            );
          case "note": {
            const tone = b.tone === "warn" ? "warn" : "info";
            return (
              <div key={i} className={`hc-note hc-note-${tone}`} role="note">
                <span className="hc-note-icon" aria-hidden="true">{NOTE_ICON[tone]}</span>
                <div className="hc-note-body">
                  <strong className="hc-note-label">{NOTE_LABEL[tone]}</strong>
                  <span>{b.text}</span>{badge}
                </div>
              </div>
            );
          }
          case "code":
            return <CodeBlock key={i} text={b.text} />;
          case "link":
            return <p key={i} className="hc-p hc-linkrow"><HelpLink block={b} onSelectSection={onSelectSection} hasSection={hasSection} />{badge}</p>;
          default:
            return null;   // an unknown block type from a newer server is skipped, never crashes the page
        }
      })}
    </>
  );
}
