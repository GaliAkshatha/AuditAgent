import { useEffect, useState } from "react";
import { getCredentialStatus, saveCredential, deleteCredential } from "../api";

const GROUPS = [
  {
    title: "Auto-register a fresh test account",
    description: "The tool creates its own throwaway account and cleans up after itself. Recommended if your app has a register endpoint.",
    keys: [
      { key: "AUDIT_REGISTER_URL", label: "Register endpoint URL", placeholder: "https://your-api.com/api/auth/register", secret: false },
      { key: "AUDIT_REGISTER_EMAIL", label: "Email template (optional)", placeholder: "you@yourdomain.com", secret: false, hint: "Uses + aliasing to stay unique each run. Use a real inbox you can check if your app requires email verification." },
    ],
  },
  {
    title: "Log into an existing demo account",
    description: "Use a demo account you've already seeded, instead of creating a new one each time.",
    keys: [
      { key: "AUDIT_LOGIN_URL", label: "Login endpoint URL", placeholder: "https://your-api.com/api/auth/login", secret: false },
      { key: "AUDIT_EMAIL", label: "Email", placeholder: "demo@yourapp.com", secret: false },
      { key: "AUDIT_USERNAME", label: "Username (if not email-based)", placeholder: "demo_user", secret: false },
      { key: "AUDIT_PASSWORD", label: "Password", placeholder: "••••••••", secret: true },
      { key: "AUDIT_LOGIN_FIELD_USER", label: "Field name override (advanced)", placeholder: "e.g. email, username", secret: false, hint: "Only needed if auto-detection guesses wrong." },
      { key: "AUDIT_LOGIN_FIELD_PASS", label: "Password field name override (advanced)", placeholder: "e.g. password, pass", secret: false },
    ],
  },
  {
    title: "Already have a token or session?",
    description: "Skip login entirely and use a token/cookie you already obtained.",
    keys: [
      { key: "AUDIT_BEARER_TOKEN", label: "Bearer token", placeholder: "eyJhbGc...", secret: true },
      { key: "AUDIT_COOKIES", label: "Cookie string", placeholder: "session=abc123; other=xyz", secret: true },
    ],
  },
];

function CredentialField({ field, configured, onSaved, onDeleted }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  async function handleSave() {
    if (!value.trim()) return;
    setSaving(true);
    setError(null);
    try {
      await saveCredential(field.key, value.trim());
      setValue("");
      setEditing(false);
      onSaved(field.key);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    await deleteCredential(field.key);
    onDeleted(field.key);
  }

  return (
    <div className="cred-field">
      <div className="cred-field-head">
        <label>{field.label}</label>
        {configured && !editing && <span className="badge ok">configured</span>}
      </div>
      {field.hint && <p className="muted small cred-hint">{field.hint}</p>}

      {configured && !editing ? (
        <div className="cred-configured-row">
          <span className="cred-masked mono">{field.secret ? "••••••••••••" : "✓ set"}</span>
          <button type="button" className="link-btn" onClick={() => setEditing(true)}>Change</button>
          <button type="button" className="link-btn cred-delete" onClick={handleDelete}>Remove</button>
        </div>
      ) : (
        <div className="cred-input-row">
          <input
            type={field.secret ? "password" : "text"}
            placeholder={field.placeholder}
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <button type="button" className="cred-save-btn" onClick={handleSave} disabled={saving || !value.trim()}>
            {saving ? "…" : "Save"}
          </button>
          {configured && (
            <button type="button" className="link-btn" onClick={() => { setEditing(false); setValue(""); }}>Cancel</button>
          )}
        </div>
      )}
      {error && <p className="verify-fail small">{error}</p>}
    </div>
  );
}

export default function CredentialsPanel({ onBack }) {
  const [status, setStatus] = useState(null);

  useEffect(() => {
    getCredentialStatus().then(setStatus).catch(() => setStatus({}));
  }, []);

  function markConfigured(key) {
    setStatus((s) => ({ ...s, [key]: true }));
  }

  function markRemoved(key) {
    setStatus((s) => ({ ...s, [key]: false }));
  }

  if (!status) return <div className="panel"><p className="muted">Loading…</p></div>;

  return (
    <div className="main-col">
      <section className="panel">
        <div className="section-head">
          <h2>Target-Site Credentials</h2>
          <button type="button" className="link-btn" onClick={onBack}>← Back</button>
        </div>
        <p className="active-test-warning">
          ⚠️ These credentials are used by <strong>Active Testing</strong> to authenticate against
          the app you're testing — not your AuditAgent login. Stored encrypted, tied only to your
          account. Only use credentials for a demo/test account, never production data.
        </p>
      </section>

      {GROUPS.map((group) => (
        <section className="panel" key={group.title}>
          <h3 className="cred-group-title">{group.title}</h3>
          <p className="muted small cred-group-desc">{group.description}</p>
          {group.keys.map((field) => (
            <CredentialField
              key={field.key}
              field={field}
              configured={!!status[field.key]}
              onSaved={markConfigured}
              onDeleted={markRemoved}
            />
          ))}
        </section>
      ))}
    </div>
  );
}
