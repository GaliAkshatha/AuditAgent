import { useEffect, useRef, useState } from "react";
import TopBar from "./components/TopBar";
import StatsOverview from "./components/StatsOverview";
import AuditForm from "./components/AuditForm";
import PipelineProgress, { STEPS } from "./components/PipelineProgress";
import ResultCard from "./components/ResultCard";
import HistoryPanel from "./components/HistoryPanel";
import Mascot from "./components/Mascot";
import ChatWidget from "./components/ChatWidget";
import ActiveTestPanel from "./components/ActiveTestPanel";
import ActiveTestStatusCard from "./components/ActiveTestStatusCard";
import ResultsCharts from "./components/ResultsCharts";
import AuthForm from "./components/AuthForm";
import LandingPage from "./components/LandingPage";
import CredentialsPanel from "./components/CredentialsPanel";
import CodeReviewFlow from "./components/CodeReviewFlow";
import { startAudit, getAudit, reportUrl, historyReportUrl, getHistory, getStats, getCurrentUser, logoutUser, updateUnifiedReport, unifiedReportUrl } from "./api";

const STEP_LOG_MESSAGES = {
  crawling: "Crawling live app — following links, checking status codes, observing API calls…",
  analyzing_repo: "Cloning repo and extracting every defined route from source…",
  building_graph: "Merging crawl + repo data into one dependency graph…",
  generating_recommendations: "Retrieving relevant patterns and asking the Architect model…",
  generating_report: "Rendering the final HTML report…",
  complete: "Done.",
};

const SCANNING_STEPS = new Set(["crawling", "analyzing_repo", "building_graph"]);
const THINKING_STEPS = new Set(["generating_recommendations", "generating_report"]);

export default function App() {
  const [authChecked, setAuthChecked] = useState(false);
  const [user, setUser] = useState(null);
  const [view, setView] = useState("audit"); // "audit" | "results" | "active-test" | "history" | "credentials"
  const [job, setJob] = useState(null);
  const [logLines, setLogLines] = useState([]);
  const [error, setError] = useState(null);
  const [historyRows, setHistoryRows] = useState([]);
  const [totalRuns, setTotalRuns] = useState(0);
  const [mostTested, setMostTested] = useState(false);
  const [mascotMood, setMascotMood] = useState("idle");
  const [chatOpen, setChatOpen] = useState(false);
  const [authView, setAuthView] = useState(null); // null (landing) | "login" | "register"
  const [auditStage, setAuditStage] = useState("review"); // "review" | "full"
  const [prefilledRepo, setPrefilledRepo] = useState("");
  const [unifiedReportId, setUnifiedReportId] = useState(null);
  const [activeTestStatus, setActiveTestStatus] = useState(null);
  const pollRef = useRef(null);
  const seenSteps = useRef(new Set());

  // Reflects Active Testing results into the SAME unified report once
  // it completes — this is what makes it one continuous, growing
  // document instead of a separate report per stage.
  useEffect(() => {
    if (unifiedReportId && activeTestStatus?.status === "complete" && activeTestStatus.result?.report) {
      updateUnifiedReport(unifiedReportId, { active_test: { report: activeTestStatus.result.report } }).catch(() => {});
    }
  }, [unifiedReportId, activeTestStatus?.status]);


  useEffect(() => {
    getCurrentUser()
      .then((u) => {
        setUser(u);
        // Fallback entry point for a directly-shared URL to Active
        // Testing (?view=active-test&output_dir=...&url=...) — the
        // primary trigger now navigates in-app like History/Credentials
        // do, not a new tab, but a bookmarked/shared link should still work.
        const params = new URLSearchParams(window.location.search);
        if (params.get("view") === "active-test" && params.get("output_dir") && params.get("url")) {
          setJob({ url: params.get("url"), summary: { output_dir: params.get("output_dir") } });
          setView("active-test");
          if (params.get("report_id")) setUnifiedReportId(params.get("report_id"));
        }
      })
      .catch(() => setUser(null))
      .finally(() => setAuthChecked(true));
  }, []);

  useEffect(() => { if (user) refreshHistory(); }, [mostTested, user]);

  async function handleLogout() {
    try { await logoutUser(); } catch { /* logging out anyway */ }
    setUser(null);
    setJob(null);
    setHistoryRows([]);
    setView("audit");
  }

  // Drive the mascot's mood from real pipeline state — not a canned loop.
  useEffect(() => {
    if (!job) { setMascotMood("idle"); return; }
    if (job.status === "failed") { setMascotMood("error"); return; }
    if (job.status === "running") {
      if (SCANNING_STEPS.has(job.step)) setMascotMood("scanning");
      else if (THINKING_STEPS.has(job.step)) setMascotMood("thinking");
      return;
    }
    if (job.status === "complete" && job.summary) {
      const clean = !job.summary.concurrency_concerning && !job.summary.never_observed_count;
      if (clean) {
        setMascotMood("happy");
      } else {
        setMascotMood("worried");
        const t = setTimeout(() => setMascotMood("proud"), 1400);
        return () => clearTimeout(t);
      }
    }
  }, [job?.status, job?.step]);

  async function refreshHistory() {
    try {
      setHistoryRows(await getHistory(mostTested, 50));
    } catch {
      // Non-fatal — history panel just shows empty state.
    }
    try {
      const stats = await getStats();
      setTotalRuns(stats.total_runs);
    } catch {
      // Non-fatal — sidebar stat just hides.
    }
  }

  async function handleSubmit(payload) {
    setError(null);
    setLogLines([`Starting audit of ${payload.url}…`]);
    seenSteps.current = new Set();
    setActiveTestStatus(null);

    try {
      const { id } = await startAudit(payload);
      setJob({ id, url: payload.url, status: "running", step: "queued", summary: null });
      poll(id, payload.url);
    } catch (err) {
      setError(err.message);
    }
  }

  function poll(jobId, url) {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const data = await getAudit(jobId);
        setJob((prev) => ({ ...prev, status: data.status, step: data.step, summary: data.summary }));

        if (data.step && !seenSteps.current.has(data.step)) {
          seenSteps.current.add(data.step);
          setLogLines((prev) => [...prev, STEP_LOG_MESSAGES[data.step] || data.step]);
        }

        if (data.status === "complete") {
          clearInterval(pollRef.current);
          setLogLines((prev) => [...prev, "✓ Audit complete."]);
          refreshHistory();
          if (unifiedReportId && data.summary) {
            updateUnifiedReport(unifiedReportId, { url, output_dir: data.summary?.output_dir }).catch(() => {});
          }
        } else if (data.status === "failed") {
          clearInterval(pollRef.current);
          setError(data.error || "The pipeline failed — check the server terminal for details.");
          setLogLines((prev) => [...prev, "✗ Pipeline failed."]);
        }
      } catch (err) {
        clearInterval(pollRef.current);
        setError(err.message);
      }
    }, 1200);
  }

  const isRunning = job?.status === "running";

  if (!authChecked) {
    return <div className="auth-wrap"><p className="muted">Loading…</p></div>;
  }

  if (!user) {
    if (authView) {
      return <AuthForm initialMode={authView} onAuthenticated={(u) => setUser(u)} onBack={() => setAuthView(null)} />;
    }
    return <LandingPage onShowAuth={(mode) => setAuthView(mode)} />;
  }

  return (
    <div className="app-shell-topbar">
      <TopBar view={view} onNavigate={setView} userEmail={user.email} onLogout={handleLogout} totalRuns={totalRuns} />

      <main className="shell-main wrap">
        {view === "audit" && (
          <div className={(isRunning || job?.status === "complete") ? "shell-content" : "shell-content-single"}>
            <div className={(isRunning || job?.status === "complete") ? "shell-main-col" : ""}>
              <StatsOverview totalRuns={totalRuns} historyRows={historyRows} />

              {auditStage === "review" && (
                <CodeReviewFlow
                  onAuditDeployed={(repo, reportId) => { setPrefilledRepo(repo); setUnifiedReportId(reportId); setAuditStage("full"); }}
                  onSkip={() => setAuditStage("full")}
                />
              )}

              {auditStage === "full" && (
                <>
                  <AuditForm onSubmit={handleSubmit} disabled={isRunning} defaultRepo={prefilledRepo} />

                  {error && (
                    <div className="result-card error reveal">
                      <h3>Something went wrong</h3>
                      <p className="mono" style={{ fontSize: 13 }}>{error}</p>
                    </div>
                  )}
                </>
              )}

              {!isRunning && (
                <div className="recent-runs-deprioritized">
                  <HistoryPanel
                    rows={historyRows.slice(0, 6)}
                    mostTested={false}
                    toggleLabel="View all →"
                    onToggleMostTested={() => setView("history")}
                    onOpenReport={(row) => window.open(historyReportUrl(row.output_dir), "_blank")}
                  />
                </div>
              )}
            </div>

            {isRunning && (
              <div className="shell-side-col">
                <PipelineProgress url={job.url} currentStep={job.step} status={job.status} logLines={logLines} />
              </div>
            )}

            {!isRunning && job?.status === "complete" && job.summary && (
              <div className="shell-side-col">
                <ResultCard jobId={job.id} summary={job.summary} reportUrl={reportUrl} />
                <button
                  type="button"
                  className="panel active-test-link-card"
                  onClick={() => setView("active-test")}
                >
                  <div>
                    <h3>Start Active Testing</h3>
                    <p className="muted small">Actually calls your mutating endpoints, with automatic rollback.</p>
                  </div>
                  <span className="active-test-link-arrow">→</span>
                </button>
                {unifiedReportId && (
                  <a
                    href={unifiedReportUrl(unifiedReportId)} target="_blank" rel="noreferrer"
                    className="view-report-btn unified-report-link"
                  >
                    📄 View report →
                  </a>
                )}
              </div>
            )}
          </div>
        )}

        {view === "results" && job?.summary && (
          <div className="shell-content-full">
            <div className="results-page-head">
              <button type="button" className="link-btn" onClick={() => setView("audit")}>← Back</button>
              <h1 className="shell-page-title">{job.url}</h1>
            </div>
            <div className="results-grid">
              <div className="results-main-col">
                <ResultCard jobId={job.id} summary={job.summary} reportUrl={reportUrl} />
                <button type="button" className="panel active-test-link-card" onClick={() => setView("active-test")}>
                  <div>
                    <h3>Active Testing</h3>
                    <p className="muted small">Actually calls your mutating endpoints, with automatic rollback.</p>
                  </div>
                  <span className="active-test-link-arrow">→</span>
                </button>
              </div>
              <div className="results-side-col">
                <ResultsCharts summary={job.summary} historyRows={historyRows} />
                <ActiveTestStatusCard status={activeTestStatus} />
              </div>
            </div>
          </div>
        )}

        {view === "active-test" && job?.summary && (
          <div className="shell-content-full">
            <div className="results-page-head">
              <button type="button" className="link-btn" onClick={() => setView("audit")}>← Back to audit</button>
              <h1 className="shell-page-title">Active Testing</h1>
            </div>
            <div className="active-test-page-single">
              <div className="panel active-test-explainer">
                <p>
                  Active Testing actually calls your app's mutating endpoints (POST, PATCH, DELETE)
                  with real, clearly-tagged test data — not just reading what's there. It requires
                  proven domain ownership first, records every change it makes, and can roll every
                  single one back automatically when you're done.
                </p>
              </div>
              <ActiveTestPanel
                url={job.url} outputDir={job.summary?.output_dir}
                onStatusChange={setActiveTestStatus} unifiedReportId={unifiedReportId}
              />
            </div>
          </div>
        )}

        {view === "history" && (
          <div className="shell-content-single">
            <h1 className="shell-page-title">History</h1>
            <HistoryPanel
              rows={historyRows}
              mostTested={mostTested}
              onToggleMostTested={() => setMostTested((v) => !v)}
              onOpenReport={(row) => window.open(historyReportUrl(row.output_dir), "_blank")}
            />
          </div>
        )}

        {view === "credentials" && (
          <div className="shell-content-single">
            <CredentialsPanel onBack={() => setView("audit")} />
          </div>
        )}
      </main>

      <Mascot mood={mascotMood} onClick={() => setChatOpen((v) => !v)} />
      <ChatWidget
        open={chatOpen}
        onClose={() => setChatOpen(false)}
        outputDir={job?.summary?.output_dir}
        provider="gemini"
      />
    </div>
  );
}
