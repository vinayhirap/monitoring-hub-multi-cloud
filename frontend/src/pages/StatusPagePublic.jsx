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
import { useState, useEffect, useCallback } from "react";
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
                    {new Date(e.started_at).toLocaleString()}
                    {e.resolved_at ? ` – ${new Date(e.resolved_at).toLocaleString()}` : " – ongoing"}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

        <p className="stp-footer">Last updated {new Date(data.generated_at).toLocaleTimeString()} · refreshes automatically</p>
      </div>
    </div>
  );
}
