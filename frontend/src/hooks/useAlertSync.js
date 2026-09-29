// src/hooks/useAlertSync.js
// ONE shared "the alert picture changed" signal for every page (2026-09-29).
//
// Before: the Alerts page polled every 10 s, the sidebar badge and Services tiles
// every 30 s, resource badges every 30 s and the Overview every 60 s -- each on
// its own timer -- so a new or resolved alert showed up on the Alerts page
// first and on the Overview minutes later.
//
// Now: a single watcher asks GET /api/alerts/version (a hash of the open-alert
// state, identical from either uvicorn worker) every POLL_MS, and also the
// instant the alerts websocket delivers anything. When the version changes, EVERY
// mounted subscriber is called in the same tick, so all screens refetch together.
// Each page keeps its own slower poll only as a safety net for purely time-based
// changes (an alert going stale) that alter no row.
import { useEffect, useRef } from "react";
import { getAlertsVersion } from "../api/api";
import { useWebSocket } from "./useWebSocket";

const POLL_MS = 5000;

const listeners = new Set();
let lastVersion = null;
let timer = null;
let checking = false;

async function check() {
  if (checking || document.hidden) return;   // hidden tabs re-check when shown
  checking = true;
  try {
    const { version } = await getAlertsVersion();
    if (lastVersion !== null && version !== lastVersion) {
      listeners.forEach(fn => { try { fn(version); } catch { /* one bad page must not stop the rest */ } });
    }
    lastVersion = version;
  } catch {
    /* transient failure: keep the last version, the next tick retries */
  } finally {
    checking = false;
  }
}

function onVisible() { if (!document.hidden) check(); }

function start() {
  if (timer) return;
  check();
  timer = setInterval(check, POLL_MS);
  document.addEventListener("visibilitychange", onVisible);
  window.addEventListener("focus", check);
}

function stop() {
  if (!timer) return;
  clearInterval(timer);
  timer = null;
  lastVersion = null;
  document.removeEventListener("visibilitychange", onVisible);
  window.removeEventListener("focus", check);
}

/**
 * useAlertSync(onChange): call onChange() whenever alert state changes anywhere.
 * Not called on mount -- the page does its own initial load.
 */
export function useAlertSync(onChange, { enabled = true } = {}) {
  const ref = useRef(onChange);
  // keep the latest callback without writing a ref during render
  useEffect(() => { ref.current = onChange; });
  const { lastMessage } = useWebSocket("alerts");

  useEffect(() => {
    if (!enabled) return undefined;
    const fn = () => ref.current && ref.current();
    listeners.add(fn);
    if (listeners.size === 1) start();
    return () => {
      listeners.delete(fn);
      if (listeners.size === 0) stop();
    };
  }, [enabled]);

  // Websocket push: confirm against the server version right now; the version
  // check then fans out to every subscriber at once.
  useEffect(() => {
    if (enabled && lastMessage) check();
  }, [lastMessage, enabled]);
}
