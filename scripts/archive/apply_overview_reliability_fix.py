#!/usr/bin/env python3
"""
apply_overview_reliability_fix.py — fixes two problems reported right
after the caching change: (1) the Overview page occasionally shows
"AWS Accounts (0) / No accounts found" for real, onboarded accounts —
self-correcting after a while or after navigating away and back — and
(2) the region row's remove ("✕") button gets clipped off-screen and
disappears on narrower account cards.

ROOT CAUSE OF (1) — cache poisoning by a failed fetch
loadAll() in Overview.jsx did:
    getLiveAccounts().catch(() => [])
Any failure — a network blip, the backend mid-restart, a slow cold
start right after deploying — silently became an empty array
indistinguishable from a real "zero accounts" response. That empty
array was then trusted enough to overwrite BOTH the on-screen state
AND the localStorage cache added for stale-while-revalidate. Once a
failure got cached, every subsequent page load hydrated from that
empty snapshot instantly (which is exactly why it "got faster" — the
cache genuinely was working, just caching the wrong thing) and stayed
wrong until the next successful poll (60s interval, or a manual
Refresh) happened to overwrite it. This is also why it looked
onboarding-related: an onboard-then-redirect-to-Overview navigation
is exactly when a request is likely to race a still-settling backend.

THE FIX: loadAll() now uses Promise.allSettled and only touches state
or the cache for whichever of accounts/alerts actually SUCCEEDED. A
failed fetch changes nothing on screen and writes nothing to the
cache — it just retries on the next interval tick, silently, the way
a transient blip should be handled. A new `loadError` flag distinguishes
"couldn't reach the server, showing last known state" from "fetch
succeeded and there are genuinely zero accounts" in the empty-state
message, so a transient failure no longer reads as "you have nothing
onboarded."

ROOT CAUSE OF (2) — the region row layout
Each region row crams a status dot, region name, up to 4 resource
chips, alert badges, a "Services →" label, and the delete button into
one `display:flex` row with `flex-wrap` only on the middle chips
container. On a narrow account card, everything after the chips can
run out of horizontal room; nothing in that flex row is allowed to
wrap the trailing items onto a second line, so the delete button (last
in source order) is the first thing to lose the fight for space.

THE FIX: the row is restructured into two wrap-safe groups —
`.region-row-main` (dot + name + chips) and `.region-row-actions`
(alert badges + delete button, pinned right via margin-left:auto) —
with `flex-wrap: wrap` on the outer row. If space is tight, the whole
actions group now wraps to its own line below the chips instead of
being pushed off the edge and disappearing. The delete button also
gets a faint resting-state background so it reads as a control at a
glance, not just on hover.

Usage:
    python apply_overview_reliability_fix.py --dry-run
    python apply_overview_reliability_fix.py
"""
import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-overview-reliability-fix"


class PatchError(Exception):
    pass


PATCHES = [
    (
        "frontend/src/pages/Overview.jsx",
        [
            (
                r'''  const [accounts,    setAccounts]    = useState([]);
  const [alerts,      setAlerts]      = useState([]);
  const [loading,     setLoading]     = useState(true);
  const [revalidating, setRevalidating] = useState(false);
  const [filter,      setFilter]      = useState("All");
  const [lastSync,    setLastSync]    = useState(null);
  const [expandedIds, setExpandedIds] = useState(new Set());

  const deletedIds = useRef(new Set());
  const { lastMessage: alertMsg } = useWebSocket("alerts");

  const loadAll = useCallback(async () => {
    setRevalidating(true);
    try {
      const [accs, als] = await Promise.all([
        getLiveAccounts().catch(() => []),
        getAlerts().catch(() => []),
      ]);
      const filtered = (Array.isArray(accs) ? accs : [])
        .filter(a => !deletedIds.current.has(a.id));
      const freshAlerts = Array.isArray(als) ? als : [];
      setAccounts(filtered);
      setAlerts(freshAlerts);
      setLastSync(new Date());
      setCached("overview:accounts_alerts", { accounts: filtered, alerts: freshAlerts });
    } finally {
      setLoading(false);
      setRevalidating(false);
    }
  }, []);

  // Hydrate instantly from whatever was cached last time this page
  // loaded -- lets the dashboard render immediately on open/reload
  // instead of sitting on "Fetching live AWS data..." every time,
  // while loadAll() below still fetches fresh data in the background
  // and replaces it (and the cache) as soon as it arrives.
  useEffect(() => {
    const cached = getCached("overview:accounts_alerts");
    if (!cached) return;
    const filtered = (cached.data.accounts || []).filter(a => !deletedIds.current.has(a.id));
    setAccounts(filtered);
    setAlerts(cached.data.alerts || []);
    setLastSync(new Date(cached.ts));
    setLoading(false);
  }, []);
''',
                r'''  const OVERVIEW_CACHE_KEY = "overview:accounts_alerts";

  const [accounts,    setAccounts]    = useState([]);
  const [alerts,      setAlerts]      = useState([]);
  const [loading,     setLoading]     = useState(true);
  const [revalidating, setRevalidating] = useState(false);
  // True only when the MOST RECENT fetch attempt failed outright (network
  // error, backend unreachable) -- lets the empty state say "couldn't
  // reach the server" instead of the misleading "no accounts found" when
  // accounts genuinely exist but the last request just didn't succeed.
  const [loadError,   setLoadError]   = useState(false);
  const [filter,      setFilter]      = useState("All");
  const [lastSync,    setLastSync]    = useState(null);
  const [expandedIds, setExpandedIds] = useState(new Set());

  const deletedIds = useRef(new Set());
  const { lastMessage: alertMsg } = useWebSocket("alerts");

  const loadAll = useCallback(async () => {
    setRevalidating(true);
    // Promise.allSettled (not Promise.all + .catch(() => [])) is the whole
    // fix here: a failed request must NEVER be indistinguishable from a
    // successful one that happens to return zero rows. Each of
    // accounts/alerts only updates state (and the cache) for the specific
    // one that actually succeeded -- a transient failure changes nothing
    // on screen and leaves the cache untouched, rather than overwriting
    // good data with an empty snapshot that then sticks around on every
    // reload until the next successful poll happens to fix it.
    const [accResult, alertResult] = await Promise.allSettled([getLiveAccounts(), getAlerts()]);

    if (accResult.status === "fulfilled") {
      const filtered = (Array.isArray(accResult.value) ? accResult.value : [])
        .filter(a => !deletedIds.current.has(a.id));
      setAccounts(filtered);
      setLoadError(false);
      const cachedAlerts = getCached(OVERVIEW_CACHE_KEY)?.data?.alerts || [];
      setCached(OVERVIEW_CACHE_KEY, {
        accounts: filtered,
        alerts: alertResult.status === "fulfilled" && Array.isArray(alertResult.value)
          ? alertResult.value
          : cachedAlerts,
      });
    } else {
      console.error("Overview: accounts fetch failed, keeping last known data:", accResult.reason);
      setLoadError(true);
    }

    if (alertResult.status === "fulfilled") {
      setAlerts(Array.isArray(alertResult.value) ? alertResult.value : []);
    } else {
      console.error("Overview: alerts fetch failed, keeping last known data:", alertResult.reason);
    }

    setLastSync(new Date());
    setLoading(false);
    setRevalidating(false);
  }, []);

  // Hydrate instantly from whatever was cached last time this page
  // loaded -- lets the dashboard render immediately on open/reload
  // instead of sitting on "Fetching live AWS data..." every time,
  // while loadAll() below still fetches fresh data in the background
  // and replaces it (and the cache) as soon as it arrives.
  useEffect(() => {
    const cached = getCached(OVERVIEW_CACHE_KEY);
    if (!cached) return;
    const filtered = (cached.data.accounts || []).filter(a => !deletedIds.current.has(a.id));
    setAccounts(filtered);
    setAlerts(cached.data.alerts || []);
    setLastSync(new Date(cached.ts));
    setLoading(false);
  }, []);
''',
            ),
            (
                r'''      {loading ? (
        <div className="ov-loading"><span className="spin">◌</span> Fetching live AWS data…</div>
      ) : filteredGroups.length === 0 ? (
        <div className="ov-empty">
          No accounts found.{" "}
          <span className="ov-link" onClick={() => navigate("/onboarding")}>Onboard an account →</span>
        </div>
      ) : (
''',
                r'''      {loading ? (
        <div className="ov-loading"><span className="spin">◌</span> Fetching live AWS data…</div>
      ) : filteredGroups.length === 0 && loadError ? (
        <div className="ov-empty">
          Couldn't reach the server just now — showing the last known state.{" "}
          <span className="ov-link" onClick={loadAll}>Retry →</span>
        </div>
      ) : filteredGroups.length === 0 ? (
        <div className="ov-empty">
          No accounts found.{" "}
          <span className="ov-link" onClick={() => navigate("/onboarding")}>Onboard an account →</span>
        </div>
      ) : (
''',
            ),
            (
                r'''function RegionRow({ regionRow, onClick, onDelete }) {
  const status = regionRow.status || "healthy";

  // Same authoritative per-region counts the backend used to set
  // regionRow.status -- no more independent (and fragile) re-derivation
  // of severity from a raw alerts list here.
  const critical = regionRow.critical_alerts || 0;
  const warning  = regionRow.warning_alerts  || 0;

  const statusClass = {
    healthy:  "region-row-healthy",
    warning:  "region-row-warning",
    critical: "region-row-critical",
  }[status] || "region-row-healthy";

  const dotColor = { healthy: "#22c55e", warning: "#f59e0b", critical: "#ef4444" }[status] || "#22c55e";

  return (
    <div
      onClick={onClick}
      className={`region-row ${statusClass}`}
    >
      {/* Status dot */}
      <span style={{
        width: 8, height: 8, borderRadius: "50%",
        background: dotColor, flexShrink: 0,
        boxShadow: `0 0 6px ${dotColor}80`,
      }} />

      {/* Region name */}
      <span className="region-row-name">
        {regionRow.region}
      </span>

      {/* Resource chips — compact */}
      <div style={{ display: "flex", gap: 5, flex: 1, flexWrap: "wrap" }}>
        <MiniChip label="EC2"    value={regionRow.ec2_total}    sub={`${regionRow.ec2_running}▶`} />
        <MiniChip label="EBS"    value={regionRow.ebs_total}    />
        <MiniChip label="S3"     value={regionRow.s3_total}     />
        <MiniChip label="λ"      value={regionRow.lambda_total} />
      </div>

      {/* Alert badges */}
      <div style={{ display: "flex", gap: 4, flexShrink: 0 }}>
        {critical > 0 && (
          <span style={{ fontSize: 10, color: "#ef4444", background: "rgba(239,68,68,0.15)", borderRadius: 4, padding: "1px 5px", fontWeight: 700 }}>
            ● {critical}
          </span>
        )}
        {warning > 0 && (
          <span style={{ fontSize: 10, color: "#f59e0b", background: "rgba(245,158,11,0.15)", borderRadius: 4, padding: "1px 5px", fontWeight: 700 }}>
            ⚠ {warning}
          </span>
        )}
      </div>

      <span style={{ fontSize: 11, color: "var(--text-muted)", flexShrink: 0 }}>Services →</span>

      <button
        className="btn-delete-sm"
        onClick={onDelete}
        title="Remove region"
        style={{ flexShrink: 0 }}
      >✕</button>
    </div>
  );
}
''',
                r'''function RegionRow({ regionRow, onClick, onDelete }) {
  const status = regionRow.status || "healthy";

  // Same authoritative per-region counts the backend used to set
  // regionRow.status -- no more independent (and fragile) re-derivation
  // of severity from a raw alerts list here.
  const critical = regionRow.critical_alerts || 0;
  const warning  = regionRow.warning_alerts  || 0;

  const statusClass = {
    healthy:  "region-row-healthy",
    warning:  "region-row-warning",
    critical: "region-row-critical",
  }[status] || "region-row-healthy";

  const dotColor = { healthy: "#22c55e", warning: "#f59e0b", critical: "#ef4444" }[status] || "#22c55e";

  return (
    <div
      onClick={onClick}
      className={`region-row ${statusClass}`}
    >
      {/* Left group: dot + name + resource chips -- allowed to wrap.
          Right group (alert badges + delete) is a separate flex item
          pinned to the end via margin-left:auto on region-row-actions;
          if the row is too narrow for both, the WHOLE actions group
          wraps to its own line below instead of the delete button
          silently losing the fight for space and disappearing off the
          edge, which is what happened when everything shared one
          nowrap-by-default flex row. */}
      <div className="region-row-main">
        <span style={{
          width: 8, height: 8, borderRadius: "50%",
          background: dotColor, flexShrink: 0,
          boxShadow: `0 0 6px ${dotColor}80`,
        }} />
        <span className="region-row-name">
          {regionRow.region}
        </span>
        <div className="region-row-chips">
          <MiniChip label="EC2"    value={regionRow.ec2_total}    sub={`${regionRow.ec2_running}▶`} />
          <MiniChip label="EBS"    value={regionRow.ebs_total}    />
          <MiniChip label="S3"     value={regionRow.s3_total}     />
          <MiniChip label="λ"      value={regionRow.lambda_total} />
        </div>
      </div>

      <div className="region-row-actions">
        {critical > 0 && (
          <span className="region-alert-badge region-alert-critical">● {critical}</span>
        )}
        {warning > 0 && (
          <span className="region-alert-badge region-alert-warning">⚠ {warning}</span>
        )}
        <span className="region-row-goto">Services →</span>
        <button
          className="btn-delete-sm"
          onClick={onDelete}
          title="Remove region"
          aria-label="Remove region"
        >✕</button>
      </div>
    </div>
  );
}
''',
            ),
        ],
    ),
    (
        "frontend/src/pages/Overview.css",
        [
            (
                r'''/* ── Region rows (drilldown) ── */
.region-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: opacity 0.15s;
  border: 1px solid transparent;
}
.region-row:hover { opacity: 0.82; }

.region-row-healthy  { background: rgba(34,197,94,0.06);  border-color: rgba(34,197,94,0.18); }
.region-row-warning  { background: rgba(245,158,11,0.06); border-color: rgba(245,158,11,0.18); }
.region-row-critical { background: rgba(239,68,68,0.06); border-color: rgba(239,68,68,0.18); }

.region-row-name {
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 700;
  color: var(--text-primary);
  min-width: 110px;
}
''',
                r'''/* ── Region rows (drilldown) ──
   Two wrap-safe groups instead of one flat nowrap row: region-row-main
   (dot/name/chips) grows and wraps freely; region-row-actions (alert
   badges + delete) is pinned to the end via margin-left:auto and, if
   the row is too narrow for both groups on one line, wraps as a whole
   onto its own line below rather than clipping the delete button off
   the edge. */
.region-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 10px;
  padding: 9px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: opacity 0.15s;
  border: 1px solid transparent;
}
.region-row:hover { opacity: 0.82; }

.region-row-healthy  { background: rgba(34,197,94,0.06);  border-color: rgba(34,197,94,0.18); }
.region-row-warning  { background: rgba(245,158,11,0.06); border-color: rgba(245,158,11,0.18); }
.region-row-critical { background: rgba(239,68,68,0.06); border-color: rgba(239,68,68,0.18); }

.region-row-main {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  flex: 1 1 auto;
  min-width: 0;
}

.region-row-chips {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}

.region-row-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  margin-left: auto;
}

.region-row-goto {
  font-size: 11px;
  color: var(--text-muted);
  white-space: nowrap;
}

.region-alert-badge {
  font-size: 10px;
  font-weight: 700;
  border-radius: 4px;
  padding: 1px 5px;
  white-space: nowrap;
}
.region-alert-critical { color: #ef4444; background: rgba(239,68,68,0.15); }
.region-alert-warning  { color: #f59e0b; background: rgba(245,158,11,0.15); }

.region-row-name {
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 700;
  color: var(--text-primary);
  min-width: 110px;
}
''',
            ),
            (
                r'''.btn-delete-sm { background:none; border:1px solid transparent; color:var(--text-muted); width:22px; height:22px; border-radius:4px; font-size:12px; display:flex; align-items:center; justify-content:center; transition:all .15s; }
.btn-delete-sm:hover { border-color:var(--red); color:var(--red); }''',
                r'''.btn-delete-sm { background:rgba(255,255,255,0.04); border:1px solid var(--border); color:var(--text-muted); width:22px; height:22px; border-radius:4px; font-size:12px; display:flex; align-items:center; justify-content:center; transition:all .15s; }
.btn-delete-sm:hover { background:rgba(239,68,68,0.12); border-color:var(--red); color:var(--red); }''',
            ),
        ],
    ),
]

# ─────────────────────────────────────────────────────────────────────────
# Preflight / apply / validate
# ─────────────────────────────────────────────────────────────────────────

def preflight():
    print("=== Pre-flight: verifying anchors match exactly ===")
    problems = []

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        for old, _new in replacements:
            count = text.count(old)
            if count == 0:
                problems.append(f"{rel_path}: anchor not found (0 matches) — {old[:70]!r}")
            elif count > 1:
                problems.append(f"{rel_path}: anchor matched {count} times, expected exactly 1")
            else:
                print(f"  OK  {rel_path}: anchor matched exactly once")

    if problems:
        print("\n".join(problems))

        def _already(rel, new_text):
            p = REPO_ROOT / rel
            return p.exists() and new_text in p.read_text(encoding="utf-8")

        already_applied = all(_already(rel, new) for rel, repls in PATCHES for _old, new in repls)
        if already_applied:
            print("\nAll target text already present — patch appears to be already applied. Nothing to do.")
            sys.exit(0)
        raise PatchError("Pre-flight failed — aborting before touching any file. See problems above.")
    print("Pre-flight OK.\n")


def apply_all(dry_run: bool):
    changed_files = []
    report = []

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        text = full_path.read_text(encoding="utf-8")
        original_text = text
        for old, new in replacements:
            if new in text:
                continue  # already patched
            if old not in text:
                raise PatchError(f"{rel_path}: expected anchor vanished mid-patch — aborting")
            text = text.replace(old, new, 1)

        if text == original_text:
            continue

        if dry_run:
            report.append(f"[DRY RUN] would patch: {rel_path}")
        else:
            backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
            if not backup_path.exists():
                shutil.copy2(full_path, backup_path)
            full_path.write_text(text, encoding="utf-8")
            report.append(f"PATCHED: {rel_path}  (backup: {backup_path.name})")
            changed_files.append(full_path)

    for line in report:
        print(line)

    return changed_files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        preflight()
        changed = apply_all(args.dry_run)
        if not args.dry_run:
            print(f"\n=== Done. {len(changed)} file(s) touched. ===")
            print("\nNext steps:")
            print("  cd frontend && npm install (if needed) && npm run build")
            print("\nNo backend changes — this is frontend-only.")
            print("\nTo verify:")
            print("  - Reload Overview a few times; a transient failure (e.g. stop")
            print("    the backend briefly) should leave the last good data on")
            print("    screen with a 'Couldn't reach the server' message instead of")
            print("    wiping it to 'No accounts found'.")
            print("  - Expand an account card's regions and shrink the browser")
            print("    window / zoom in — the ✕ remove button should always stay")
            print("    visible, wrapping to its own line rather than disappearing.")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
