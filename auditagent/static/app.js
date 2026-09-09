const form = document.getElementById("audit-form");
const submitBtn = document.getElementById("submit-btn");
const progressPanel = document.getElementById("progress-panel");
const progressUrl = document.getElementById("progress-url");
const progressResult = document.getElementById("progress-result");
const historyDiv = document.getElementById("history");
const advancedToggle = document.getElementById("advanced-toggle");
const advancedFields = document.getElementById("advanced-fields");
const mostTestedToggle = document.getElementById("most-tested-toggle");

let showingMostTested = false;
let pollTimer = null;

advancedToggle.addEventListener("click", () => {
  const hidden = advancedFields.hasAttribute("hidden");
  if (hidden) advancedFields.removeAttribute("hidden");
  else advancedFields.setAttribute("hidden", "");
  advancedToggle.textContent = hidden ? "Hide advanced options" : "Advanced options";
});

mostTestedToggle.addEventListener("click", () => {
  showingMostTested = !showingMostTested;
  mostTestedToggle.textContent = showingMostTested ? "Show recent runs" : "Show most-tested";
  loadHistory();
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();

  const payload = {
    url: document.getElementById("url").value.trim(),
    repo: document.getElementById("repo").value.trim() || null,
    api_base_url: document.getElementById("api_base_url").value.trim() || null,
    provider: document.getElementById("provider").value,
    max_pages: parseInt(document.getElementById("max_pages").value, 10) || 25,
  };

  submitBtn.disabled = true;
  submitBtn.textContent = "Starting...";
  progressPanel.removeAttribute("hidden");
  progressUrl.textContent = payload.url;
  progressResult.innerHTML = "";
  resetSteps();

  try {
    const res = await fetch("/api/audits", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Request failed (${res.status})`);
    }
    const { id } = await res.json();
    pollJob(id);
  } catch (err) {
    showError(err.message);
    submitBtn.disabled = false;
    submitBtn.textContent = "Run audit";
  }
});

function resetSteps() {
  document.querySelectorAll(".step").forEach((el) => {
    el.classList.remove("active", "done", "failed");
  });
}

function updateSteps(currentStep) {
  const order = ["crawling", "analyzing_repo", "building_graph", "generating_recommendations", "generating_report", "complete"];
  const currentIndex = order.indexOf(currentStep);
  document.querySelectorAll(".step").forEach((el) => {
    const stepIndex = order.indexOf(el.dataset.step);
    el.classList.remove("active", "done");
    if (stepIndex < currentIndex || currentStep === "complete") {
      el.classList.add("done");
    } else if (stepIndex === currentIndex) {
      el.classList.add("active");
    }
  });
}

function pollJob(jobId) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/audits/${jobId}`);
      if (!res.ok) throw new Error("Lost track of this job");
      const job = await res.json();

      updateSteps(job.step);

      if (job.status === "complete") {
        clearInterval(pollTimer);
        showResult(jobId, job.summary);
        submitBtn.disabled = false;
        submitBtn.textContent = "Run audit";
        loadHistory();
      } else if (job.status === "failed") {
        clearInterval(pollTimer);
        document.querySelectorAll(".step.active").forEach((el) => el.classList.add("failed"));
        showError(job.error || "The pipeline failed — check the server terminal for details.");
        submitBtn.disabled = false;
        submitBtn.textContent = "Run audit";
      }
    } catch (err) {
      clearInterval(pollTimer);
      showError(err.message);
      submitBtn.disabled = false;
      submitBtn.textContent = "Run audit";
    }
  }, 1500);
}

function showResult(jobId, summary) {
  const parts = [];
  if (summary.pages_crawled != null) parts.push(`<div><b>${summary.pages_crawled}</b>pages crawled</div>`);
  if (summary.top_bottleneck_ms) parts.push(`<div><b>${summary.top_bottleneck_ms}ms</b>slowest page</div>`);
  if (summary.never_observed_count) parts.push(`<div><b>${summary.never_observed_count}</b>unreached endpoints</div>`);
  if (summary.concurrency_concerning) parts.push(`<div><b style="color:var(--danger)">⚠️</b>concurrency issue</div>`);

  progressResult.innerHTML = `
    <div class="result-card">
      <h3>Audit complete</h3>
      <div class="stats">${parts.join("")}</div>
      ${summary.report_html
        ? `<a class="view-report-btn" href="/api/audits/${jobId}/report" target="_blank">View full report</a>`
        : `<p class="empty">Report generation was skipped or failed — check server logs.</p>`}
    </div>`;
}

function showError(message) {
  progressResult.innerHTML = `
    <div class="result-card error">
      <h3>Something went wrong</h3>
      <p class="mono" style="font-size:13px;">${escapeHtml(message)}</p>
    </div>`;
}

async function loadHistory() {
  try {
    const res = await fetch(`/api/history?most_tested=${showingMostTested}`);
    const rows = await res.json();
    renderHistory(rows);
  } catch {
    historyDiv.innerHTML = `<p class="empty">Couldn't load history.</p>`;
  }
}

function renderHistory(rows) {
  if (!rows.length) {
    historyDiv.innerHTML = `<p class="empty">No runs yet — run your first audit above.</p>`;
    return;
  }

  if (showingMostTested) {
    const body = rows.map(r => `
      <tr>
        <td class="url-cell">${escapeHtml(r.url)}</td>
        <td>${r.run_count}x</td>
        <td>${formatDate(r.last_run)}</td>
      </tr>`).join("");
    historyDiv.innerHTML = `
      <table class="history-table">
        <tr><th>URL</th><th>Runs</th><th>Last tested</th></tr>
        ${body}
      </table>`;
  } else {
    const body = rows.map(r => {
      const badges = [];
      if (r.concurrency_concerning) badges.push(`<span class="badge warn">concurrency</span>`);
      if (r.never_observed_count) badges.push(`<span class="badge warn">${r.never_observed_count} unreached</span>`);
      if (!badges.length && r.success) badges.push(`<span class="badge ok">clean</span>`);
      return `
        <tr>
          <td class="url-cell">${escapeHtml(r.url)}</td>
          <td>${formatDate(r.started_at)}</td>
          <td>${r.top_bottleneck_ms ? Math.round(r.top_bottleneck_ms) + "ms" : "—"}</td>
          <td>${badges.join(" ")}</td>
        </tr>`;
    }).join("");
    historyDiv.innerHTML = `
      <table class="history-table">
        <tr><th>URL</th><th>When</th><th>Bottleneck</th><th></th></tr>
        ${body}
      </table>`;
  }
}

function formatDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

loadHistory();
