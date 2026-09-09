import { useEffect, useState } from "react";
import { startActiveTest, getActiveTestStatus, rollbackActiveTest, getVerificationStatus, diagnoseFailure, saveCredential, generateActiveTestReport, unifiedReportUrl, unifiedReportPdfUrl } from "../api";

const WAIT_FLAVOR_TEXT = [
  "Poking at your POST endpoints…",
  "Recording every change for safe rollback…",
  "Testing PATCH and DELETE too…",
  "Checking what happens with real test data…",
  "Tagging everything \"AuditAgent Test\" so it's easy to spot…",
  "Working through each resource group in order…",
];

function useWaitingState(isActive) {
  const [elapsed, setElapsed] = useState(0);
  const [flavorIndex, setFlavorIndex] = useState(0);

  useEffect(() => {
    if (!isActive) { setElapsed(0); setFlavorIndex(0); return; }
    const startedAt = Date.now();
    const timer = setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000);
    const flavorTimer = setInterval(() => setFlavorIndex((i) => (i + 1) % WAIT_FLAVOR_TEXT.length), 2400);
    return () => { clearInterval(timer); clearInterval(flavorTimer); };
  }, [isActive]);

  return { elapsed, flavorText: WAIT_FLAVOR_TEXT[flavorIndex] };
}

function extractDomain(url) {
  try { return new URL(url).host; } catch { return null; }
}

const CLASSIFICATION_LABELS = {
  consistent_failure: { label: "Likely a real bug", tone: "danger" },
  cold_start_pattern: { label: "Likely cold start", tone: "warn" },
  intermittent_failure: { label: "Likely infra/load issue", tone: "warn" },
  not_reproduced: { label: "Didn't reproduce", tone: "ok" },
};

function classifyStep(step) {
  if (step.skipped_reason) return "skipped";
  if (step.error) return "error";
  if (step.status == null) return "unknown";
  if (step.status < 300) return "ok";
  if (step.status === 401 || step.status === 403) return "auth";
  if (step.status >= 500) return "crash";
  return "rejected";
}

function classifyGroup(entry) {
  const priority = ["crash", "error", "rejected", "auth", "skipped", "unknown", "ok"];
  const classes = entry.steps.map(classifyStep);
  for (const p of priority) {
    if (classes.includes(p)) return p;
  }
  return "ok";
}

const CLASS_LABELS = {
  ok: { label: "OK", tone: "ok" },
  auth: { label: "Auth required", tone: "warn" },
  crash: { label: "Server error", tone: "danger" },
  rejected: { label: "Rejected", tone: "warn" },
  skipped: { label: "Skipped", tone: "skip" },
  error: { label: "Network error", tone: "warn" },
  unknown: { label: "—", tone: "skip" },
};

function ResultsSummaryBar({ report }) {
  const counts = {};
  report.forEach((entry) => {
    const cls = classifyGroup(entry);
    counts[cls] = (counts[cls] || 0) + 1;
  });

  const order = ["ok", "auth", "crash", "rejected", "skipped"];
  return (
    <div className="results-summary-bar">
      {order.filter((k) => counts[k]).map((k) => (
        <div className={`results-summary-chip ${CLASS_LABELS[k].tone}`} key={k}>
          <span className="results-summary-count">{counts[k]}</span>
          <span className="results-summary-label">{CLASS_LABELS[k].label}</span>
        </div>
      ))}
    </div>
  );
}

function ResourceCard({ entry, expanded, onToggle }) {
  const cls = classifyGroup(entry);
  const info = CLASS_LABELS[cls];

  return (
    <div className={`resource-card ${expanded ? "expanded" : ""}`}>
      <button type="button" className="resource-card-head" onClick={onToggle}>
        <span className="resource-card-path mono">{entry.resource}</span>
        <span className={`badge ${info.tone}`}>{info.label}</span>
      </button>

      {expanded && (
        <table className="history-table resource-card-detail">
          <tbody>
            {entry.steps.map((step, j) => {
              const failed = step.status ? step.status >= 400 : !!step.error;
              return (
                <tr key={j}>
                  <td className="mono">{step.method}</td>
                  <td className="url-cell">
                    {step.url}
                    {step.skipped_reason && <p className="muted small skip-reason">{step.skipped_reason}</p>}
                  </td>
                  <td>
                    {step.skipped_reason
                      ? <span className="badge skip">skipped</span>
                      : step.status
                        ? <span className={`badge ${step.status < 400 ? "ok" : "warn"}`}>{step.status}</span>
                        : step.error
                          ? <span className="badge warn">error</span>
                          : "—"}
                  </td>
                  <td>
                    {failed && step.method !== "GET" && !step.skipped_reason && (
                      <DiagnoseButton method={step.method} url={step.url} body={step.body || {}} />
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {expanded && entry.steps.some((s) => s.note) && (
        <p className="muted small" style={{ padding: "0 14px 12px" }}>{entry.steps.find((s) => s.note)?.note}</p>
      )}
    </div>
  );
}

function DiagnoseButton({ method, url, body }) {
  const [state, setState] = useState("idle"); // idle | running | done
  const [result, setResult] = useState(null);

  async function handleClick(patient) {
    setState("running");
    try {
      const r = await diagnoseFailure(method, url, body, patient);
      setResult(r);
      setState("done");
    } catch (err) {
      setResult({ classification: "error", explanation: err.message });
      setState("done");
    }
  }

  if (state === "idle") {
    return (
      <div className="diagnose-btn-row">
        <button type="button" className="link-btn diagnose-btn" onClick={() => handleClick(false)}>Dig deeper</button>
        <button type="button" className="link-btn diagnose-btn-patient" onClick={() => handleClick(true)} title="Wait up to 90s per attempt instead of 15s — for apps that might be cold-starting on free-tier hosting">
          🐢 wait longer
        </button>
      </div>
    );
  }
  if (state === "running") {
    return <p className="diagnose-running"><span className="chat-live-dot" /> Retrying a few times to check the pattern…</p>;
  }

  const info = CLASSIFICATION_LABELS[result.classification] || { label: result.classification, tone: "warn" };
  return (
    <div className="diagnose-result">
      <span className={`badge ${info.tone}`}>{info.label}</span>
      <p className="muted small">{result.explanation}</p>
      {result.attempts && (
        <div className="diagnose-attempts mono">
          {result.attempts.map((a, i) => (
            <span key={i} className={`diagnose-attempt ${a.status && a.status < 400 ? "ok" : "fail"}`}>
              {a.status ?? "ERR"}{a.elapsed_ms != null ? ` · ${a.elapsed_ms}ms` : ""}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export default function ActiveTestPanel({ url, outputDir, onStatusChange, unifiedReportId }) {
  const domain = extractDomain(url);
  const [verified, setVerified] = useState(false);
  const [job, setJob] = useState(null); // {status, result, error}
  const [rollbackPreview, setRollbackPreview] = useState(null);
  const [rollbackResult, setRollbackResult] = useState(null);
  const [rollingBack, setRollingBack] = useState(false);
  const [acceptingSuggestion, setAcceptingSuggestion] = useState(false);
  const [expandedCards, setExpandedCards] = useState(new Set());

  function toggleCard(i) {
    setExpandedCards((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i); else next.add(i);
      return next;
    });
  }
  const [suggestionDismissed, setSuggestionDismissed] = useState(false);
  const [reportState, setReportState] = useState("idle"); // "idle" | "generating" | "done" | "failed"
  const [reportUrls, setReportUrls] = useState(null);
  const { elapsed, flavorText } = useWaitingState(job?.status === "running");

  async function handleGenerateReport() {
    setReportState("generating");
    try {
      const data = await generateActiveTestReport(outputDir, url, job.result?.report || [], unifiedReportId);
      setReportUrls(data);
      setReportState("done");
      window.open(data.short_url, "_blank");
    } catch (err) {
      setReportState("failed");
    }
  }

  useEffect(() => {
    if (!domain) return;
    getVerificationStatus(domain).then((s) => setVerified(s.verified)).catch(() => setVerified(false));
  }, [domain]);

  // Reports lightweight status up to the parent — this is what lets the
  // Results page show a compact summary in its side panel without
  // needing a second instance of this component (and its full internal
  // state: rollback, diagnose, suggestion flow) mounted twice.
  useEffect(() => {
    onStatusChange?.(job);
  }, [job]);

  async function handleStart() {
    setJob({ status: "running" });
    setRollbackPreview(null);
    setRollbackResult(null);
    setSuggestionDismissed(false);
    try {
      const { id } = await startActiveTest(outputDir);
      poll(id);
    } catch (err) {
      setJob({ status: "failed", error: err.message });
    }
  }

  async function handleAcceptSuggestion(registerUrl) {
    setAcceptingSuggestion(true);
    try {
      await saveCredential("AUDIT_REGISTER_URL", registerUrl);
      await handleStart(); // re-run active testing immediately, now authenticated
    } catch (err) {
      setJob((j) => ({ ...j, error: `Couldn't save credential: ${err.message}` }));
    } finally {
      setAcceptingSuggestion(false);
    }
  }

  function poll(jobId) {
    const timer = setInterval(async () => {
      try {
        const data = await getActiveTestStatus(jobId);
        setJob(data);
        if (data.status === "complete" || data.status === "failed") clearInterval(timer);
      } catch (err) {
        setJob({ status: "failed", error: err.message });
        clearInterval(timer);
      }
    }, 1200);
  }

  async function handlePreviewRollback() {
    const result = await rollbackActiveTest(outputDir, false);
    setRollbackPreview(result);
  }

  async function handleConfirmRollback() {
    setRollingBack(true);
    try {
      const result = await rollbackActiveTest(outputDir, true);
      setRollbackResult(result);
    } finally {
      setRollingBack(false);
    }
  }

  if (!outputDir) return null;

  return (
    <section className="panel active-test-panel">
      <h2>Active Testing <span className="beta-tag">actually calls mutating endpoints</span></h2>

      {!verified ? (
        <p className="verify-fail">
          {domain} isn't verified — active testing calls real POST/PATCH/DELETE endpoints and requires
          proven domain ownership first (same requirement as the concurrency probe).
        </p>
      ) : !job ? (
        <div>
          <p className="active-test-warning">
            ⚠️ This will create, modify, and delete real test data using generic test payloads
            (clearly tagged "AuditAgent Test"). Every mutation is recorded and can be rolled back
            below. Only run this against a demo/test account, never production data.
          </p>
          <button type="button" className="active-test-btn" onClick={handleStart}>
            Run active test
          </button>
        </div>
      ) : job.status === "running" ? (
        <div className="active-test-waiting">
          <div className="waiting-visual">
            <span className="waiting-ring" />
            <span className="waiting-icon">⚡</span>
          </div>
          <p className="waiting-flavor">{flavorText}</p>
          <p className="waiting-elapsed mono">{elapsed}s elapsed</p>
        </div>
      ) : job.status === "failed" ? (
        <p className="verify-fail">Active test failed: {job.error}</p>
      ) : (
        <div>
          <p className="active-test-summary">
            {job.result?.resource_groups_tested ?? 0} resource group(s) tested.
            {job.note && <span className="muted"> {job.note}</span>}
          </p>

          <div className="inline-full-report">
            <h3>📄 Full Report</h3>
            <p className="muted small">
              What's good, what's bad, what needs changing, and scalability suggestions — added to
              the same report that started at code review.
            </p>
            <div className="inline-full-report-actions">
              <button type="button" className="generate-report-btn" onClick={handleGenerateReport} disabled={reportState === "generating"}>
                {reportState === "generating" ? "Generating…" : reportState === "done" ? "Regenerate report" : "Generate report"}
              </button>
              {unifiedReportId && (
                <>
                  <a href={unifiedReportUrl(unifiedReportId)} target="_blank" rel="noreferrer" className="view-report-btn">
                    View report →
                  </a>
                  <a href={unifiedReportPdfUrl(unifiedReportId)} download className="pdf-download-link">⬇ Download PDF</a>
                </>
              )}
            </div>
            {reportState === "done" && reportUrls && (
              <p className="muted small">
                Shareable link: <a href={reportUrls.short_url} target="_blank" rel="noreferrer" className="mono">{reportUrls.short_url}</a>
              </p>
            )}
            {reportState === "failed" && (
              <p className="verify-fail small">Couldn't generate the report — try again.</p>
            )}
          </div>

          {job.detected_register_url && !suggestionDismissed && (
            <div className="register-suggestion">
              <p>
                <strong>Found a register endpoint in your source code:</strong>{" "}
                <span className="mono">{job.detected_register_url}</span>
              </p>
              <p className="muted small">
                Many of the results above may just be "not authenticated," not real bugs — want to
                auto-register a test account with this and re-run, with zero typing?
              </p>
              <div className="register-suggestion-actions">
                <button
                  type="button" className="cred-save-btn"
                  onClick={() => handleAcceptSuggestion(job.detected_register_url)}
                  disabled={acceptingSuggestion}
                >
                  {acceptingSuggestion ? "Setting up…" : "Yes, use it & retest"}
                </button>
                <button type="button" className="link-btn" onClick={() => setSuggestionDismissed(true)}>
                  No thanks
                </button>
              </div>
            </div>
          )}

          {job.result?.report?.length > 0 && <ResultsSummaryBar report={job.result.report} />}

          <div className="resource-grid">
            {job.result?.report?.map((entry, i) => (
              <ResourceCard
                key={i} entry={entry}
                expanded={expandedCards.has(i)}
                onToggle={() => toggleCard(i)}
              />
            ))}
          </div>

          <div className="rollback-section">
            <h3>Roll back these changes</h3>
            {!rollbackPreview && !rollbackResult && (
              <button type="button" className="link-btn" onClick={handlePreviewRollback}>
                Preview rollback
              </button>
            )}

            {rollbackPreview && !rollbackResult && (
              <div className="verify-panel">
                <p className="verify-hint">
                  {rollbackPreview.compensable_actions} of {rollbackPreview.total_actions} action(s) can be undone
                  {rollbackPreview.self_canceling_pairs > 0 && ` (${rollbackPreview.self_canceling_pairs} self-canceling pair(s) — created and deleted within this run, nothing to undo)`}.
                </p>
                {rollbackPreview.results.filter(r => r.compensable && !r.self_canceling).map((r, i) => (
                  <div key={i} className="verify-token mono" style={{marginBottom: 6}}>
                    {r.compensating_action.method} {r.compensating_action.url}
                  </div>
                ))}
                <button type="button" className="verify-check-btn" onClick={handleConfirmRollback} disabled={rollingBack}>
                  {rollingBack ? "Rolling back…" : "Confirm rollback"}
                </button>
              </div>
            )}

            {rollbackResult && (
              <div className={`result-card ${rollbackResult.failed_rollbacks > 0 ? "error" : ""} reveal`}>
                <h3>{rollbackResult.failed_rollbacks === 0 ? "✅ Rollback complete" : "⚠️ Rollback finished with failures"}</h3>
                <p className="muted">
                  {rollbackResult.compensable_actions - rollbackResult.failed_rollbacks} succeeded, {rollbackResult.failed_rollbacks} failed,{" "}
                  {rollbackResult.non_compensable_actions} couldn't be auto-compensated.
                </p>
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
