// src/pages/Login.jsx
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { CheckIcon } from "../components/icons";
import { AwsBrandLogo, AzureBrandLogo, GoogleCloudBrandLogo } from "../components/cloud-icons";
import "./Login.css";

const EyeIcon = ({ open }) => (
  open ? (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/>
      <line x1="1" y1="1" x2="23" y2="23"/>
    </svg>
  ) : (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
      <circle cx="12" cy="12" r="3"/>
    </svg>
  )
);

const PwField = ({ id, label, value, onChange, show, onToggle, placeholder, autoComplete, disabled, autoFocus }) => (
  <div className="login-field">
    <label htmlFor={id}>{label}</label>
    <div className="login-input-wrap">
      <span className="login-input-icon">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
          <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
        </svg>
      </span>
      <input
        id={id}
        type={show ? "text" : "password"}
        className="login-input"
        placeholder={placeholder}
        value={value}
        onChange={onChange}
        autoComplete={autoComplete}
        disabled={disabled}
        autoFocus={autoFocus}
      />
      <button type="button" className="login-toggle-pw" onClick={onToggle} tabIndex={-1}>
        <EyeIcon open={show} />
      </button>
    </div>
  </div>
);

export default function Login() {
  const { login } = useAuth();
  const navigate   = useNavigate();

  // "login" | "forgot" | "reset" | "change"
  const [mode, setMode] = useState("login");

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error,    setError]    = useState("");
  const [info,     setInfo]     = useState("");
  const [loading,  setLoading]  = useState(false);
  const [showPw,   setShowPw]   = useState(false);

  // forgot/reset state
  const [resetToken,   setResetToken]   = useState("");
  const [tokenInput,   setTokenInput]   = useState("");
  const [newPw,        setNewPw]        = useState("");
  const [newPw2,       setNewPw2]       = useState("");
  const [showNewPw,    setShowNewPw]    = useState(false);

  // change-password state
  const [currentPw,    setCurrentPw]    = useState("");
  const [showCurrentPw, setShowCurrentPw] = useState(false);

  function resetTransientState() {
    setError(""); setInfo(""); setPassword("");
    setResetToken(""); setTokenInput(""); setNewPw(""); setNewPw2("");
    setCurrentPw("");
  }

  function goTo(nextMode) {
    resetTransientState();
    setMode(nextMode);
  }

  async function handleSubmit(e) {
    e.preventDefault();
    if (!username.trim() || !password.trim()) {
      setError("Username and password are required.");
      return;
    }
    setLoading(true);
    setError("");
    const ok = await login(username.trim(), password);
    setLoading(false);
    if (ok) {
      navigate("/overview", { replace: true });
    } else {
      setError("Invalid username or password.");
    }
  }

  async function handleForgotSubmit(e) {
    e.preventDefault();
    if (!username.trim()) {
      setError("Enter your username first.");
      return;
    }
    setLoading(true);
    setError(""); setInfo("");
    try {
      const res  = await fetch("/api/auth/forgot-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Request failed");
      if (data.token) {
        setResetToken(data.token);
        setMode("reset");
      } else {
        setInfo(data.message || "If that account exists, a reset token has been generated.");
      }
    } catch (err) {
      setError(err.message || "Could not request a reset. Try again.");
    } finally {
      setLoading(false);
    }
  }

  async function handleResetSubmit(e) {
    e.preventDefault();
    if (!tokenInput.trim() || !newPw || !newPw2) {
      setError("All fields are required.");
      return;
    }
    if (newPw !== newPw2) {
      setError("New passwords don't match.");
      return;
    }
    if (newPw.length < 8) {
      setError("New password must be at least 8 characters.");
      return;
    }
    setLoading(true);
    setError(""); setInfo("");
    try {
      const res  = await fetch("/api/auth/reset-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: tokenInput.trim(), new_password: newPw }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Reset failed");
      goTo("login");
      setInfo("Password reset. Sign in with your new password.");
    } catch (err) {
      setError(err.message || "Could not reset password.");
    } finally {
      setLoading(false);
    }
  }

  async function handleChangeSubmit(e) {
    e.preventDefault();
    if (!username.trim() || !currentPw || !newPw || !newPw2) {
      setError("All fields are required.");
      return;
    }
    if (newPw !== newPw2) {
      setError("New passwords don't match.");
      return;
    }
    if (newPw.length < 8) {
      setError("New password must be at least 8 characters.");
      return;
    }
    setLoading(true);
    setError(""); setInfo("");
    try {
      const res  = await fetch("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: username.trim(),
          current_password: currentPw,
          new_password: newPw,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Change failed");
      goTo("login");
      setInfo("Password changed. Sign in with your new password.");
    } catch (err) {
      setError(err.message || "Could not change password.");
    } finally {
      setLoading(false);
    }
  }

  const titles = {
    login:  ["Welcome back",       "Sign in to continue to CloudOps"],
    forgot: ["Forgot password",    "Enter your username and we'll generate a reset token"],
    reset:  ["Reset password",     "Enter the reset token and choose a new password"],
    change: ["Change password",    "Enter your current password and choose a new one"],
  };
  const [title, desc] = titles[mode];

  return (
    <div className="login-root">
      <div className="login-shell">
        <div className="login-visual" aria-hidden="true">
          <div className="login-grid">
            {Array.from({ length: 80 }).map((_, i) => (
              <div key={i} className="login-grid-cell" />
            ))}
          </div>
          <div className="login-blob login-blob-1" />
          <div className="login-blob login-blob-2" />

          <div className="login-visual-content">
            <div className="login-orbit">
              <div className="login-orbit-hub">
                <svg width="26" height="26" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
                  <rect width="512" height="512" rx="112" fill="#0b1220" />
                  <g transform="translate(-891.82,2.79) scale(0.8064)">
                    <path fill="#2bb3ac" d="M1331.98,222.58c41.39-41.42,103.91-48.88,152.93-22.35l53.65-53.65c-79.15-54.58-188.41-46.67-258.82,23.77-70.44,70.4-78.35,179.67-23.77,258.82l53.65-53.65c-26.53-49.02-19.07-111.55,22.35-152.93Z" />
                    <path fill="#2bb3ac" d="M1567.06,457.66c70.44-70.44,78.35-179.7,23.73-258.85l-53.65,53.65c26.53,49.02,19.07,111.55-22.32,152.97-41.42,41.39-103.95,48.85-152.93,22.32l-53.65,53.65c79.15,54.62,188.38,46.71,258.82-23.73Z" />
                  </g>
                </svg>
              </div>
              <div className="login-orbit-badge login-orbit-badge-aws">
                <AwsBrandLogo size={12} />AWS
              </div>
              <div className="login-orbit-badge login-orbit-badge-azure">
                <AzureBrandLogo size={15} />Azure
              </div>
              <div className="login-orbit-badge login-orbit-badge-gcp">
                <GoogleCloudBrandLogo size={14} />GCP
              </div>
            </div>

            <div className="login-eyebrow">Multi-Cloud Observability</div>
            <h2 className="login-visual-title">Total visibility.<br />Across every cloud.</h2>
            <p className="login-visual-sub">
              Real-time metrics, unified alerting, and role-based access —
              across every account, region, and provider you run.
            </p>
            <ul className="login-visual-features">
              <li><span className="login-feature-check"><CheckIcon size={11} /></span>Live health &amp; metrics across all accounts</li>
              <li><span className="login-feature-check"><CheckIcon size={11} /></span>Unified, severity-based alerting</li>
              <li><span className="login-feature-check"><CheckIcon size={11} /></span>Fine-grained RBAC for teams and groups</li>
            </ul>
          </div>
        </div>

        <div className="login-form-side">
          <div className="login-card">
        <div className="login-brand">
          <div className="login-brand-sub">AURIONPRO</div>
          <div className="login-logo-row">
            <svg width="34" height="34" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg" aria-label="CloudOps">
              <rect width="512" height="512" rx="112" fill="#0b1220" />
              <g transform="translate(-891.82,2.79) scale(0.8064)">
                <path fill="#2bb3ac" d="M1331.98,222.58c41.39-41.42,103.91-48.88,152.93-22.35l53.65-53.65c-79.15-54.58-188.41-46.67-258.82,23.77-70.44,70.4-78.35,179.67-23.77,258.82l53.65-53.65c-26.53-49.02-19.07-111.55,22.35-152.93Z" />
                <path fill="#2bb3ac" d="M1567.06,457.66c70.44-70.44,78.35-179.7,23.73-258.85l-53.65,53.65c26.53,49.02,19.07,111.55-22.32,152.97-41.42,41.39-103.95,48.85-152.93,22.32l-53.65,53.65c79.15,54.62,188.38,46.71,258.82-23.73Z" />
              </g>
            </svg>
            <span className="login-wordmark">Cloud<span className="login-wordmark-accent">Ops</span></span>
          </div>
        </div>

        <h1 className="login-title">{title}</h1>
        <p className="login-desc">{desc}</p>

        {/* ── LOGIN ─────────────────────────────────────────── */}
        {mode === "login" && (
          <form className="login-form" onSubmit={handleSubmit} noValidate>
            <div className="login-field">
              <label htmlFor="username">Username</label>
              <div className="login-input-wrap">
                <span className="login-input-icon">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
                    <circle cx="12" cy="7" r="4"/>
                  </svg>
                </span>
                <input
                  id="username"
                  type="text"
                  className="login-input"
                  placeholder="Enter username"
                  value={username}
                  onChange={e => { setUsername(e.target.value); setError(""); }}
                  autoComplete="username"
                  autoFocus
                  disabled={loading}
                />
              </div>
            </div>

            <PwField
              id="password" label="Password" placeholder="Enter password"
              value={password} onChange={e => { setPassword(e.target.value); setError(""); }}
              show={showPw} onToggle={() => setShowPw(v => !v)}
              autoComplete="current-password" disabled={loading}
            />

            <div className="login-links-row">
              <button type="button" className="login-link" onClick={() => goTo("forgot")}>
                Forgot password?
              </button>
              <button type="button" className="login-link" onClick={() => goTo("change")}>
                Change password
              </button>
            </div>

            {info && <div className="login-info" role="status">{info}</div>}
            {error && (
              <div className="login-error" role="alert">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <circle cx="12" cy="12" r="10"/>
                  <line x1="12" y1="8" x2="12" y2="12"/>
                  <line x1="12" y1="16" x2="12.01" y2="16"/>
                </svg>
                {error}
              </div>
            )}

            <button type="submit" className={`login-btn ${loading ? "login-btn-loading" : ""}`} disabled={loading}>
              {loading ? (<><span className="login-spinner" />Authenticating…</>) : "Sign In →"}
            </button>
          </form>
        )}

        {/* ── FORGOT PASSWORD ──────────────────────────────── */}
        {mode === "forgot" && (
          <form className="login-form" onSubmit={handleForgotSubmit} noValidate>
            <div className="login-field">
              <label htmlFor="fp-username">Username</label>
              <div className="login-input-wrap">
                <span className="login-input-icon">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
                    <circle cx="12" cy="7" r="4"/>
                  </svg>
                </span>
                <input
                  id="fp-username"
                  type="text"
                  className="login-input"
                  placeholder="Enter username"
                  value={username}
                  onChange={e => { setUsername(e.target.value); setError(""); }}
                  autoComplete="username"
                  autoFocus
                  disabled={loading}
                />
              </div>
            </div>

            {info && <div className="login-info" role="status">{info}</div>}
            {error && <div className="login-error" role="alert">{error}</div>}

            <button type="submit" className={`login-btn ${loading ? "login-btn-loading" : ""}`} disabled={loading}>
              {loading ? (<><span className="login-spinner" />Requesting…</>) : "Send Reset Token →"}
            </button>
            <button type="button" className="login-link login-link-back" onClick={() => goTo("login")}>
              ← Back to sign in
            </button>
          </form>
        )}

        {/* ── RESET PASSWORD (has token) ──────────────────────── */}
        {mode === "reset" && (
          <form className="login-form" onSubmit={handleResetSubmit} noValidate>
            {resetToken && (
              <div className="login-info login-token-box" role="status">
                No email service is configured yet — here's your one-time token
                (valid {30} min):
                <code className="login-token-code">{resetToken}</code>
              </div>
            )}
            <div className="login-field">
              <label htmlFor="reset-token">Reset token</label>
              <input
                id="reset-token"
                type="text"
                className="login-input login-input-plain"
                placeholder="Paste reset token"
                value={tokenInput}
                onChange={e => { setTokenInput(e.target.value); setError(""); }}
                disabled={loading}
              />
            </div>
            <PwField
              id="new-password" label="New password" placeholder="At least 8 characters"
              value={newPw} onChange={e => { setNewPw(e.target.value); setError(""); }}
              show={showNewPw} onToggle={() => setShowNewPw(v => !v)}
              autoComplete="new-password" disabled={loading}
            />
            <PwField
              id="new-password-confirm" label="Confirm new password" placeholder="Re-enter new password"
              value={newPw2} onChange={e => { setNewPw2(e.target.value); setError(""); }}
              show={showNewPw} onToggle={() => setShowNewPw(v => !v)}
              autoComplete="new-password" disabled={loading}
            />

            {error && <div className="login-error" role="alert">{error}</div>}

            <button type="submit" className={`login-btn ${loading ? "login-btn-loading" : ""}`} disabled={loading}>
              {loading ? (<><span className="login-spinner" />Resetting…</>) : "Reset Password →"}
            </button>
            <button type="button" className="login-link login-link-back" onClick={() => goTo("login")}>
              ← Back to sign in
            </button>
          </form>
        )}

        {/* ── CHANGE PASSWORD (knows current password) ────────── */}
        {mode === "change" && (
          <form className="login-form" onSubmit={handleChangeSubmit} noValidate>
            <div className="login-field">
              <label htmlFor="cp-username">Username</label>
              <div className="login-input-wrap">
                <span className="login-input-icon">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
                    <circle cx="12" cy="7" r="4"/>
                  </svg>
                </span>
                <input
                  id="cp-username"
                  type="text"
                  className="login-input"
                  placeholder="Enter username"
                  value={username}
                  onChange={e => { setUsername(e.target.value); setError(""); }}
                  autoComplete="username"
                  autoFocus
                  disabled={loading}
                />
              </div>
            </div>
            <PwField
              id="current-password" label="Current password" placeholder="Enter current password"
              value={currentPw} onChange={e => { setCurrentPw(e.target.value); setError(""); }}
              show={showCurrentPw} onToggle={() => setShowCurrentPw(v => !v)}
              autoComplete="current-password" disabled={loading}
            />
            <PwField
              id="cp-new-password" label="New password" placeholder="At least 8 characters"
              value={newPw} onChange={e => { setNewPw(e.target.value); setError(""); }}
              show={showNewPw} onToggle={() => setShowNewPw(v => !v)}
              autoComplete="new-password" disabled={loading}
            />
            <PwField
              id="cp-new-password-confirm" label="Confirm new password" placeholder="Re-enter new password"
              value={newPw2} onChange={e => { setNewPw2(e.target.value); setError(""); }}
              show={showNewPw} onToggle={() => setShowNewPw(v => !v)}
              autoComplete="new-password" disabled={loading}
            />

            {error && <div className="login-error" role="alert">{error}</div>}

            <button type="submit" className={`login-btn ${loading ? "login-btn-loading" : ""}`} disabled={loading}>
              {loading ? (<><span className="login-spinner" />Updating…</>) : "Change Password →"}
            </button>
            <button type="button" className="login-link login-link-back" onClick={() => goTo("login")}>
              ← Back to sign in
            </button>
          </form>
        )}
          </div>
        </div>
      </div>
    </div>
  );
}
