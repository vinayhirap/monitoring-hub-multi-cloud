// components/ErrorBoundary.jsx -- keeps one broken page from blanking the whole app.
// Wrapped around the routed content in Layout (keyed by pathname so navigating away resets it); the
// sidebar, top bar and navigation keep working. Shows a short message, never a stack trace.
import { Component } from "react";
import { Link } from "react-router-dom";

export default class ErrorBoundary extends Component {
  state = { error: null };
  static getDerivedStateFromError(error) { return { error }; }
  componentDidCatch(error, info) { console.error("Page render failed:", error, info?.componentStack); }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="ui-empty eb" role="alert">
        <div className="ui-empty-title">This page ran into a problem</div>
        <div className="ui-sub">The rest of the app is unaffected. Try again, or go back to the Overview. If it keeps happening, tell an administrator which page and what you were doing.</div>
        <div className="eb-actions">
          <button type="button" className="ui-btn" onClick={() => this.setState({ error: null })}>Try again</button>
          {this.props.root ? <a className="ui-btn" href="/overview">Go to Overview</a> : <Link className="ui-btn" to="/overview">Go to Overview</Link>}
          <button type="button" className="ui-btn" onClick={() => window.location.reload()}>Reload</button>
        </div>
      </div>
    );
  }
}
