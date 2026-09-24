// src/hooks/useWebSocket.js
// Singleton per channel — prevents duplicate connections across components
import { useEffect, useRef, useState } from "react";

// wss:// when the page is served over HTTPS (browsers block ws:// from an https
// page as mixed content, which would silently kill live alert/overview updates).
const WS_BASE = `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/ws`;
const _sockets    = {};          // channel → WebSocket
const _listeners  = {};          // channel → Set of {onMsg, onStatus}
const _reconnect  = {};          // channel → timeout handle
const _backoffMs  = {};          // channel → current retry delay

const BACKOFF_INITIAL_MS = 1000;
const BACKOFF_MAX_MS     = 30000;

function getOrCreate(channel) {
  if (_sockets[channel] && _sockets[channel].readyState <= 1) return;

  const url    = `${WS_BASE}/${channel}`;
  const socket = new WebSocket(url);
  _sockets[channel] = socket;

  socket.onopen = () => {
    // A successful handshake means the server accepted us (valid
    // session cookie); reset backoff so the next transient drop
    // retries quickly rather than inheriting a long prior delay.
    _backoffMs[channel] = BACKOFF_INITIAL_MS;
    notifyStatus(channel, true);
    socket._ping = setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) socket.send("ping");
    }, 10000);
  };

  socket.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.type === "pong") return;
      notifyMsg(channel, data);
    } catch {}
  };

  socket.onclose = (event) => {
    clearInterval(socket._ping);
    notifyStatus(channel, false);
    delete _sockets[channel];

    // SECURITY/LOAD: previously this always retried after a flat 1s,
    // forever, with no way to stop -- a tab left open with an expired
    // or missing session cookie (login page, logged-out tab, expired
    // token) would hammer the server's WS handshake once a second
    // indefinitely, showing up server-side as a continuous stream of
    // rejected connections with no way to tell "stale client" apart
    // from "real outage" (see server log discussion this session).
    //
    // Fix: exponential backoff up to BACKOFF_MAX_MS, and stop
    // scheduling further attempts once nobody is listening on this
    // channel any more (all components using it have unmounted) --
    // no reason to keep a dead channel alive in the background.
    if (!_listeners[channel] || _listeners[channel].size === 0) {
      delete _backoffMs[channel];
      return;
    }

    // 4401 == invalid/missing session cookie (see app/main.py's
    // websocket_endpoint, close(code=4401)). AlertToast keeps a
    // listener mounted for the whole logged-in session, so without
    // this, an expired session with the tab still open would retry
    // this same rejected handshake forever (capped at 30s, but
    // forever) instead of ever giving up -- retrying can't fix an
    // auth failure; only a fresh login can. apiFetch's own 401
    // handling is what actually bounces the tab to /login once any
    // REST call runs, independent of this socket.
    if (event.code === 4401) {
      delete _backoffMs[channel];
      return;
    }

    const delay = _backoffMs[channel] || BACKOFF_INITIAL_MS;
    clearTimeout(_reconnect[channel]);
    _reconnect[channel] = setTimeout(() => getOrCreate(channel), delay);
    _backoffMs[channel] = Math.min(delay * 2, BACKOFF_MAX_MS);
  };

  socket.onerror = () => socket.close();
}

function notifyStatus(channel, connected) {
  (_listeners[channel] || new Set()).forEach(l => l.onStatus?.(connected));
}

function notifyMsg(channel, data) {
  (_listeners[channel] || new Set()).forEach(l => l.onMsg?.(data));
}

export function useWebSocket(channel) {
  const [isConnected, setIsConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState(null);
  const listenerRef = useRef(null);

  useEffect(() => {
    if (!_listeners[channel]) _listeners[channel] = new Set();

    const listener = {
      onMsg:    (data)      => setLastMessage(data),
      onStatus: (connected) => setIsConnected(connected),
    };
    listenerRef.current = listener;
    _listeners[channel].add(listener);

    // Set initial status
    const existing = _sockets[channel];
    if (existing?.readyState === WebSocket.OPEN) {
      setIsConnected(true);
    }

    // Create socket if needed (delayed to avoid StrictMode double-fire)
    const t = setTimeout(() => getOrCreate(channel), 50);

    return () => {
      clearTimeout(t);
      if (listenerRef.current) {
        _listeners[channel]?.delete(listenerRef.current);
      }
      // Don't close socket — other components may still use it
    };
  }, [channel]);

  return { isConnected, lastMessage };
}