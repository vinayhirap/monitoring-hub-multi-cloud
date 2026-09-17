// src/contexts/TimezoneContext.jsx
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
 * UserManagement's "Created" column) — those render a bare date with
 * no time-of-day, so an IST vs UTC toggle has no visible effect on
 * them except in the rare case a timestamp falls in the ~5.5h band
 * where the calendar day actually differs, which isn't worth
 * threading a timezone parameter through every plain (non-component)
 * date-formatting helper in the codebase for.
 *
 * 2026-09-17: the audit-log entry TIMESTAMP in Compliance.jsx
 * (formerly formatTs's predecessor formatUTC, fixed to UTC regardless
 * of this selector) now DOES follow the toggle, same as every other
 * clock in the app, per explicit request — a per-viewer "audit trail
 * reads in a different zone than everything else on screen" was
 * confusing in practice. The underlying instant stored in the DB is
 * unchanged either way; this only changes which zone it's displayed
 * in.
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
