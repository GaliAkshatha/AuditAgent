import { useState } from "react";
import DomainVerify from "./DomainVerify";

export default function AuditForm({ onSubmit, disabled, defaultRepo }) {
  const [url, setUrl] = useState("");
  const [repo, setRepo] = useState(defaultRepo || "");
  const [apiBaseUrl, setApiBaseUrl] = useState("");
  const [provider, setProvider] = useState("gemini");
  const [maxPages, setMaxPages] = useState(25);
  const [patient, setPatient] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [domainVerified, setDomainVerified] = useState(false);

  function handleSubmit(e) {
    e.preventDefault();
    onSubmit({
      url: url.trim(),
      repo: repo.trim() || null,
      api_base_url: apiBaseUrl.trim() || null,
      provider,
      max_pages: Number(maxPages) || 25,
      patient,
    });
  }

  return (
    <section className="panel form-panel">
      <form onSubmit={handleSubmit}>
        <div className="field">
          <label htmlFor="url">Deployed app URL</label>
          <input
            id="url" type="url" required autoComplete="off"
            placeholder="https://your-app.com"
            value={url} onChange={(e) => setUrl(e.target.value)}
          />
          <DomainVerify url={url} onVerifiedChange={setDomainVerified} />
        </div>

        <div className="field">
          <label htmlFor="repo">
            GitHub repo <span className="optional">optional — enables endpoint-gap findings</span>
          </label>
          <input
            id="repo" type="url" autoComplete="off"
            placeholder="https://github.com/user/repo"
            value={repo} onChange={(e) => setRepo(e.target.value)}
          />
        </div>

        <button type="button" className="link-btn" onClick={() => setShowAdvanced((v) => !v)}>
          {showAdvanced ? "Hide advanced options" : "Advanced options"}
        </button>

        <div className={`advanced ${showAdvanced ? "open" : ""}`}>
          <div className="advanced-inner">
            <div className="field">
              <label htmlFor="api_base_url">
                API base URL <span className="optional">usually auto-detected — only set this if detection guesses wrong</span>
              </label>
              <input
                id="api_base_url" type="url" autoComplete="off"
                placeholder="https://api.your-app.com"
                value={apiBaseUrl} onChange={(e) => setApiBaseUrl(e.target.value)}
              />
            </div>
            <div className="field-row">
              <div className="field">
                <label htmlFor="provider">Model provider</label>
                <select id="provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
                  <option value="gemini">Gemini (free tier)</option>
                  <option value="anthropic">Claude</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="max_pages">Max pages</label>
                <input
                  id="max_pages" type="number" min="1" max="200"
                  value={maxPages} onChange={(e) => setMaxPages(e.target.value)}
                />
              </div>
            </div>

            <label className="patient-toggle">
              <input type="checkbox" checked={patient} onChange={(e) => setPatient(e.target.checked)} />
              <span>
                <strong>Patient mode</strong> — wait up to 90s per page instead of 15s
                <span className="patient-hint">
                  For apps on free-tier hosting (Render, Vercel, etc.) that cold-start and can take a
                  genuinely long time to wake up. A slow page gets reported as slow with its real
                  timing, instead of falsely reported as broken by a premature timeout. The whole
                  audit will take longer with this on — that's the tradeoff for an accurate number.
                </span>
              </span>
            </label>
          </div>
        </div>

        <button type="submit" className="primary-btn" disabled={disabled || !domainVerified || !url}>
          {disabled ? "Running…" : !url ? "Run audit" : !domainVerified ? "Verify domain to run audit" : "Run audit"}
        </button>
      </form>
    </section>
  );
}
