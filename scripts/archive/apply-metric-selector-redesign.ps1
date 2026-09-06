# apply-metric-selector-redesign.ps1
# Redesigns the AWS CloudWatch metric picker (MetricSelector) used in
# Account Onboarding + Settings -> Metrics: card-based services grouped
# into collapsible Core / Extended / Directory sections, grid-aligned
# metric rows, category color-coding, and a cleaner toolbar.
#
# Run this from your repo root:
#   D:\Project\monitoring-tool\monitoring-hub-V3-grafana> .\apply-metric-selector-redesign.ps1
#
# It only touches these two files (safe to re-run):
#   frontend/src/components/MetricSelector.jsx
#   frontend/src/components/MetricSelector.css

$ErrorActionPreference = "Stop"

if (-not (Test-Path "frontend/src/components")) {
    Write-Host "ERROR: frontend/src/components not found." -ForegroundColor Red
    Write-Host "Run this script from your repo root (where the 'frontend' folder lives)." -ForegroundColor Red
    exit 1
}

$jsxPath = "frontend/src/components/MetricSelector.jsx"
$cssPath = "frontend/src/components/MetricSelector.css"

Write-Host "Writing $jsxPath ..." -ForegroundColor Cyan
@'
// src/components/MetricSelector.jsx
import { useState, useMemo } from "react";
import "./MetricSelector.css";

const SECTION_META = {
  core:      { label: "Core",      hint: "Already collected by this app today" },
  extended:  { label: "Extended",  hint: "Curated, common AWS services" },
  directory: { label: "Directory", hint: "100+ AWS namespaces â€” discover live metric names per account" },
};
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
 *   catalog       â€” [{ service, display_service, namespace, category, metrics:[{id, metric_name, statistic, unit, description, is_default, enabled?}], directory_id }]
 *   selectedIds   â€” Set<number> of currently-enabled metric ids (controlled)
 *   onChange(nextSet) â€” called with a new Set whenever selection changes
 *   onDiscover(namespace) â€” optional async fn; runs live AWS discovery and
 *                            persists results server-side. Caller must
 *                            refresh `catalog` afterwards with real ids.
 *   compact       â€” slightly shorter max-height, used inline in onboarding
 */
export default function MetricSelector({ catalog, selectedIds, onChange, onDiscover, compact = false }) {
  const [search, setSearch]           = useState("");
  const [tab, setTab]                 = useState("all");
  const [expandedSvc, setExpandedSvc] = useState(() => new Set());
  const [collapsedSection, setCollapsedSection] = useState(() => new Set(["directory"]));
  const [discovering, setDiscovering] = useState(null);

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
            placeholder="Search metrics or servicesâ€¦"
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
          <span className="ms-count-sub"> Â· {totalGroups} service{totalGroups === 1 ? "" : "s"} shown</span>
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
                          <span className={`ms-avatar ms-avatar-${group.category}`}>{initials(group.display_service)}</span>
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
                                  Metric names for this service aren't pre-loaded â€” discover what your account actually publishes.
                                </span>
                                {onDiscover ? (
                                  <button
                                    type="button"
                                    className="ms-btn-ghost"
                                    disabled={discovering === group.namespace}
                                    onClick={() => handleDiscover(group)}
                                  >
                                    {discovering === group.namespace ? "Discoveringâ€¦" : "Discover metrics"}
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
                                <span className="ms-metric-stat">{m.statistic}{m.unit ? ` Â· ${m.unit}` : ""}</span>
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

'@ | Set-Content -Path $jsxPath -Encoding UTF8

Write-Host "Writing $cssPath ..." -ForegroundColor Cyan
@'
/* src/components/MetricSelector.css */
.ms-root {
  display: flex;
  flex-direction: column;
  gap: 12px;
  font-family: var(--font-sans);
}

/* â”€â”€ Toolbar â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.ms-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

.ms-search-wrap {
  position: relative;
  flex: 1 1 240px;
  min-width: 200px;
}
.ms-search-icon {
  position: absolute;
  left: 11px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--text-muted);
  pointer-events: none;
}
.ms-search {
  width: 100%;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text-primary);
  padding: 9px 12px 9px 32px;
  font-size: 13px;
  transition: border-color 0.15s;
}
.ms-search:focus { outline: none; border-color: var(--accent); }
.ms-search::placeholder { color: var(--text-muted); }

.ms-tabs {
  display: flex;
  gap: 4px;
  background: var(--bg-input);
  padding: 3px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
}
.ms-tab {
  background: transparent;
  border: none;
  color: var(--text-secondary);
  border-radius: 5px;
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
  white-space: nowrap;
}
.ms-tab:hover { color: var(--text-primary); }
.ms-tab-active {
  background: var(--accent-dim);
  color: var(--accent);
}

/* â”€â”€ Summary bar â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.ms-summary-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  padding: 2px 2px;
}
.ms-count-badge {
  font-size: 12.5px;
  color: var(--text-secondary);
}
.ms-count-badge strong {
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: 13.5px;
}
.ms-count-sub { color: var(--text-muted); }

.ms-actions { display: flex; align-items: center; gap: 8px; }
.ms-btn-ghost {
  background: transparent;
  border: 1px solid var(--border-md);
  color: var(--text-secondary);
  border-radius: var(--radius);
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s, background 0.15s;
}
.ms-btn-ghost:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-dim); }
.ms-btn-ghost:disabled { opacity: 0.5; cursor: default; }
.ms-btn-danger:hover { border-color: var(--red); color: var(--red); background: rgba(255,77,109,0.08); }

/* â”€â”€ List / sections â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.ms-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-height: 560px;
  overflow-y: auto;
  overflow-x: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 14px;
  background: var(--bg-surface);
}
.ms-compact .ms-list { max-height: 420px; }

.ms-empty {
  padding: 48px 20px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.ms-empty-icon { font-size: 22px; opacity: 0.5; }

.ms-section { display: flex; flex-direction: column; gap: 8px; }

.ms-section-header {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-md);
  border-radius: var(--radius);
  padding: 9px 12px;
  cursor: pointer;
  text-align: left;
}
.ms-section-header:hover { border-color: var(--border-bright); }

.ms-section-chevron { color: var(--text-muted); font-size: 10px; transition: transform 0.15s; }
.ms-section-chevron-open { transform: rotate(90deg); }

.ms-section-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.ms-section-dot-core      { background: var(--accent-green); box-shadow: 0 0 6px var(--accent-green); }
.ms-section-dot-extended  { background: var(--accent); box-shadow: 0 0 6px var(--accent); }
.ms-section-dot-directory { background: var(--accent-purple); box-shadow: 0 0 6px var(--accent-purple); }

.ms-section-label {
  font-size: 12.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-primary);
}
.ms-section-hint { font-size: 11.5px; color: var(--text-muted); }
.ms-section-spacer { flex: 1; }
.ms-section-count { font-size: 11.5px; color: var(--text-secondary); font-family: var(--font-mono); }

.ms-section-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-left: 4px;
}

/* â”€â”€ Service cards â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.ms-card {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-card);
  overflow: hidden;
  transition: border-color 0.15s;
}
.ms-card-active { border-color: var(--border-bright); }

.ms-card-header {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 10px;
  background: transparent;
  border: none;
  color: var(--text-primary);
  padding: 10px 12px;
  cursor: pointer;
  text-align: left;
}
.ms-card-header:hover { background: var(--bg-card-hover); }
.ms-card-header:disabled { cursor: default; opacity: 0.6; }

.ms-avatar {
  width: 28px;
  height: 28px;
  border-radius: 7px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10.5px;
  font-weight: 700;
  flex-shrink: 0;
  font-family: var(--font-mono);
}
.ms-avatar-core      { background: rgba(0,229,160,0.12); color: var(--accent-green); }
.ms-avatar-extended  { background: var(--accent-dim);    color: var(--accent); }
.ms-avatar-directory { background: rgba(167,139,250,0.12); color: var(--accent-purple); }

.ms-card-titles { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.ms-card-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ms-card-namespace {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-muted);
}

.ms-spacer { flex: 1; min-width: 8px; }

.ms-progress-pill {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-secondary);
  background: var(--bg-input);
  border: 1px solid var(--border-md);
  border-radius: 20px;
  padding: 2px 9px;
  white-space: nowrap;
}
.ms-progress-partial { color: var(--accent); border-color: var(--accent); }
.ms-progress-full     { color: var(--accent-green); border-color: var(--accent-green); }

.ms-select-all {
  font-size: 11px;
  font-weight: 500;
  color: var(--accent);
  cursor: pointer;
  padding: 3px 8px;
  white-space: nowrap;
  border-radius: 5px;
}
.ms-select-all:hover { background: var(--accent-dim); }

.ms-chevron { color: var(--text-muted); font-size: 10px; transition: transform 0.15s; flex-shrink: 0; }
.ms-chevron-open { transform: rotate(180deg); }

.ms-card-body {
  border-top: 1px solid var(--border);
  padding: 6px 8px 8px 8px;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.ms-discover-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 8px;
  font-size: 12px;
  color: var(--text-muted);
  flex-wrap: wrap;
}
.ms-discover-hint-sub { font-size: 11px; color: var(--text-muted); font-style: italic; }

/* Metric row â€” CSS grid keeps columns aligned regardless of text length */
.ms-metric-row {
  display: grid;
  grid-template-columns: 18px minmax(140px, 220px) minmax(90px, 130px) 1fr auto;
  align-items: center;
  column-gap: 10px;
  padding: 7px 8px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12.5px;
}
.ms-metric-row:hover { background: var(--bg-card-hover); }

.ms-metric-row input[type="checkbox"] {
  accent-color: var(--accent);
  width: 14px;
  height: 14px;
}

.ms-metric-name {
  color: var(--text-primary);
  font-family: var(--font-mono);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ms-metric-stat {
  color: var(--text-muted);
  font-size: 11px;
  white-space: nowrap;
}
.ms-metric-desc {
  color: var(--text-secondary);
  font-size: 11.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ms-default-tag {
  font-size: 9.5px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: var(--accent-green);
  border: 1px solid var(--accent-green);
  border-radius: 4px;
  padding: 2px 6px;
  white-space: nowrap;
}

/* â”€â”€ Scrollbar polish â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.ms-list::-webkit-scrollbar { width: 8px; }
.ms-list::-webkit-scrollbar-track { background: transparent; }
.ms-list::-webkit-scrollbar-thumb { background: var(--border-md); border-radius: 8px; }
.ms-list::-webkit-scrollbar-thumb:hover { background: var(--border-bright); }

/* â”€â”€ Small screens â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
@media (max-width: 640px) {
  .ms-metric-row { grid-template-columns: 18px 1fr; row-gap: 2px; }
  .ms-metric-stat, .ms-metric-desc { grid-column: 2; font-size: 10.5px; }
  .ms-default-tag { grid-column: 2; justify-self: start; }
}

'@ | Set-Content -Path $cssPath -Encoding UTF8

Write-Host ""
Write-Host "Done. Rebuild the frontend to see it:" -ForegroundColor Green
Write-Host "  cd frontend"
Write-Host "  npm run build     # or: npm run dev"

