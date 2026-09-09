import { useState } from "react";
import { loginUser, registerUser } from "../api";

export default function AuthForm({ initialMode = "login", onAuthenticated, onBack }) {
  const [mode, setMode] = useState(initialMode);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const fn = mode === "login" ? loginUser : registerUser;
      const user = await fn(email, password);
      onAuthenticated(user);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="auth-wrap">
      <div className="panel auth-panel">
        {onBack && (
          <button type="button" className="link-btn auth-back" onClick={onBack}>← Back to home</button>
        )}
        <div className="wordmark auth-wordmark">AuditAgent</div>
        <p className="tagline auth-tagline">Crawl it. Read the code. Find what breaks first.</p>

        <div className="auth-tabs">
          <button
            type="button"
            className={`auth-tab ${mode === "login" ? "active" : ""}`}
            onClick={() => { setMode("login"); setError(null); }}
          >
            Log in
          </button>
          <button
            type="button"
            className={`auth-tab ${mode === "register" ? "active" : ""}`}
            onClick={() => { setMode("register"); setError(null); }}
          >
            Sign up
          </button>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="field">
            <label htmlFor="auth-email">Email</label>
            <input
              id="auth-email" type="email" required autoComplete="email"
              value={email} onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="auth-password">
              Password {mode === "register" && <span className="optional">at least 8 characters</span>}
            </label>
            <input
              id="auth-password" type="password" required minLength={8}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              value={password} onChange={(e) => setPassword(e.target.value)}
            />
          </div>

          {error && <p className="verify-fail auth-error">{error}</p>}

          <button type="submit" className="primary-btn" disabled={loading}>
            {loading ? "…" : mode === "login" ? "Log in" : "Create account"}
          </button>
        </form>
      </div>
    </div>
  );
}
