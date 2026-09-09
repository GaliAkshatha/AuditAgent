export default function StatsOverview({ totalRuns, historyRows }) {
  if (!totalRuns) return null;

  const cleanRuns = historyRows.filter(
    (r) => !r.concurrency_concerning && !r.never_observed_count && r.success
  ).length;
  const concurrencyIssues = historyRows.filter((r) => r.concurrency_concerning).length;
  const uniqueSites = new Set(historyRows.map((r) => r.url)).size;

  const stats = [
    { label: "Audits run", value: totalRuns },
    { label: "Sites tracked", value: uniqueSites },
    { label: "Clean runs", value: cleanRuns, tone: "ok" },
    { label: "Concurrency flags", value: concurrencyIssues, tone: concurrencyIssues ? "warn" : "ok" },
  ];

  return (
    <div className="stats-overview">
      {stats.map((s) => (
        <div className={`stats-overview-card ${s.tone || ""}`} key={s.label}>
          <div className="stats-overview-value">{s.value}</div>
          <div className="stats-overview-label">{s.label}</div>
        </div>
      ))}
    </div>
  );
}
