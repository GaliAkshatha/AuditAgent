import { useState } from "react";
import { startCodeReview, getCodeReview, createUnifiedReport, unifiedReportUrl } from "../api";

export default function CodeReviewFlow({ onAuditDeployed, onSkip }) {
  const [repo, setRepo] = useState("");
  const [job, setJob] = useState(null);
  const [reportId, setReportId] = useState(null);

  async function handleSubmit(e) {
    e.preventDefault();
    setJob({ status: "running" });
    try {
      const { id } = await startCodeReview(repo);
      poll(id);
    } catch (err) {
      setJob({ status: "failed", error: err.message });
    }
  }

  function poll(jobId) {
    const timer = setInterval(async () => {
      try {
        const data = await getCodeReview(jobId);
        setJob(data);
        if (data.status === "complete" || data.status === "failed") {
          clearInterval(timer);
          if (data.status === "complete" && !data.result?.error) {
            try {
              const { report_id } = await createUnifiedReport(repo, data.result);
              setReportId(report_id);
            } catch {
              // Report creation is a bonus, not core to the review flow — don't block on it.
            }
          }
        }
      } catch (err) {
        setJob({ status: "failed", error: err.message });
        clearInterval(timer);
      }
    }, 1200);
  }

  if (!job) {
    return (
      <section className="panel">
        <h2>Review your code first</h2>
        <p className="muted small">
          Paste your GitHub repo — short, scannable flags on what's good and what's risky. No live
          URL needed for this step.
        </p>
        <form onSubmit={handleSubmit} className="code-review-form">
          <input
            type="url" required placeholder="https://github.com/user/repo"
            value={repo} onChange={(e) => setRepo(e.target.value)}
          />
          <button type="submit" className="primary-btn code-review-btn">Review code</button>
        </form>
        <button type="button" className="link-btn" onClick={onSkip}>Skip — go straight to full audit</button>
      </section>
    );
  }

  if (job.status === "running") {
    return (
      <section className="panel">
        <p className="active-test-running"><span className="chat-live-dot" /> Reading your code…</p>
      </section>
    );
  }

  if (job.status === "failed" || job.result?.error) {
    return (
      <section className="panel">
        <p className="verify-fail">{job.error || job.result?.error}</p>
        <div className="review-prompt-actions">
          <button type="button" className="link-btn" onClick={() => setJob(null)}>Try again</button>
          <button type="button" className="link-btn" onClick={onSkip}>Skip to full audit</button>
        </div>
      </section>
    );
  }

  const { good = [], bad = [] } = job.result || {};

  return (
    <section className="panel">
      <div className="section-head">
        <h2>Code Review</h2>
        <span className="muted small">{job.result.files_reviewed} files scanned</span>
      </div>

      <div className="review-top-actions">
        <button type="button" className="primary-btn" onClick={() => onAuditDeployed(repo, reportId)}>
          Yes, audit deployed site
        </button>
        <button type="button" className="link-btn" onClick={onSkip}>No, that's enough</button>
        {reportId && (
          <a href={unifiedReportUrl(reportId)} target="_blank" rel="noreferrer" className="view-report-btn">
            📄 View report →
          </a>
        )}
      </div>

      <div className="review-grid">
        {good.map((item, i) => (
          <div className="review-card ok" key={`good-${i}`}>
            <span className="review-card-icon">✅</span>
            <div>
              <div className="review-card-title">{item.title}</div>
              <div className="review-card-detail">{item.detail}</div>
            </div>
          </div>
        ))}
        {bad.map((item, i) => (
          <div className={`review-card ${item.severity || "medium"}`} key={`bad-${i}`}>
            <span className="review-card-icon">⚠️</span>
            <div>
              <div className="review-card-title">{item.title}</div>
              <div className="review-card-detail">{item.detail}</div>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
