import { useEffect, useState } from "react";
import { startVerification, checkVerification, getVerificationStatus } from "../api";

function extractDomain(url) {
  try {
    return new URL(url).host;
  } catch {
    return null;
  }
}

export default function DomainVerify({ url, onVerifiedChange }) {
  const domain = extractDomain(url);
  const [verified, setVerified] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [instructions, setInstructions] = useState(null);
  const [checking, setChecking] = useState(false);
  const [checkResult, setCheckResult] = useState(null);

  useEffect(() => {
    setExpanded(false);
    setInstructions(null);
    setCheckResult(null);
    if (!domain) { setVerified(false); onVerifiedChange?.(false); return; }
    getVerificationStatus(domain)
      .then((s) => { setVerified(s.verified); onVerifiedChange?.(s.verified); })
      .catch(() => { setVerified(false); onVerifiedChange?.(false); });
  }, [domain]);

  if (!domain) return null;
  if (verified) {
    return (
      <div className="verify-status verified">
        <span className="verify-dot ok" /> <strong>{domain}</strong> is verified — ready to audit.
      </div>
    );
  }

  async function handleStart() {
    const data = await startVerification(domain);
    setInstructions(data);
    setExpanded(true);
  }

  async function handleCheck() {
    setChecking(true);
    setCheckResult(null);
    try {
      const result = await checkVerification(domain);
      setCheckResult(result);
      if (result.verified) { setVerified(true); onVerifiedChange?.(true); }
    } finally {
      setChecking(false);
    }
  }

  return (
    <div className="verify-status unverified">
      <div className="verify-row">
        <span className="verify-dot warn" />
        <strong>{domain}</strong> isn't verified yet — auditing a deployed site requires proven
        domain ownership first. Verify it to run the audit.
        <button type="button" className="link-btn verify-toggle" onClick={() => (instructions ? setExpanded((v) => !v) : handleStart())}>
          {expanded ? "Hide" : "Verify ownership"}
        </button>
      </div>

      {expanded && instructions && (
        <div className="verify-panel">
          <p className="verify-hint">Prove you control {domain} with EITHER method:</p>

          <div className="verify-method">
            <span className="verify-method-label">1. Well-known file</span>
            <p>Create a file at <code className="mono">{instructions.well_known.url}</code> containing exactly:</p>
            <code className="verify-token mono">{instructions.token}</code>
          </div>

          <div className="verify-method">
            <span className="verify-method-label">2. DNS TXT record</span>
            <p>Add a TXT record for <code className="mono">{instructions.dns_txt.record_name}</code> with value:</p>
            <code className="verify-token mono">{instructions.token}</code>
          </div>

          <button type="button" className="verify-check-btn" onClick={handleCheck} disabled={checking}>
            {checking ? "Checking…" : "I've done this — check now"}
          </button>

          {checkResult && !checkResult.verified && (
            <p className="verify-fail">Not verified yet — neither method found the token. DNS changes can take a few minutes to propagate.</p>
          )}
        </div>
      )}
    </div>
  );
}
