// src/components/AlertToast.jsx
import { useEffect, useRef, useState } from "react";
import { useWebSocket } from "../hooks/useWebSocket";
import { AlertOctagonIcon, AlertTriangleIcon, XIcon } from "./icons";
import { metricLabel, formatMetricValue } from "../utils/metricLabels";
import "./AlertToast.css";

const SOUND_URL = "https://actions.google.com/sounds/v1/alarms/alarm_clock.ogg";

export default function AlertToast() {
  const [toasts, setToasts]       = useState([]);
  const audioRef                  = useRef(null);
  const { lastMessage: alertMsg } = useWebSocket("alerts");

  // Init audio
  useEffect(() => {
    audioRef.current = new Audio(SOUND_URL);
    audioRef.current.volume = 0.7;
  }, []);

  // React to new WS alert
  useEffect(() => {
    if (!alertMsg || alertMsg.type !== "new_alert") return;
    const toast = {
      id:          Date.now(),
      severity:    alertMsg.severity   || "WARNING",
      metric:      alertMsg.metric     || "Unknown",
      value:       alertMsg.value      ?? 0,
      threshold:   alertMsg.threshold  ?? 0,
      account_id:  alertMsg.account_id,
      account_name:alertMsg.account_name,
      region:      alertMsg.region,
    };
    setToasts(prev => [toast, ...prev].slice(0, 5));

    // Beep on CRITICAL
    if ((alertMsg.severity || "").toUpperCase() === "CRITICAL") {
      try { audioRef.current?.play().catch(() => {}); } catch {}
    }

    // Auto-dismiss after 8s
    setTimeout(() => {
      setToasts(prev => prev.filter(t => t.id !== toast.id));
    }, 8000);
  }, [alertMsg]);

  if (toasts.length === 0) return null;

  return (
    // role=status + aria-live: new-alert popups are announced to screen readers (audit B13). Polite, so a burst of
    // alerts queues instead of interrupting whatever the person is reading.
    <div role="status" aria-live="polite" aria-atomic="false" style={{
        position: "fixed",
        bottom: "min(24px, 4vh)", right: "min(24px, 4vw)",
        display: "flex", flexDirection: "column", gap: 10,
        zIndex: 9999,
        width: "min(380px, calc(100vw - 48px))",
        maxWidth: "calc(100vw - 48px)",
      }}>
        {toasts.map(t => (
          <ToastItem key={t.id} toast={t} onClose={() => setToasts(p => p.filter(x => x.id !== t.id))} />
        ))}
      </div>
  );
}

function ToastItem({ toast, onClose }) {
  const isCrit  = toast.severity === "CRITICAL";
  // Theme tokens, not hard-coded dark-theme colours: the near-white text and translucent tint were unreadable on the light
  // theme, and the see-through background let the table show through the words.
  const color   = isCrit ? "var(--crit-fg)" : "var(--warn-fg)";
  const fill    = isCrit ? "var(--crit-fill)" : "var(--warn-fill)";
  const tint    = isCrit ? "var(--crit-bg)" : "var(--warn-bg)";
  const border  = isCrit ? "var(--crit-line)" : "var(--warn-line)";

  return (
    <div style={{
      background: `linear-gradient(${tint}, ${tint}), var(--surface-2)`,
      borderTop: `1px solid ${border}`, borderRight: `1px solid ${border}`, borderBottom: `1px solid ${border}`,
      borderLeft: `4px solid ${fill}`,
      borderRadius: 10, padding: "14px 16px",
      display: "flex", gap: 12, alignItems: "flex-start",
      boxShadow: "0 8px 24px rgba(15, 28, 53, 0.22)",
      animation: isCrit ? "slideIn .25s ease, toastCriticalGlow 2.4s ease infinite" : "slideIn .25s ease",
    }}>
      <div style={{
        flexShrink: 0, color,
        width: 36, height: 36, borderRadius: 10,
        background: tint, border: `1px solid ${border}`,
        display: "flex", alignItems: "center", justifyContent: "center",
      }}>
        {isCrit ? <AlertOctagonIcon size={19} /> : <AlertTriangleIcon size={19} />}
      </div>
      <div style={{ flex: 1 }}>
        <div style={{ fontWeight: 800, fontSize: 14, color, marginBottom: 3, letterSpacing: "0.01em" }}>
          {toast.severity} ALERT
        </div>
        <div style={{ fontSize: 13, color: "var(--ink-1)", marginBottom: 2 }}>
          {metricLabel(toast.metric)} — <strong>{formatMetricValue(toast.metric, toast.value)}</strong> (threshold: {formatMetricValue(toast.metric, toast.threshold)})
        </div>
        <div style={{ fontSize: 11, color: "var(--ink-3)" }}>
          {toast.account_name || `Account #${toast.account_id}`}
          {toast.region ? ` · ${toast.region}` : ""}
        </div>
      </div>
      <button className="toast-close-btn" onClick={onClose} style={{
        background: "none", border: "none", color: "var(--ink-3)",
        cursor: "pointer", padding: 4, lineHeight: 1, display: "flex",
      }}>
        <XIcon size={14} />
      </button>
    </div>
  );
}
