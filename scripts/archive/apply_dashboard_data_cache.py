#!/usr/bin/env python3
"""
apply_dashboard_data_cache.py — fixes the "Fetching live AWS data..."
(and equivalent) full-page loading wait on every page load/reload by
adding a localStorage-backed stale-while-revalidate cache: whatever was
shown last time renders INSTANTLY, while a fresh live fetch still runs
in the background and replaces it (and the cache) the moment it
arrives. A genuine first-ever visit, with nothing cached yet, still
shows the normal loading state — there's nothing to show early in
that case.

WHAT THIS ADDS

  - frontend/src/utils/dataCache.js (new): getCached(key) / setCached(key,
    data) / clearCached(key) — a small localStorage wrapper. Entries
    older than 24h are treated as absent rather than shown (avoids ever
    displaying ancient data if something's been broken a while).
    Deliberately scoped to the slow "list of things" fetches (account
    list, instance list, resource list) — NOT the per-instance
    CloudWatch chart time-series fetches, which are already fast and
    depend on a specific selection + time range.

  - frontend/src/pages/Overview.jsx: hydrates the account/alert grid from
    cache on mount (skips the "Fetching live AWS data..." placeholder
    when there's something to show), and adds a small non-blocking
    "· updating..." indicator next to the Synced time while a
    background refresh is in flight.

  - frontend/src/pages/AccountDetail.jsx: hydrates the EC2 instance table
    from cache, keyed per account id — switching accounts (or
    reloading) shows that account's last-known instance list instantly
    instead of "Loading instances...", and correctly falls back to the
    normal loading state when there's no cache yet for that specific
    account (so you never see instances from a DIFFERENT account
    displayed under the wrong URL while the real fetch is in flight).

  - frontend/src/pages/ServiceDetail.jsx: same pattern for the per-service
    resource table, keyed per account id + service.

Usage:
    python apply_dashboard_data_cache.py --dry-run
    python apply_dashboard_data_cache.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-dashboard-data-cache"


class PatchError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────
# New file: frontend/src/utils/dataCache.js
# ─────────────────────────────────────────────────────────────────────────
DATA_CACHE_JS = r'''// src/utils/dataCache.js
/**
 * Lightweight stale-while-revalidate cache for the AWS-data list/dashboard
 * fetches that show a full-page "Fetching live AWS data…" (or equivalent)
 * placeholder on every load or reload — Overview's account+alert list,
 * AccountDetail's EC2 instance list, ServiceDetail's per-service resource
 * list. Backed by localStorage so it survives a full page reload, not
 * just client-side navigation.
 *
 * Pattern: on mount (or when the page's key params change — account id,
 * service), read whatever's cached and render it immediately with no
 * spinner, while a fresh fetch runs in the background; when the fresh
 * fetch resolves, swap it in and refresh the cache. On a genuine
 * first-ever visit (nothing cached yet) the page still shows its normal
 * loading state — there's nothing to show early in that case.
 *
 * Deliberately NOT used for per-instance CloudWatch chart time-series
 * fetches (MetricChart data) — those depend on a specific resource +
 * time-range selection, change constantly, and are already fast; the
 * caching payoff there is much lower and the key space far larger. This
 * only covers the slow "list of things" fetches that reliably show the
 * same shape of data on every reload.
 */
const PREFIX = "mh_cache:";
const MAX_AGE_MS = 24 * 60 * 60 * 1000; // ignore anything older than this rather than show ancient data

export function getCached(key) {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed.ts !== "number") return null;
    if (Date.now() - parsed.ts > MAX_AGE_MS) {
      localStorage.removeItem(PREFIX + key);
      return null;
    }
    return parsed; // { data, ts }
  } catch {
    return null;
  }
}

export function setCached(key, data) {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify({ data, ts: Date.now() }));
  } catch {
    // localStorage full, disabled, or unavailable (private browsing) —
    // caching is a nice-to-have here, never something a live fetch
    // should fail or block on.
  }
}

export function clearCached(key) {
  try {
    localStorage.removeItem(PREFIX + key);
  } catch {
    // same as above — never let cache cleanup throw
  }
}
'''

NEW_FILES = [
    ("frontend/src/utils/dataCache.js", DATA_CACHE_JS),
]


PATCHES = [
    (
        "frontend/src/pages/Overview.jsx",
        [
            (
                'import { useTimezone } from "../contexts/TimezoneContext";\n',
                'import { useTimezone } from "../contexts/TimezoneContext";\n'
                'import { getCached, setCached } from "../utils/dataCache";\n',
            ),
            (
                """  const [accounts,    setAccounts]    = useState([]);
  const [alerts,      setAlerts]      = useState([]);
  const [loading,     setLoading]     = useState(true);
  const [filter,      setFilter]      = useState("All");
  const [lastSync,    setLastSync]    = useState(null);
  const [expandedIds, setExpandedIds] = useState(new Set());

  const deletedIds = useRef(new Set());
  const { lastMessage: alertMsg } = useWebSocket("alerts");

  const loadAll = useCallback(async () => {
    try {
      const [accs, als] = await Promise.all([
        getLiveAccounts().catch(() => []),
        getAlerts().catch(() => []),
      ]);
      const filtered = (Array.isArray(accs) ? accs : [])
        .filter(a => !deletedIds.current.has(a.id));
      setAccounts(filtered);
      setAlerts(Array.isArray(als) ? als : []);
      setLastSync(new Date());
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAll();
    const t = setInterval(loadAll, 60000);""",
                """  const [accounts,    setAccounts]    = useState([]);
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

  useEffect(() => {
    loadAll();
    const t = setInterval(loadAll, 60000);""",
            ),
            (
                '              Synced {lastSync.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: ianaName })}',
                '              Synced {lastSync.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: ianaName })}\n'
                '              {revalidating && <span style={{ marginLeft: 6, opacity: 0.7 }}>\u00b7 updating\u2026</span>}',
            ),
        ],
    ),
    (
        "frontend/src/pages/AccountDetail.jsx",
        [
            (
                'import { useTimezone } from "../contexts/TimezoneContext";\n',
                'import { useTimezone } from "../contexts/TimezoneContext";\n'
                'import { getCached, setCached } from "../utils/dataCache";\n',
            ),
            (
                """  useEffect(() => {
    loadInstances();
    const t = setInterval(loadInstances, 30000);
    return () => clearInterval(t);
  }, [id]);

  async function loadInstances() {
    try {
      const data = await getLiveEC2(id);
      setInstances(Array.isArray(data) ? data : []);
    } catch (e) {
      console.error("EC2 load error:", e);
    } finally {
      setLoading(false);
    }
  }""",
                """  useEffect(() => {
    // Hydrate instantly from whatever was cached for THIS account last
    // time, so switching accounts (or reloading this page) doesn't sit
    // on "Loading instances..." every time -- loadInstances() below
    // still fetches fresh data in the background and replaces both the
    // displayed list and the cache as soon as it arrives. Falls back to
    // a normal loading state when there's nothing cached yet for this
    // specific account id (never shows a DIFFERENT account's leftover
    // instances under this URL).
    const cached = getCached(`account:${id}:ec2`);
    if (cached) {
      setInstances(Array.isArray(cached.data) ? cached.data : []);
      setLoading(false);
    } else {
      setInstances([]);
      setLoading(true);
    }
    loadInstances();
    const t = setInterval(loadInstances, 30000);
    return () => clearInterval(t);
  }, [id]);

  async function loadInstances() {
    try {
      const data = await getLiveEC2(id);
      const list = Array.isArray(data) ? data : [];
      setInstances(list);
      setCached(`account:${id}:ec2`, list);
    } catch (e) {
      console.error("EC2 load error:", e);
    } finally {
      setLoading(false);
    }
  }""",
            ),
        ],
    ),
    (
        "frontend/src/pages/ServiceDetail.jsx",
        [
            (
                'import { useTimezone } from "../contexts/TimezoneContext";\n',
                'import { useTimezone } from "../contexts/TimezoneContext";\n'
                'import { getCached, setCached } from "../utils/dataCache";\n',
            ),
            (
                """  const loadRows = useCallback(async () => {
    if (notImplRef.current) return;
    if (!service) {
      // Unsupported/unmapped service (GCP/Azure, or an AWS directory-tier
      // service without a live-data endpoint yet) -- don't even attempt a
      // fetch, go straight to the same graceful NotImplState AWS services
      // without endpoints already use.
      notImplRef.current = true;
      setNotImpl(true);
      setRows([]);
      setLoading(false);
      return;
    }
    setError(null);
    try {
      const data = await fetchService(id, service);
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {""",
                """  const loadRows = useCallback(async () => {
    if (notImplRef.current) return;
    if (!service) {
      // Unsupported/unmapped service (GCP/Azure, or an AWS directory-tier
      // service without a live-data endpoint yet) -- don't even attempt a
      // fetch, go straight to the same graceful NotImplState AWS services
      // without endpoints already use.
      notImplRef.current = true;
      setNotImpl(true);
      setRows([]);
      setLoading(false);
      return;
    }
    setError(null);
    try {
      const data = await fetchService(id, service);
      const list = Array.isArray(data) ? data : [];
      setRows(list);
      setCached(`service:${id}:${service}`, list);
    } catch (e) {""",
            ),
            (
                """  useEffect(() => {
    notImplRef.current = false;
    setNotImpl(false);
    setLoading(true);
    setRows([]);
    setError(null);
    loadRows();
    const t = setInterval(() => { if (!notImplRef.current) loadRows(); }, 15000);
    return () => clearInterval(t);
  }, [loadRows]);""",
                """  useEffect(() => {
    notImplRef.current = false;
    setNotImpl(false);
    setError(null);
    // Hydrate instantly from whatever was cached for this
    // account+service last time -- switching services/accounts (or
    // reloading) doesn't have to sit on "Loading..." every time.
    // loadRows() below still fetches fresh data in the background and
    // replaces both the table and the cache once it arrives.
    const cached = service ? getCached(`service:${id}:${service}`) : null;
    if (cached) {
      setRows(Array.isArray(cached.data) ? cached.data : []);
      setLoading(false);
    } else {
      setRows([]);
      setLoading(true);
    }
    loadRows();
    const t = setInterval(() => { if (!notImplRef.current) loadRows(); }, 15000);
    return () => clearInterval(t);
  }, [loadRows, id, service]);""",
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

    for rel_path, content in NEW_FILES:
        full_path = REPO_ROOT / rel_path
        if full_path.exists():
            print(f"  (already exists, will skip creating) {rel_path}")
        else:
            print(f"  OK  {rel_path}: will be created")

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

        already_applied = (
            all(_already(rel, content) for rel, content in NEW_FILES)
            and all(_already(rel, new) for rel, repls in PATCHES for _old, new in repls)
        )
        if already_applied:
            print("\nAll target text already present — patch appears to be already applied. Nothing to do.")
            sys.exit(0)
        raise PatchError("Pre-flight failed — aborting before touching any file. See problems above.")
    print("Pre-flight OK.\n")


def apply_all(dry_run: bool):
    changed_files = []
    report = []

    for rel_path, content in NEW_FILES:
        full_path = REPO_ROOT / rel_path
        if full_path.exists():
            continue
        if dry_run:
            report.append(f"[DRY RUN] would create: {rel_path}")
        else:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            report.append(f"CREATED: {rel_path}")
            changed_files.append(full_path)

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
            print("  cd frontend")
            print("  npm install    # only if node_modules isn't already present")
            print("  npm run build  # or: npm run dev, for local preview")
            print("\nNo backend/API/DB changes — this is frontend-only.")
            print("\nTo verify: open Overview, wait for a real load, reload the page —")
            print("the account grid should now appear instantly (with a small")
            print("'· updating…' hint while it refreshes) instead of showing the")
            print("full 'Fetching live AWS data…' placeholder every time.")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
