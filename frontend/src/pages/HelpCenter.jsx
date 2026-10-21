// In-app Help Center. Fetches GET /api/help/sections (Phase E creates the endpoint):
//   { sections: [ { id, title, group, audience, requires, summary?, blocks: [...] } ] }
// The server decides which sections this user may read; the search box only filters what it returned.
import { useEffect, useMemo, useState, useCallback } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { apiFetch } from "../api/api";
import { PageHeader, EmptyState, Skeleton } from "../components/ui";
import BlockRenderer from "../help/BlockRenderer";
import { filterSections, groupSections } from "../help/searchSections";
import "./HelpCenter.css";

export default function HelpCenter() {
  const location = useLocation();
  const navigate = useNavigate();
  const [sections, setSections] = useState(null);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  const [openGroups, setOpenGroups] = useState({});

  useEffect(() => {
    let live = true;
    apiFetch("/api/help/sections")
      .then(d => { if (live) setSections(Array.isArray(d?.sections) ? d.sections : []); })
      .catch(e => { if (live) setError(e); });
    return () => { live = false; };
  }, []);

  const visible = useMemo(() => filterSections(sections || [], query), [sections, query]);
  const groups = useMemo(() => groupSections(visible), [visible]);
  const ids = useMemo(() => new Set((sections || []).map(s => s.id)), [sections]);
  const hasSection = useCallback(id => ids.has(id), [ids]);

  const hashId = decodeURIComponent((location.hash || "").replace(/^#/, ""));
  const selected = visible.find(s => s.id === hashId) || visible[0] || null;

  const select = useCallback(id => {
    navigate({ hash: `#${id}` }, { replace: true });
    window.scrollTo?.({ top: 0 });
  }, [navigate]);

  const searching = query.trim().length > 0;
  const groupOpen = g => searching || openGroups[g] === true
    || (openGroups[g] === undefined && g !== "Services and resource pages")
    || (selected && selected.group === g);

  return (
    <div className="page hc-page">
      <PageHeader title="Help Center" subtitle="How every CloudOps feature, service and setting works" />
      {error && (
        <EmptyState
          title="Help could not be loaded"
          body={error.status === 403 ? "You do not have access to the Help Center." : "The help content could not be fetched. Try again in a moment."}
        />
      )}
      {!error && sections === null && <div className="hc-loading"><Skeleton h={18} /><Skeleton h={18} w="70%" /><Skeleton h={18} w="85%" /></div>}
      {!error && sections !== null && (
        <div className="hc-layout">
          <nav className="hc-nav" aria-label="Help sections">
            <input
              type="search" className="hc-search" value={query} onChange={e => setQuery(e.target.value)}
              placeholder="Search help" aria-label="Search help"
            />
            <div className="hc-count">{visible.length} of {sections.length} sections</div>
            {groups.map(([g, list]) => (
              <div key={g} className="hc-group">
                <button
                  type="button" className="hc-group-btn" aria-expanded={groupOpen(g)}
                  onClick={() => setOpenGroups(o => ({ ...o, [g]: !groupOpen(g) }))}
                >
                  <span>{g}</span><span className="hc-group-n">{list.length}</span>
                </button>
                {groupOpen(g) && (
                  <ul className="hc-nav-list">
                    {list.map(s => (
                      <li key={s.id}>
                        <a
                          href={`#${s.id}`} className={selected && selected.id === s.id ? "hc-nav-item active" : "hc-nav-item"}
                          aria-current={selected && selected.id === s.id ? "page" : undefined}
                          onClick={e => { e.preventDefault(); select(s.id); }}
                        >{s.title}</a>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
            {!visible.length && <p className="hc-empty">No section matches "{query}".</p>}
          </nav>
          <article className="hc-article">
            {selected ? (
              <>
                <h2 className="hc-title">{selected.title}</h2>
                <div className="hc-meta">
                  <span className="hc-chip">{selected.group}</span>
                  <span className="hc-chip">For {selected.audience}</span>
                  {selected.requires && <span className="hc-chip hc-mono">needs {selected.requires}</span>}
                </div>
                <BlockRenderer blocks={selected.blocks || []} onSelectSection={select} hasSection={hasSection} />
              </>
            ) : (
              <EmptyState title="Nothing to show" body="No help section matches your search." />
            )}
          </article>
        </div>
      )}
    </div>
  );
}
