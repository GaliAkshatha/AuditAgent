import { useEffect, useState } from "react";
import HealthScore, { computeHealthScore } from "./HealthScore";
import { pdfReportUrl } from "../api";

function useCountUp(target, duration = 700) {
  const [value, setValue] = useState(0);
  useEffect(() => {
    if (target == null) return;
    let start = null;
    let raf;
    function tick(ts) {
      if (start === null) start = ts;
      const progress = Math.min((ts - start) / duration, 1);
      setValue(Math.round(progress * target));
      if (progress < 1) raf = requestAnimationFrame(tick);
    }
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, duration]);
  return value;
}

function Chip({ icon, value, suffix = "", label, tone = "", delay = 0 }) {
  const animated = useCountUp(typeof value === "number" ? value : null);
  if (value == null) return null;
  return (
    <div className={`chip ${tone}`} style={{ animationDelay: `${delay}ms` }}>
      <span className="chip-icon">{icon}</span>
      <span className="chip-value">{typeof value === "number" ? animated : value}{suffix}</span>
      <span className="chip-label">{label}</span>
    </div>
  );
}

export default function ResultCard({ jobId, summary, reportUrl }) {
  if (!summary) return null;
  const score = computeHealthScore(summary);
  const clean = !summary.concurrency_concerning && !summary.never_observed_count;

  return (
    <div className="result-card reveal">
      <h3>Audit complete</h3>

      {summary.detected_api_base_url && (
        <p className="detected-notice">
          🔍 Detected your API is on a different host — automatically used{" "}
          <span className="mono">{summary.detected_api_base_url}</span> for endpoint checks, no
          manual entry needed.
        </p>
      )}

      {summary.api_base_url_source === "same_domain_fallback" && summary.never_observed_count > 0 && (
        <p className="detected-notice warn">
          ⚠️ Couldn't confidently detect a separate API host from this crawl — endpoint checks used
          the same domain as the site itself. If your backend actually lives elsewhere (a common
          split-deployment setup), set the real API base URL manually in Advanced options; a
          shallow or unauthenticated crawl may simply not observe enough real API traffic to detect
          it automatically.
        </p>
      )}

      <HealthScore score={score} />

      <div className="chips">
        <Chip icon="🕸️" value={summary.pages_crawled} label="pages crawled" delay={0} />
        {summary.top_bottleneck_ms ? (
          <Chip icon="🐢" value={Math.round(summary.top_bottleneck_ms)} suffix="ms" label="slowest page" tone="warn" delay={80} />
        ) : null}
        {summary.never_observed_count ? (
          <Chip icon="❓" value={summary.never_observed_count} label="unreached endpoints" tone="warn" delay={160} />
        ) : null}
        {summary.concurrency_concerning ? (
          <Chip icon="⚠️" value="Found" label="concurrency issue" tone="danger" delay={240} />
        ) : null}
        {clean && <Chip icon="✅" value="Clean" label="no red flags" tone="ok" delay={80} />}
      </div>

      {summary.report_html ? (
        <div className="report-links">
          <a className="view-report-btn" href={reportUrl(jobId)} target="_blank" rel="noreferrer">
            View full report
          </a>
          {summary.output_dir && (
            <a className="pdf-download-link" href={pdfReportUrl(summary.output_dir)} download>
              ⬇ Download PDF
            </a>
          )}
        </div>
      ) : (
        <p className="empty">Report generation was skipped or failed — check server logs.</p>
      )}
    </div>
  );
}
