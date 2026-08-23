// src/components/MetricSelector.jsx
import { useState, useMemo } from "react";
import "./MetricSelector.css";

import { CloudServiceIcon } from "./cloud-icons";

const PROVIDER_LABEL = { aws: "AWS", gcp: "GCP", azure: "Azure" };
function sectionMeta(provider) {
  const label = PROVIDER_LABEL[provider] || "cloud";
  return {
    core:      { label: "Core",      hint: "Already collected by this app today" },
    extended:  { label: "Extended",  hint: `Curated, common ${label} services` },
    directory: { label: "Directory", hint: `More ${label} services — discover live metric names per account` },
  };
}
const CATEGORY_TABS = ["all", "core", "extended", "directory"];
const CATEGORY_TAB_LABEL = { all: "All", core: "Core", extended: "Extended", directory: "Directory" };

function initials(name) {
  if (!name) return "?";
  const parts = name.replace(/^Amazon |^AWS /, "").split(" ").filter(Boolean);
  return ((parts[0]?.[0] || "") + (parts[1]?.[0] || "")).toUpperCase() || name[0].toUpperCase();
}

/**
 * Reusable AWS CloudWatch metric picker.
 *
 * props:
 *   catalog       — [{ service, display_service, namespace, category, metrics:[{id, metric_name, statistic, unit, description, is_default, enabled?}], directory_id }]
 *   selectedIds   — Set<number> of currently-enabled metric ids (controlled)
 *   onChange(nextSet) — called with a new Set whenever selection changes
 *   onDiscover(namespace) — optional async fn; runs live AWS discovery and
 *                            persists results server-side. Caller must
 *                            refresh `catalog` afterwards with real ids.
 *   compact       — slightly shorter max-height, used inline in onboarding
 */
export default function MetricSelector({ catalog, selectedIds, onChange, onDiscover, compact = false, provider = "aws" }) {
  const [search, setSearch]           = useState("");
  const [tab, setTab]                 = useState("all");
  const [expandedSvc, setExpandedSvc] = useState(() => new Set());
  const [collapsedSection, setCollapsedSection] = useState(() => new Set(["directory"]));
  const [discovering, setDiscovering] = useState(null);
  const SECTION_META = useMemo(() => sectionMeta(provider), [provider]);

  const q = search.trim().toLowerCase();

  const sections = useMemo(() => {
    const bySection = { core: [], extended: [], directory: [] };
    (catalog || []).forEach(g => {
      if (!g.display_service) return; // defensively hide any orphaned/unlabeled rows
      if (tab !== "all" && g.category !== tab) return;

      let metrics = g.metrics;
      let include = true;
      if (q) {
        const svcMatch = (g.display_service || "").toLowerCase().includes(q);
        metrics = svcMatch ? g.metrics : g.metrics.filter(m =>
          m.metric_name.toLowerCase().includes(q) || (m.description || "").toLowerCase().includes(q)
        );
        include = metrics.length > 0 || svcMatch;
      }
      if (!include) return;
      if (bySection[g.category]) bySection[g.category].push({ ...g, metrics });
    });
    Object.values(bySection).forEach(list => list.sort((a, b) => (a.display_service || "").localeCompare(b.display_service || "")));
    return bySection;
  }, [catalog, q, tab]);

  const sectionOrder = tab === "all" ? ["core", "extended", "directory"] : [tab];
  const totalSelected = selectedIds.size;
  const totalGroups = sectionOrder.reduce((n, s) => n + sections[s].length, 0);

  function toggleMetric(id) {
    const next = new Set(selectedIds);
    next.has(id) ? next.delete(id) : next.add(id);
    onChange(next);
  }

  function toggleService(group, allOn) {
    const next = new Set(selectedIds);
    group.metrics.forEach(m => allOn ? next.delete(m.id) : next.add(m.id));
    onChange(next);
  }

  function toggleExpandSvc(service) {
    setExpandedSvc(prev => {
      const next = new Set(prev);
      next.has(service) ? next.delete(service) : next.add(service);
      return next;
    });
  }

  function toggleSection(section) {
    setCollapsedSection(prev => {
      const next = new Set(prev);
      next.has(section) ? next.delete(section) : next.add(section);
      return next;
    });
  }

  function applyDefaults() {
    const next = new Set(selectedIds);
    (catalog || []).forEach(g => g.metrics.forEach(m => { if (m.is_default) next.add(m.id); }));
    onChange(next);
  }

  function clearAll() {
    onChange(new Set());
  }

  async function handleDiscover(group) {
    if (!onDiscover) return;
    setDiscovering(group.namespace);
    try {
      await onDiscover(group.namespace);
      setExpandedSvc(prev => new Set(prev).add(group.service));
    } catch (e) {
      console.error("Discover failed", e);
    } finally {
      setDiscovering(null);
    }
  }

  return (
    <div className={`ms-root ${compact ? "ms-compact" : ""}`}>
      <div className="ms-toolbar">
        <div className="ms-search-wrap">
          <svg className="ms-search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none">
            <circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="2" />
            <line x1="21" y1="21" x2="16.65" y2="16.65" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
          <input
            className="ms-search"
            placeholder="Search metrics or services…"
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </div>
        <div className="ms-tabs">
          {CATEGORY_TABS.map(t => (
            <button
              key={t}
              type="button"
              className={`ms-tab ${tab === t ? "ms-tab-active" : ""}`}
              onClick={() => setTab(t)}
            >
              {CATEGORY_TAB_LABEL[t]}
            </button>
          ))}
        </div>
      </div>

      <div className="ms-summary-bar">
        <span className="ms-count-badge">
          <strong>{totalSelected}</strong> metric{totalSelected === 1 ? "" : "s"} selected
          <span className="ms-count-sub"> · {totalGroups} service{totalGroups === 1 ? "" : "s"} shown</span>
        </span>
        <div className="ms-actions">
          <button type="button" className="ms-btn-ghost" onClick={applyDefaults}>âœ“ Apply recommended</button>
          <button type="button" className="ms-btn-ghost ms-btn-danger" onClick={clearAll}>Clear all</button>
        </div>
      </div>

      <div className="ms-list">
        {totalGroups === 0 && (
          <div className="ms-empty">
            <div className="ms-empty-icon">âŒ•</div>
            No metrics match {search ? `"${search}"` : "this filter"}.
          </div>
        )}

        {sectionOrder.map(sectionKey => {
          const groups = sections[sectionKey];
          if (groups.length === 0) return null;
          const meta = SECTION_META[sectionKey];
          const isCollapsed = tab === "all" && collapsedSection.has(sectionKey) && !q;
          const selInSection = groups.reduce((n, g) => n + g.metrics.filter(m => selectedIds.has(m.id)).length, 0);
          const totalInSection = groups.reduce((n, g) => n + g.metrics.length, 0);

          return (
            <div key={sectionKey} className="ms-section">
              {tab === "all" && (
                <button type="button" className="ms-section-header" onClick={() => toggleSection(sectionKey)}>
                  <span className={`ms-section-chevron ${isCollapsed ? "" : "ms-section-chevron-open"}`}>â–¸</span>
                  <span className={`ms-section-dot ms-section-dot-${sectionKey}`} />
                  <span className="ms-section-label">{meta.label}</span>
                  <span className="ms-section-hint">{meta.hint}</span>
                  <span className="ms-section-spacer" />
                  <span className="ms-section-count">
                    {totalInSection > 0 ? `${selInSection}/${totalInSection} selected` : `${groups.length} services`}
                  </span>
                </button>
              )}

              {!isCollapsed && (
                <div className="ms-section-body">
                  {groups.map(group => {
                    const isOpen   = expandedSvc.has(group.service) || q !== "";
                    const selCount = group.metrics.filter(m => selectedIds.has(m.id)).length;
                    const allOn    = group.metrics.length > 0 && selCount === group.metrics.length;
                    const partial  = selCount > 0 && !allOn;
                    const isDirectoryEmpty = group.category === "directory" && group.metrics.length === 0;

                    return (
                      <div key={group.service} className={`ms-card ${selCount > 0 ? "ms-card-active" : ""}`}>
                        <button
                          type="button"
                          className="ms-card-header"
                          onClick={() => toggleExpandSvc(group.service)}
                          disabled={group.metrics.length === 0 && !isDirectoryEmpty}
                        >
                          <span className={`ms-avatar ms-avatar-${group.category}`}>
                            <CloudServiceIcon provider={provider} service={group.service} size={18} />
                          </span>
                          <span className="ms-card-titles">
                            <span className="ms-card-name">{group.display_service}</span>
                            <span className="ms-card-namespace">{group.namespace}</span>
                          </span>
                          <span className="ms-spacer" />
                          {group.metrics.length > 0 && (
                            <>
                              <span className={`ms-progress-pill ${allOn ? "ms-progress-full" : partial ? "ms-progress-partial" : ""}`}>
                                {selCount}/{group.metrics.length}
                              </span>
                              <span
                                className="ms-select-all"
                                onClick={(e) => { e.stopPropagation(); toggleService(group, allOn); }}
                              >
                                {allOn ? "Clear" : "Select all"}
                              </span>
                            </>
                          )}
                          <span className={`ms-chevron ${isOpen ? "ms-chevron-open" : ""}`}>â–¾</span>
                        </button>

                        {isOpen && (
                          <div className="ms-card-body">
                            {isDirectoryEmpty && (
                              <div className="ms-discover-row">
                                <span className="ms-discover-hint">
                                  Metric names for this service aren't pre-loaded — discover what your account actually publishes.
                                </span>
                                {onDiscover ? (
                                  <button
                                    type="button"
                                    className="ms-btn-ghost"
                                    disabled={discovering === group.namespace}
                                    onClick={() => handleDiscover(group)}
                                  >
                                    {discovering === group.namespace ? "Discovering…" : "Discover metrics"}
                                  </button>
                                ) : (
                                  <span className="ms-discover-hint-sub">Available after onboarding (Settings â†’ Metrics).</span>
                                )}
                              </div>
                            )}
                            {group.metrics.map(m => (
                              <label key={m.id} className="ms-metric-row">
                                <input
                                  type="checkbox"
                                  checked={selectedIds.has(m.id)}
                                  onChange={() => toggleMetric(m.id)}
                                />
                                <span className="ms-metric-name">{m.metric_name}</span>
                                <span className="ms-metric-stat">{m.statistic}{m.unit ? ` · ${m.unit}` : ""}</span>
                                <span className="ms-metric-desc">{m.description}</span>
                                {m.is_default && <span className="ms-default-tag">recommended</span>}
                              </label>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

