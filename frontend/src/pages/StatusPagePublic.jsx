// src/pages/StatusPagePublic.jsx
// Public status page -- NO AUTHENTICATION, reachable by anyone at
// /status. Fetched with plain fetch() (no credentials, no apiFetch --
// apiFetch redirects to /login on a 401, which would be wrong here:
// this page must render identically whether or not the visitor is
// logged in, or even has an account at all). See
// app/api/status_page.py's module docstring for the sanitization
// boundary this relies on server-side -- this component only ever
// renders what the backend already sanitized (component names and
// computed statuses), nothing here can leak more than that.
//
// Timezone selector added on top of the same TimezoneContext the
// authenticated app already uses for its topbar clock (App.jsx wraps
// the ENTIRE route tree, /status included, in TimezoneProvider, so
// this needed no extra plumbing to reach it) -- same IST/UTC choice,
// same persisted preference, so a visitor's choice here carries over
// if they also use the authenticated app in the same browser, and
// vice versa. Timestamps are rendered through formatDateTime/formatTime
// rather than raw toLocaleString() -- see app/api/status_page.py's
// comment on why started_at/resolved_at/generated_at now carry an
// explicit "Z": without it, every visitor's browser silently
// mis-parsed a UTC instant as their own local time regardless of any
// selector, which is the bug this page used to have.
import { useState, useEffect, useCallback } from "react";
import { useTimezone, TIMEZONE_OPTIONS } from "../contexts/TimezoneContext";
import "./StatusPagePublic.css";

const STATUS_COPY = {
  operational: { label: "All Systems Operational", tone: "ok" },
  degraded:    { label: "Degraded Performance",     tone: "warn" },
  outage:      { label: "Service Outage",           tone: "bad" },
};

function ComponentDot({ status }) {
  return <span className={`stp-dot stp-dot-${status}`} />;
}

export default function StatusPagePublic() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const { timezone, setTimezone, formatDateTime, formatTime } = useTimezone();

  const load = useCallback(() => {
    fetch("/api/status-page")
      .then(res => { if (!res.ok) throw new Error(`${res.status}`); return res.json(); })
      .then(setData)
      .catch(e => setError(e.message));
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  if (error) {
    return (
      <div className="stp-page">
        <div className="stp-container">
          <p className="stp-error">Status page unavailable right now — please check back shortly.</p>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="stp-page">
        <div className="stp-container">
          <p className="stp-loading">Loading status…</p>
        </div>
      </div>
    );
  }

  const overall = STATUS_COPY[data.overall_status] || STATUS_COPY.operational;

  return (
    <div className="stp-page">
      <div className="stp-container">
        <div className="stp-header-row">
          <select
            className="stp-tz-select"
            value={timezone}
            onChange={e => setTimezone(e.target.value)}
            title="Display timezone — applies to every time on this page"
            aria-label="Display timezone"
          >
            {Object.entries(TIMEZONE_OPTIONS).map(([key, opt]) => (
              <option key={key} value={key}>{opt.label}</option>
            ))}
          </select>
        </div>

        <div className={`stp-banner stp-banner-${overall.tone}`}>
          <ComponentDot status={data.overall_status} />
          <span>{overall.label}</span>
        </div>

        <div className="stp-components">
          {data.components.length === 0 ? (
            <p className="stp-empty">No services configured yet.</p>
          ) : (
            data.components.map((c, i) => (
              <div className="stp-component-row" key={i}>
                <span>{c.name}</span>
                <span className={`stp-component-status stp-component-status-${c.status}`}>
                  <ComponentDot status={c.status} /> {STATUS_COPY[c.status]?.label.replace("All Systems ", "") || c.status}
                </span>
              </div>
            ))
          )}
        </div>

        {data.recent_events.length > 0 && (
          <div className="stp-events">
            <h2>Recent events</h2>
            {data.recent_events.map((e, i) => (
              <div className="stp-event" key={i}>
                <span className={`stp-event-dot stp-dot-${e.status}`} />
                <div>
                  <div className="stp-event-title">{e.component} — {e.status === "outage" ? "Outage" : "Degraded performance"}</div>
                  <div className="stp-event-time">
                    {formatDateTime(e.started_at)}
                    {e.resolved_at ? ` – ${formatDateTime(e.resolved_at)}` : " – ongoing"}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

        <p className="stp-footer">Last updated {formatTime(data.generated_at)} {timezone} · refreshes automatically</p>
      </div>
    </div>
  );
}
