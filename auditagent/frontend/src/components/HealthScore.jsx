// A single at-a-glance score, computed from the real findings — not a fake
// number. Penalties are heuristic and intentionally simple/transparent:
// this is meant to be a fun, legible summary, not a scientific metric.

export function computeHealthScore(summary) {
  if (!summary) return null;
  let score = 100;

  if (summary.concurrency_concerning) score -= 20;

  if (summary.top_bottleneck_ms) {
    const overage = Math.max(0, summary.top_bottleneck_ms - 1500);
    score -= Math.min(30, Math.round(overage / 300));
  }

  if (summary.never_observed_count) {
    score -= Math.min(25, summary.never_observed_count);
  }

  return Math.max(5, Math.min(100, score));
}

function band(score) {
  if (score >= 85) return { label: "Excellent", cls: "ok" };
  if (score >= 65) return { label: "Solid", cls: "ok" };
  if (score >= 40) return { label: "Needs work", cls: "warn" };
  return { label: "At risk", cls: "danger" };
}

export default function HealthScore({ score }) {
  if (score == null) return null;
  const b = band(score);

  return (
    <div className="health-score">
      <div className="health-score-head">
        <span className="health-score-label">Site health</span>
        <span className={`health-score-badge ${b.cls}`}>{b.label}</span>
      </div>
      <div className="health-bar">
        <div
          className={`health-bar-fill ${b.cls}`}
          style={{ width: `${score}%` }}
        />
      </div>
    </div>
  );
}
