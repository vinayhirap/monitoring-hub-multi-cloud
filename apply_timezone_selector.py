#!/usr/bin/env python3
"""
apply_timezone_selector.py — adds a global IST/UTC display-timezone
selector, using the exact same user-preference pattern the app
already uses for its dark/light theme toggle (a small control in the
topbar, persisted to localStorage, applied instantly with no reload).

WHAT THIS ADDS
  - frontend/src/contexts/TimezoneContext.jsx (new): a React context
    — TimezoneProvider + useTimezone() — holding the selected
    timezone ("IST" | "UTC"), its IANA name for toLocale*/Intl calls,
    and formatTime/formatDateTime helpers. Also exports a stateless
    formatInTz() for the handful of plain (non-component) date
    helpers elsewhere in the codebase that can't call a hook
    themselves.

  - frontend/src/App.jsx: wraps the app in <TimezoneProvider>, same
    level as the existing <AuthProvider>.

  - frontend/src/components/Layout.jsx: adds an actual <select> next
    to the existing dark/light toggle button in the topbar, and makes
    the topbar clock + sidebar "last updated" time respect the
    selection — previously the topbar clock was hardcoded to
    Asia/Kolkata (and labeled "IST" unconditionally) while the
    sidebar's last-updated time used the *browser's* local timezone
    with no explicit override, so the two could silently disagree.
    Both now read from the same selector.

  - frontend/src/components/Layout.css: styles the new <select> to
    match the existing theme-toggle button, for both dark and light
    themes.

  - frontend/src/pages/AccountDetail.jsx and ServiceDetail.jsx: the
    MetricChart components' X-axis/tooltip timestamps now format in
    the selected timezone instead of an unstated browser-local one —
    this is the actual "in charts" part of the ask.

  - frontend/src/pages/Alerts.jsx: alert "triggered at" timestamps.
  - frontend/src/pages/Overview.jsx and Compliance.jsx: the "Synced
    HH:MM:SS" indicators.

SCOPE BOUNDARY (documented, not an oversight): calendar-only fields
that show a date with no time-of-day — resource "Created"/"Last
Modified" in ServiceDetail/AccountDetail, the numeric audit-log entry
date in Compliance — are left as-is; an IST/UTC toggle has no visible
effect on a bare date except in a rare ~5.5h boundary case, so they
weren't worth threading a timezone parameter through every plain
helper function for. The audit-log TIMESTAMP column in Compliance.jsx
(formatUTC) is also deliberately left fixed to UTC on purpose — an
audit trail should read identically for every viewer, not shift with
whatever this selector happens to be set to. Both exceptions are
called out in the TimezoneContext.jsx docstring too.

PREREQUISITE: none beyond the existing frontend (React + Vite). This
is a frontend-only change — no backend/API/DB changes at all.

Usage:
    python apply_timezone_selector.py --dry-run
    python apply_timezone_selector.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-timezone-selector"


class PatchError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────
# New file: frontend/src/contexts/TimezoneContext.jsx
# ─────────────────────────────────────────────────────────────────────────
TIMEZONE_CONTEXT_JSX = r'''// src/contexts/TimezoneContext.jsx
/**
 * Global display-timezone selector — IST or UTC — the same
 * user-preference pattern Layout.jsx already uses for dark/light
 * theme (persisted to localStorage, applied instantly, no reload
 * needed). Every clock, chart timestamp, and "synced" label in the
 * app reads through here, so switching the selector updates all of
 * them at once instead of each page carrying its own hardcoded
 * timezone (which is what was happening before — the topbar clock
 * was hardcoded to Asia/Kolkata while charts silently used whatever
 * timezone the browser happened to be in).
 *
 * Deliberately NOT applied to calendar-only fields (e.g. resource
 * "Created" / "Last Modified" dates in ServiceDetail/AccountDetail,
 * or the numeric audit-log entry date in Compliance) — those render
 * a bare date with no time-of-day, so an IST vs UTC toggle has no
 * visible effect on them except in the rare case a timestamp falls
 * in the ~5.5h band where the calendar day actually differs, which
 * isn't worth threading a timezone parameter through every plain
 * (non-component) date-formatting helper in the codebase for.
 *
 * The audit-log entry TIMESTAMP in Compliance.jsx (formatUTC) is
 * ALSO deliberately left fixed to UTC regardless of this selector —
 * an audit trail should read identically for every viewer, in one
 * canonical timezone, not shift depending on who's looking at it.
 */
import { createContext, useContext, useState, useEffect } from "react";

export const TIMEZONE_OPTIONS = {
  IST: { label: "IST", ianaName: "Asia/Kolkata" },
  UTC: { label: "UTC", ianaName: "UTC" },
};

const STORAGE_KEY = "displayTimezone";

const TimezoneContext = createContext(null);

function readStoredTimezone() {
  const stored = localStorage.getItem(STORAGE_KEY);
  return stored === "UTC" ? "UTC" : "IST"; // IST is the default — matches the app's prior hardcoded Asia/Kolkata clock
}

/** Stateless helper — usable from plain (non-component) functions that
 * already have an ianaName in hand via a parameter or closure, without
 * needing to call the useTimezone() hook themselves (hooks can only be
 * called from inside a React component/hook, not a bare helper). */
export function formatInTz(input, ianaName, opts) {
  if (!input) return "";
  try {
    const d = input instanceof Date ? input : new Date(input);
    if (Number.isNaN(d.getTime())) return String(input);
    return d.toLocaleString("en-US", { timeZone: ianaName, ...opts });
  } catch {
    return String(input);
  }
}

export function TimezoneProvider({ children }) {
  const [timezone, setTimezone] = useState(readStoredTimezone);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, timezone);
  }, [timezone]);

  const ianaName = TIMEZONE_OPTIONS[timezone].ianaName;

  const value = {
    timezone,          // "IST" | "UTC" — drives the <select> in the topbar
    setTimezone,        // (tz: "IST" | "UTC") => void
    ianaName,           // "Asia/Kolkata" | "UTC" — pass straight into any toLocale*/Intl `timeZone` option
    formatTime(input, opts = {}) {
      return formatInTz(input, ianaName, {
        hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, ...opts,
      });
    },
    formatDateTime(input, opts = {}) {
      return formatInTz(input, ianaName, {
        month: "short", day: "numeric", year: "numeric",
        hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
        ...opts,
      });
    },
  };

  return <TimezoneContext.Provider value={value}>{children}</TimezoneContext.Provider>;
}

export function useTimezone() {
  const ctx = useContext(TimezoneContext);
  if (!ctx) {
    // Defensive fallback for a component rendered outside the
    // provider (shouldn't happen once App.jsx wraps everything, but
    // this keeps a stray usage from throwing and blanking the page —
    // it behaves as IST, the app's prior default).
    return {
      timezone: "IST", setTimezone: () => {}, ianaName: "Asia/Kolkata",
      formatTime: (input, opts = {}) => formatInTz(input, "Asia/Kolkata", {
        hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, ...opts,
      }),
      formatDateTime: (input, opts = {}) => formatInTz(input, "Asia/Kolkata", {
        month: "short", day: "numeric", year: "numeric",
        hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
        ...opts,
      }),
    };
  }
  return ctx;
}
'''

NEW_FILES = [
    ("frontend/src/contexts/TimezoneContext.jsx", TIMEZONE_CONTEXT_JSX),
]


# ─────────────────────────────────────────────────────────────────────────
# Patches to existing files — each (old, new) anchor is verified to
# match EXACTLY ONCE in the target file before anything is written
# (see preflight()).
# ─────────────────────────────────────────────────────────────────────────
PATCHES = [
    (
        "frontend/src/App.jsx",
        [
            (
                'import { AuthProvider, useAuth }  from "./auth/AuthContext";',
                'import { AuthProvider, useAuth }  from "./auth/AuthContext";\n'
                'import { TimezoneProvider } from "./contexts/TimezoneContext";',
            ),
            (
                """export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </AuthProvider>
  );
}""",
                """export default function App() {
  return (
    <TimezoneProvider>
      <AuthProvider>
        <BrowserRouter>
          <AppRoutes />
        </BrowserRouter>
      </AuthProvider>
    </TimezoneProvider>
  );
}""",
            ),
        ],
    ),
    (
        "frontend/src/components/Layout.jsx",
        [
            (
                'import AlertToast from "./AlertToast";\nimport "./Layout.css";',
                'import AlertToast from "./AlertToast";\n'
                'import { useTimezone, TIMEZONE_OPTIONS } from "../contexts/TimezoneContext";\n'
                'import "./Layout.css";',
            ),
            (
                "  const navigate          = useNavigate();\n",
                "  const navigate          = useNavigate();\n"
                "  const { timezone, setTimezone, ianaName } = useTimezone();\n",
            ),
            (
                """  const timeStr = now.toLocaleTimeString("en-IN", {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    timeZone: "Asia/Kolkata",
  });""",
                """  // Respects the IST/UTC selector in the topbar (TimezoneContext) —
  // previously hardcoded to Asia/Kolkata regardless of any preference.
  const timeStr = now.toLocaleTimeString("en-US", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    timeZone: ianaName,
  });""",
            ),
            (
                '              {now.toLocaleDateString("en-IN", { month: "short", day: "numeric", year: "numeric" })},{" "}',
                '              {now.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: ianaName })},{" "}',
            ),
            (
                """            <div className="topbar-clock">
              {timeStr} <span className="topbar-tz">IST</span>
            </div>""",
                """            <select
              className="tz-select"
              value={timezone}
              onChange={e => setTimezone(e.target.value)}
              title="Display timezone — applies to every clock and chart"
              aria-label="Display timezone"
            >
              {Object.entries(TIMEZONE_OPTIONS).map(([key, opt]) => (
                <option key={key} value={key}>{opt.label}</option>
              ))}
            </select>
            <div className="topbar-clock">
              {timeStr} <span className="topbar-tz">{timezone}</span>
            </div>""",
            ),
        ],
    ),
    (
        "frontend/src/components/Layout.css",
        [
            (
                """.btn-theme-toggle:hover {
  background: rgba(99,130,190,0.2);
  border-color: rgba(99,130,190,0.4);
}""",
                """.btn-theme-toggle:hover {
  background: rgba(99,130,190,0.2);
  border-color: rgba(99,130,190,0.4);
}

/* ── Timezone selector — same visual family as the theme toggle ── */
.tz-select {
  background: rgba(99,130,190,0.1);
  border: 1px solid rgba(99,130,190,0.2);
  border-radius: 6px;
  color: #a8bdd8;
  font-size: 11px;
  font-family: 'JetBrains Mono', monospace;
  height: 30px;
  padding: 0 6px;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
  flex-shrink: 0;
}
.tz-select:hover {
  background: rgba(99,130,190,0.2);
  border-color: rgba(99,130,190,0.4);
}
.tz-select option {
  background: #0b1220;
  color: #a8bdd8;
}""",
            ),
            (
                '[data-theme="light"] .btn-theme-toggle     { background: rgba(0,119,204,0.07); border-color: rgba(0,119,204,0.2); color: #2bb3ac; }',
                '[data-theme="light"] .btn-theme-toggle     { background: rgba(0,119,204,0.07); border-color: rgba(0,119,204,0.2); color: #2bb3ac; }\n'
                '[data-theme="light"] .tz-select            { background: rgba(0,119,204,0.07); border-color: rgba(0,119,204,0.2); color: #2bb3ac; }\n'
                '[data-theme="light"] .tz-select option     { background: #ffffff; color: #2bb3ac; }',
            ),
        ],
    ),
    (
        "frontend/src/pages/AccountDetail.jsx",
        [
            (
                'import "./AccountDetail.css";',
                'import "./AccountDetail.css";\n'
                'import { useTimezone } from "../contexts/TimezoneContext";',
            ),
            (
                """function MetricChart({ title, data, color, unit, threshold }) {
  if (!data || data.length === 0) {""",
                """function MetricChart({ title, data, color, unit, threshold }) {
  const { ianaName } = useTimezone();
  if (!data || data.length === 0) {""",
            ),
            (
                """  const latest = data[data.length - 1]?.v ?? 0;
  const formatted = data.map(d => ({
    t: new Date(d.t).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false }),
    v: d.v,
  }));""",
                """  const latest = data[data.length - 1]?.v ?? 0;
  const formatted = data.map(d => ({
    t: new Date(d.t).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: ianaName }),
    v: d.v,
  }));""",
            ),
        ],
    ),
    (
        "frontend/src/pages/ServiceDetail.jsx",
        [
            (
                '  XIcon, TagIcon, BarChartIcon, ToolIcon, ZapIcon,\n} from "../components/icons";',
                '  XIcon, TagIcon, BarChartIcon, ToolIcon, ZapIcon,\n} from "../components/icons";\n'
                'import { useTimezone } from "../contexts/TimezoneContext";',
            ),
            (
                """function MetricChart({ title, data, color, unit, threshold, thresholdLabel, timeRange }) {
  if (!data || data.length === 0) return (""",
                """function MetricChart({ title, data, color, unit, threshold, thresholdLabel, timeRange }) {
  const { ianaName } = useTimezone();
  if (!data || data.length === 0) return (""",
            ),
            (
                """  const latest = data[data.length - 1]?.v ?? 0;
  const formatted = data.map(d => ({
    t: new Date(d.t).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false }),
    v: d.v,
    ...(threshold ? { threshold } : {}),
  }));""",
                """  const latest = data[data.length - 1]?.v ?? 0;
  const formatted = data.map(d => ({
    t: new Date(d.t).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: ianaName }),
    v: d.v,
    ...(threshold ? { threshold } : {}),
  }));""",
            ),
        ],
    ),
    (
        "frontend/src/pages/Alerts.jsx",
        [
            (
                'import "./Alerts.css";',
                'import "./Alerts.css";\n'
                'import { useTimezone, formatInTz } from "../contexts/TimezoneContext";',
            ),
            (
                "  const { user } = useAuth();\n  const navigate = useNavigate();\n",
                "  const { user } = useAuth();\n  const navigate = useNavigate();\n"
                "  const { ianaName } = useTimezone();\n",
            ),
            (
                '''function shortDateTime(iso) {
  try {
    return new Date(iso).toLocaleString("en-US", {
      month:  "numeric",
      day:    "numeric",
      year:   "numeric",
      hour:   "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return iso;
  }
}''',
                '''function shortDateTime(iso, ianaName) {
  // Timezone-aware version of the previous browser-local formatter —
  // ianaName is threaded in from the calling component\'s useTimezone()
  // since this is a plain helper, not a component, and can\'t call
  // hooks itself.
  return formatInTz(iso, ianaName, {
    month:  "numeric",
    day:    "numeric",
    year:   "numeric",
    hour:   "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }) || iso;
}''',
            ),
            (
                '{a.triggered_at ? shortDateTime(a.triggered_at) : "—"}',
                '{a.triggered_at ? shortDateTime(a.triggered_at, ianaName) : "—"}',
            ),
        ],
    ),
    (
        "frontend/src/pages/Overview.jsx",
        [
            (
                'import "./Overview.css";',
                'import "./Overview.css";\n'
                'import { useTimezone } from "../contexts/TimezoneContext";',
            ),
            (
                "export default function Overview() {\n  const navigate = useNavigate();\n",
                "export default function Overview() {\n  const navigate = useNavigate();\n"
                "  const { ianaName } = useTimezone();\n",
            ),
            (
                '              Synced {lastSync.toLocaleTimeString()}',
                '              Synced {lastSync.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: ianaName })}',
            ),
        ],
    ),
    (
        "frontend/src/pages/Compliance.jsx",
        [
            (
                'import "./Compliance.css";',
                'import "./Compliance.css";\n'
                'import { useTimezone } from "../contexts/TimezoneContext";',
            ),
            (
                '  const isAdmin = (user?.role || "viewer").toLowerCase() === "admin";\n',
                '  const isAdmin = (user?.role || "viewer").toLowerCase() === "admin";\n'
                '  const { ianaName } = useTimezone();\n',
            ),
            (
                '<span className="bar-sync">· synced {lastFetch.toLocaleTimeString()}</span>',
                '<span className="bar-sync">· synced {lastFetch.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: ianaName })}</span>',
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
                problems.append(f"{rel_path}: anchor matched {count} times, expected exactly 1 — {old[:70]!r}")
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
            print("\nNo backend/API/DB changes — this is frontend-only, no server restart needed")
            print("beyond redeploying the built frontend assets.")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
