const CLASS_LABELS = {
  ok: { label: "OK", tone: "ok" },
  auth: { label: "Auth required", tone: "warn" },
  crash: { label: "Server error", tone: "danger" },
  rejected: { label: "Rejected", tone: "warn" },
  skipped: { label: "Skipped", tone: "skip" },
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

export default function ActiveTestStatusCard({ status }) {
  if (!status) {
    return (
      <section className="panel active-test-status-card">
        <h3>Active Testing</h3>
        <p className="muted small">Not run yet for this audit.</p>
      </section>
    );
  }

  if (status.status === "running") {
    return (
      <section className="panel active-test-status-card">
        <h3>Active Testing</h3>
        <p className="active-test-running small"><span className="chat-live-dot" /> Testing endpoints…</p>
      </section>
    );
  }

  if (status.status === "failed") {
    return (
      <section className="panel active-test-status-card">
        <h3>Active Testing</h3>
        <p className="verify-fail small">Failed: {status.error}</p>
      </section>
    );
  }

  const report = status.result?.report || [];
  const counts = {};
  report.forEach((entry) => {
    const cls = classifyGroup(entry);
    counts[cls] = (counts[cls] || 0) + 1;
  });
  const order = ["ok", "auth", "crash", "rejected", "skipped"];

  return (
    <section className="panel active-test-status-card">
      <h3>Active Testing</h3>
      <p className="muted small">{status.result?.resource_groups_tested ?? 0} resource group(s) tested.</p>
      <div className="status-card-chips">
        {order.filter((k) => counts[k]).map((k) => (
          <span key={k} className={`badge ${CLASS_LABELS[k].tone}`}>{counts[k]} {CLASS_LABELS[k].label}</span>
        ))}
      </div>
    </section>
  );
}
