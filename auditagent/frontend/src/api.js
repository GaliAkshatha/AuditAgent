// In production (Vercel), set VITE_API_BASE_URL to the real deployed
// Render backend URL as a build-time env var. Falls back to localhost
// for local dev, where the backend runs on :8000 by default.
const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

// Shared fetch wrapper — the one place `credentials: "include"` lives,
// instead of repeated across every call site where it's easy to forget
// one and silently break auth for just that endpoint. Frontend (:5173)
// and backend (:8000) are different origins in dev, so without this the
// browser won't send the session cookie at all, no matter how "logged
// in" the user appears to be.
async function apiFetch(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: options.body ? { "Content-Type": "application/json", ...options.headers } : options.headers,
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const error = new Error(err.detail || `Request failed (${res.status})`);
    error.status = res.status;
    throw error;
  }
  return res.json();
}

// ---- Auth --------------------------------------------------------------

export async function registerUser(email, password) {
  return apiFetch("/api/auth/register", { method: "POST", body: JSON.stringify({ email, password }) });
}

export async function loginUser(email, password) {
  return apiFetch("/api/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
}

export async function logoutUser() {
  return apiFetch("/api/auth/logout", { method: "POST" });
}

export async function getCurrentUser() {
  return apiFetch("/api/auth/me");
}

// ---- Per-user target-site credentials -----------------------------------

export async function saveCredential(keyName, value) {
  return apiFetch("/api/credentials", { method: "POST", body: JSON.stringify({ key_name: keyName, value }) });
}

export async function getCredentialStatus() {
  return apiFetch("/api/credentials");
}

export async function deleteCredential(keyName) {
  return apiFetch(`/api/credentials/${keyName}`, { method: "DELETE" });
}

export async function startCodeReview(repo) {
  return apiFetch("/api/code-review", { method: "POST", body: JSON.stringify({ repo }) });
}

export async function getCodeReview(jobId) {
  return apiFetch(`/api/code-review/${jobId}`);
}

// ---- Audits --------------------------------------------------------------

export async function startAudit(payload) {
  return apiFetch("/api/audits", { method: "POST", body: JSON.stringify(payload) });
}

export async function getAudit(jobId) {
  return apiFetch(`/api/audits/${jobId}`);
}

export function reportUrl(jobId) {
  return `${API_BASE}/api/audits/${jobId}/report`;
}

export function pdfReportUrl(outputDir) {
  return `${API_BASE}/api/history/report/pdf?output_dir=${encodeURIComponent(outputDir)}`;
}

export function activeTestPdfUrl(outputDir) {
  return `${API_BASE}/api/active-test/report/pdf?output_dir=${encodeURIComponent(outputDir)}`;
}

// ---- Domain verification ---------------------------------------------------

export async function startVerification(domain) {
  return apiFetch("/api/verify/start", { method: "POST", body: JSON.stringify({ domain }) });
}

export async function checkVerification(domain) {
  return apiFetch("/api/verify/check", { method: "POST", body: JSON.stringify({ domain }) });
}

export async function getVerificationStatus(domain) {
  try {
    return await apiFetch(`/api/verify/status?domain=${encodeURIComponent(domain)}`);
  } catch {
    return { verified: false };
  }
}

// ---- History ---------------------------------------------------------------

export function historyReportUrl(outputDir) {
  return `${API_BASE}/api/history/report?output_dir=${encodeURIComponent(outputDir)}`;
}

export async function getHistory(mostTested = false, limit = 20) {
  return apiFetch(`/api/history?most_tested=${mostTested}&limit=${limit}`);
}

export async function getStats() {
  try {
    return await apiFetch("/api/stats");
  } catch {
    return { total_runs: 0 };
  }
}

// ---- Active testing ----------------------------------------------------

export async function startActiveTest(outputDir) {
  return apiFetch("/api/active-test", { method: "POST", body: JSON.stringify({ output_dir: outputDir }) });
}

export async function getActiveTestStatus(jobId) {
  return apiFetch(`/api/active-test/${jobId}`);
}

export async function rollbackActiveTest(outputDir, execute = false) {
  return apiFetch("/api/active-test/rollback", { method: "POST", body: JSON.stringify({ output_dir: outputDir, execute }) });
}

export async function diagnoseFailure(method, url, body, patient = false) {
  return apiFetch("/api/active-test/diagnose", { method: "POST", body: JSON.stringify({ method, url, body, patient }) });
}

export async function generateActiveTestReport(outputDir, url, report, unifiedReportId) {
  return apiFetch("/api/active-test/report", {
    method: "POST",
    body: JSON.stringify({ output_dir: outputDir, url, report, unified_report_id: unifiedReportId || null }),
  });
}

export async function shareUnifiedReport(reportId) {
  return apiFetch(`/api/report/share?report_id=${encodeURIComponent(reportId)}`, { method: "POST" });
}

export function unifiedReportPdfUrl(reportId) {
  return `${API_BASE}/api/report/pdf?report_id=${encodeURIComponent(reportId)}`;
}

export async function createUnifiedReport(repo, codeReviewResult) {
  return apiFetch("/api/report/create", { method: "POST", body: JSON.stringify({ repo, code_review_result: codeReviewResult }) });
}

export async function updateUnifiedReport(reportId, fields) {
  return apiFetch("/api/report/update", { method: "POST", body: JSON.stringify({ report_id: reportId, ...fields }) });
}

export function unifiedReportUrl(reportId) {
  return `${API_BASE}/api/report/view?report_id=${encodeURIComponent(reportId)}`;
}

// ---- Public demo (unauthenticated) --------------------------------------

export async function startDemoAudit(url) {
  return apiFetch("/api/demo-audit", { method: "POST", body: JSON.stringify({ url }) });
}

export async function getDemoAudit(jobId) {
  return apiFetch(`/api/demo-audit/${jobId}`);
}

export function demoReportUrl(jobId) {
  return `${API_BASE}/api/demo-audit/${jobId}/report`;
}

// ---- Chat --------------------------------------------------------------

export async function sendChatMessage(outputDir, question, provider = "gemini") {
  const data = await apiFetch("/api/chat", { method: "POST", body: JSON.stringify({ output_dir: outputDir, question, provider }) });
  return data.answer;
}
