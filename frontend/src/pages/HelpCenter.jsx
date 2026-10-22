// In-app Help Center. Fetches GET /api/help/sections (Phase E creates the endpoint):
//   { sections: [ { id, title, group, audience, requires, summary?, blocks: [...] } ] }
// The server decides which sections this user may read; the search box only filters what it returned.
import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { apiFetch } from "../api/api";
import { PageHeader, EmptyState, Skeleton } from "../components/ui";
import BlockRenderer from "../help/BlockRenderer";
import {
  filterSections, groupSections, audienceLabel, tocOf, neighbours, COLLAPSED_BY_DEFAULT,
} from "../help/searchSections";
import "./HelpCenter.css";

const prefersReducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

export default function HelpCenter() {
  const location = useLocation();
  const navigate = useNavigate();
  const [sections, setSections] = useState(null);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const [openGroups, setOpenGroups] = useState({});
  const [navShown, setNavShown] = useState(false);     // small screens: the topic list is folded away until asked for
  const [linkCopied, setLinkCopied] = useState(false);
  const searchRef = useRef(null);
  const articleRef = useRef(null);

  useEffect(() => {
    let live = true;
    apiFetch("/api/help/sections")
      .then(d => { if (live) { setError(null); setSections(Array.isArray(d?.sections) ? d.sections : []); } })
      .catch(e => { if (live) setError(e); });
    return () => { live = false; };
  }, [attempt]);

  // "/" jumps to the search box, like most documentation sites (ignored while typing in a field).
  useEffect(() => {
    const onKey = e => {
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target;
      const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
      if (typing) return;
      e.preventDefault();
      searchRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const visible = useMemo(() => filterSections(sections || [], query), [sections, query]);
  const groups = useMemo(() => groupSections(visible), [visible]);
  const ordered = useMemo(() => groups.flatMap(([, list]) => list), [groups]);   // reading order = sidebar order
  const ids = useMemo(() => new Set((sections || []).map(s => s.id)), [sections]);
  const hasSection = useCallback(id => ids.has(id), [ids]);

  const hashId = decodeURIComponent((location.hash || "").replace(/^#/, ""));
  const selected = visible.find(s => s.id === hashId) || ordered[0] || null;
  const toc = useMemo(() => tocOf(selected), [selected]);
  const { prev, next } = useMemo(() => neighbours(ordered, selected?.id), [ordered, selected]);

  const select = useCallback(id => {
    const target = (sections || []).find(s => s.id === id);
    if (target) setOpenGroups(o => ({ ...o, [target.group]: true }));
    navigate({ hash: `#${id}` }, { replace: true });
    setNavShown(false);
    setLinkCopied(false);
    // Scroll the article into view (works whichever element the app scrolls, unlike window.scrollTo).
    requestAnimationFrame(() => articleRef.current?.scrollIntoView({ block: "start", behavior: prefersReducedMotion() ? "auto" : "smooth" }));
  }, [navigate, sections]);

  const jumpTo = useCallback((domId) => {
    document.getElementById(domId)?.scrollIntoView({ block: "start", behavior: prefersReducedMotion() ? "auto" : "smooth" });
  }, []);

  const copyLink = useCallback(async () => {
    if (!selected) return;
    const url = `${window.location.origin}${window.location.pathname}#${selected.id}`;
    try {
      await navigator.clipboard.writeText(url);
      setLinkCopied(true);
      setTimeout(() => setLinkCopied(false), 1800);
    } catch { /* clipboard blocked: the address bar already holds the link */ }
  }, [selected]);

  const searching = query.trim().length > 0;
  const groupOpen = g => {
    if (searching) return true;
    if (openGroups[g] !== undefined) return openGroups[g];
    if (COLLAPSED_BY_DEFAULT.includes(g)) return !!selected && selected.group === g;
    return true;
  };
  const allOpen = groups.length > 0 && groups.every(([g]) => groupOpen(g));
  const setAllGroups = open => setOpenGroups(Object.fromEntries(groups.map(([g]) => [g, open])));

  return (
    <div className="page hc-page">
      <PageHeader title="Help Center" subtitle="How every CloudOps feature, service and setting works. Press / to search." />
      {error && (
        <EmptyState
          title="Help could not be loaded"
          body={error.status === 403 ? "You do not have access to the Help Center." : "The help content could not be fetched. Check your connection and try again."}
          action={error.status === 403 ? undefined : <button type="button" className="ui-btn ui-btn-primary" onClick={() => { setError(null); setSections(null); setAttempt(n => n + 1); }}>Try again</button>}
        />
      )}
      {!error && sections === null && <div className="hc-loading" aria-busy="true"><Skeleton h={18} /><Skeleton h={18} w="70%" /><Skeleton h={18} w="85%" /></div>}
      {!error && sections !== null && (
        <div className="hc-layout">
          <nav className="hc-nav" aria-label="Help topics">
            <button
              type="button" className="ui-btn hc-nav-toggle" aria-expanded={navShown} aria-controls="hc-nav-body"
              onClick={() => setNavShown(v => !v)}
            >{navShown ? "Hide topics" : `Browse all topics (${sections.length})`}</button>
            <div id="hc-nav-body" className={navShown ? "hc-nav-body is-shown" : "hc-nav-body"}>
              <div className="hc-search-wrap">
                <input
                  ref={searchRef} type="search" className="hc-search" value={query} onChange={e => setQuery(e.target.value)}
                  placeholder="Search topics..." aria-label="Search help"
                  onKeyDown={e => { if (e.key === "Escape") { setQuery(""); e.currentTarget.blur(); } }}
                />
                {searching && (
                  <button type="button" className="hc-search-clear" onClick={() => { setQuery(""); searchRef.current?.focus(); }} aria-label="Clear search">×</button>
                )}
              </div>
              <div className="hc-count-row">
                <span className="hc-count" aria-live="polite">
                  {searching ? `${visible.length} of ${sections.length} topics match` : `${sections.length} topics`}
                </span>
                {groups.length > 1 && !searching && (
                  <button type="button" className="hc-linkbtn" onClick={() => setAllGroups(!allOpen)}>
                    {allOpen ? "Collapse all" : "Expand all"}
                  </button>
                )}
              </div>
              {groups.map(([g, list]) => {
                const open = groupOpen(g);
                const listId = `hc-list-${g.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}`;
                return (
                  <div key={g} className="hc-group">
                    <button
                      type="button" className="hc-group-btn" aria-expanded={open} aria-controls={listId}
                      onClick={() => setOpenGroups(o => ({ ...o, [g]: !open }))}
                    >
                      <span className={open ? "hc-chev is-open" : "hc-chev"} aria-hidden="true">›</span>
                      <span className="hc-group-name">{g}</span>
                      <span className="hc-group-n">{list.length}</span>
                    </button>
                    {open && (
                      <ul className="hc-nav-list" id={listId}>
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
                );
              })}
              {!visible.length && (
                <div className="hc-empty">
                  <p>No topic matches "{query}".</p>
                  <button type="button" className="ui-btn" onClick={() => setQuery("")}>Clear search</button>
                </div>
              )}
            </div>
          </nav>
          <article className="hc-article" ref={articleRef}>
            {selected ? (
              <>
                <div className="hc-crumb">{selected.group}</div>
                <div className="hc-titlerow">
                  <h2 className="hc-title">{selected.title}</h2>
                  <button type="button" className="ui-btn hc-copylink" onClick={copyLink}>{linkCopied ? "Link copied" : "Copy link"}</button>
                </div>
                {selected.summary && <p className="hc-lead">{selected.summary}</p>}
                <div className="hc-meta">
                  <span className="hc-chip" title="Who this page is written for">Audience: {audienceLabel(selected.audience)}</span>
                  {selected.requires && (
                    <span className="hc-chip" title="The permission needed to open this page. Ask an administrator if you need it.">
                      Permission: <span className="hc-mono">{selected.requires}</span>
                    </span>
                  )}
                </div>
                {toc.length >= 3 && (
                  <nav className="hc-toc" aria-label="On this page">
                    <span className="hc-toc-title">On this page</span>
                    <ul>
                      {toc.map(t => (
                        <li key={t.id}><a href={`#${t.id}`} onClick={e => { e.preventDefault(); jumpTo(t.id); }}>{t.text}</a></li>
                      ))}
                    </ul>
                  </nav>
                )}
                <div className="hc-body">
                  <BlockRenderer blocks={selected.blocks || []} sectionId={selected.id} onSelectSection={select} hasSection={hasSection} />
                </div>
                {(prev || next) && (
                  <div className="hc-pager">
                    {prev ? (
                      <button type="button" className="ui-btn hc-pager-btn" onClick={() => select(prev.id)}>
                        <span className="hc-pager-dir">← Previous</span><span className="hc-pager-title">{prev.title}</span>
                      </button>
                    ) : <span />}
                    {next ? (
                      <button type="button" className="ui-btn hc-pager-btn hc-pager-next" onClick={() => select(next.id)}>
                        <span className="hc-pager-dir">Next →</span><span className="hc-pager-title">{next.title}</span>
                      </button>
                    ) : <span />}
                  </div>
                )}
              </>
            ) : (
              <EmptyState
                title="Nothing to show"
                body="No help topic matches your search."
                action={searching ? <button type="button" className="ui-btn" onClick={() => setQuery("")}>Clear search</button> : undefined}
              />
            )}
          </article>
        </div>
      )}
    </div>
  );
}
