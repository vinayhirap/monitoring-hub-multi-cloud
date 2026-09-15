// src/pages/Search.jsx
// Natural-language alert search. See app/nlquery/parser.py's module
// docstring for why this is deterministic keyword matching (severity/
// status/resource-type/time-window/free-text), not an embedding
// model -- explainable, zero heavy dependency, instant.
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { searchAlerts } from "../api/api";
import { SearchIcon, AlertOctagonIcon } from "../components/icons";
import "./Search.css";

function SevBadge({ sev }) {
  return <span className={`srch-sev srch-sev-${sev.toLowerCase()}`}>● {sev}</span>;
}

export default function Search() {
  const [q, setQ] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const handleSearch = async (e) => {
    e.preventDefault();
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const data = await searchAlerts(q.trim());
      setResult(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="srch-page">
      <div className="c-header">
        <div>
          <h1>Search <span className="hl">Alerts</span></h1>
          <p className="sub">Plain English -- try "critical rds alerts last hour" or "warning alerts on payment-api today"</p>
        </div>
      </div>

      <form className="srch-form" onSubmit={handleSearch}>
        <div className="srch-input-wrap">
          <SearchIcon size={15} />
          <input
            className="srch-input"
            value={q}
            onChange={e => setQ(e.target.value)}
            placeholder="e.g. critical alerts on ec2 in the last 2 hours"
            autoFocus
          />
        </div>
        <button type="submit" className="srch-btn" disabled={loading || !q.trim()}>
          {loading ? "Searching…" : "Search"}
        </button>
      </form>

      {error && <div className="srch-error"><AlertOctagonIcon size={13} /> {error}</div>}

      {result && (
        <div className="srch-card">
          <div className="srch-bar">
            <span className="bar-icon">▐</span>
            <span className="bar-title">INTERPRETED AS</span>
            <span className="srch-interpretation">{result.interpreted_as}</span>
          </div>

          {result.results.length === 0 ? (
            <div className="srch-empty">No matching alerts found.</div>
          ) : (
            <table className="srch-table">
              <thead>
                <tr>
                  <th>Severity</th>
                  <th>Resource</th>
                  <th>Metric</th>
                  <th>Account</th>
                  <th>Triggered</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {result.results.map(a => (
                  <tr key={a.id} className="srch-row" onClick={() => navigate("/alerts")}>
                    <td><SevBadge sev={a.severity} /></td>
                    <td>{a.resource_name || a.aws_resource_id}</td>
                    <td className="mono">{a.metric_name}</td>
                    <td>{a.account_name}</td>
                    <td className="mono">{new Date(a.created_at).toLocaleString()}</td>
                    <td>{a.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
