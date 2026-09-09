import { useState } from "react";
import { startDemoAudit, getDemoAudit, demoReportUrl } from "../api";

const FEATURES = [
  {
    icon: "🕸️",
    title: "Crawl it",
    body: "A real async crawler follows your live app, checks response times, and probes for concurrency issues.",
  },
  {
    icon: "🔍",
    title: "Read the code",
    body: "Point it at your repo and it cross-references every route defined in source against what the crawl actually reached — surfacing endpoints nobody's watching.",
  },
  {
    icon: "⚡",
    title: "Test it for real",
    body: "Opt-in active testing actually calls your mutating endpoints with real test data — then rolls every change back automatically, dependency-ordered.",
  },
  {
    icon: "🔒",
    title: "Safely, by design",
    body: "Domain ownership verification gates anything invasive. Your target-site credentials stay encrypted, private to your account.",
  },
];

export default function LandingPage({ onShowAuth }) {
  const [url, setUrl] = useState("");
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);

  async function handleDemo(e) {
    e.preventDefault();
    setError(null);
    setJob({ status: "running" });
    try {
      const { id } = await startDemoAudit(url);
      poll(id);
    } catch (err) {
      setError(err.message);
      setJob(null);
    }
  }

  function poll(jobId) {
    const timer = setInterval(async () => {
      try {
        const data = await getDemoAudit(jobId);
        setJob({ ...data, id: jobId });
        if (data.status === "complete" || data.status === "failed") clearInterval(timer);
      } catch (err) {
        setError(err.message);
        clearInterval(timer);
      }
    }, 1200);
  }

  return (
    <div className="landing">
      <header className="topbar landing-topbar">
        <div className="wrap topbar-inner">
          <span className="wordmark">AuditAgent</span>
          <div className="topbar-right">
            <button type="button" className="link-btn" onClick={() => onShowAuth("login")}>Log in</button>
            <button type="button" className="primary-btn landing-signup-btn" onClick={() => onShowAuth("register")}>
              Sign up free
            </button>
          </div>
        </div>
      </header>

      <main className="wrap landing-main">
        <section className="landing-hero">
          <div className="landing-hero-badge">✦ AI-agent auditing for early-stage apps</div>
          <h1>Crawl it. Read the code.<br />Find what breaks first.</h1>
          <p className="landing-subhead">
            Passive crawling, endpoint-gap analysis against your real source code, and opt-in
            active testing with automatic rollback — one continuous report from first commit
            to production traffic.
          </p>
          <div className="landing-hero-actions">
            <button type="button" className="primary-btn landing-hero-cta" onClick={() => onShowAuth("register")}>
              Start auditing free
            </button>
            <a href="#demo" className="landing-hero-secondary">Try it without an account ↓</a>
          </div>
        </section>

        <section className="landing-stats-strip">
          <div><span className="landing-stat-value">3</span><span className="landing-stat-label">signals combined</span></div>
          <div><span className="landing-stat-value">0</span><span className="landing-stat-label">setup required for a demo</span></div>
          <div><span className="landing-stat-value">100%</span><span className="landing-stat-label">rollback on active tests</span></div>
        </section>

        <section className="panel landing-demo-panel" id="demo">
          <h2>Try a free demo audit</h2>
          <p className="muted small">
            No account needed. Passive-only, capped at 5 pages — sign up for full audits, active
            testing, and saved history.
          </p>

          {!job ? (
            <form onSubmit={handleDemo} className="landing-demo-form">
              <input
                type="url" required placeholder="https://your-app.com"
                value={url} onChange={(e) => setUrl(e.target.value)}
              />
              <button type="submit" className="primary-btn landing-demo-btn">Run demo audit</button>
            </form>
          ) : job.status === "running" ? (
            <p className="active-test-running"><span className="chat-live-dot" /> Crawling {url}…</p>
          ) : job.status === "failed" ? (
            <div>
              <p className="verify-fail">{job.error || error || "Demo failed — try a different URL."}</p>
              <button type="button" className="link-btn" onClick={() => setJob(null)}>Try again</button>
            </div>
          ) : (
            <div className="reveal">
              <div className="chips">
                <div className="chip">
                  <span className="chip-icon">🕸️</span>
                  <span className="chip-value">{job.summary?.pages_crawled ?? "—"}</span>
                  <span className="chip-label">pages crawled</span>
                </div>
                {job.summary?.top_bottleneck_ms && (
                  <div className="chip warn">
                    <span className="chip-icon">🐢</span>
                    <span className="chip-value">{Math.round(job.summary.top_bottleneck_ms)}ms</span>
                    <span className="chip-label">slowest page</span>
                  </div>
                )}
              </div>
              {job.summary?.report_html && (
                <a className="view-report-btn" href={demoReportUrl(job.id)} target="_blank" rel="noreferrer">
                  View demo report
                </a>
              )}
              <div className="landing-upsell">
                <p><strong>Want the full picture?</strong> Sign up to check every route in your source
                code against what's actually reachable, run real active tests with automatic rollback,
                and keep private history of every run.</p>
                <button type="button" className="primary-btn" onClick={() => onShowAuth("register")}>
                  Sign up free
                </button>
              </div>
            </div>
          )}
          {error && !job && <p className="verify-fail">{error}</p>}
        </section>

        <section className="landing-features">
          <h2 className="landing-section-title">How it works</h2>
          <div className="landing-features-grid">
            {FEATURES.map((f, i) => (
              <div className="landing-feature" key={f.title} style={{ animationDelay: `${i * 0.08}s` }}>
                <span className="landing-feature-icon-badge">{f.icon}</span>
                <h3>{f.title}</h3>
                <p className="muted small">{f.body}</p>
              </div>
            ))}
          </div>
        </section>
      </main>
    </div>
  );
}
