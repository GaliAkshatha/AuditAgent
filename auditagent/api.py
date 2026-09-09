"""
AuditAgent — Web API
Wraps orchestrator.py's pipeline behind an HTTP API so the frontend can
kick off an audit, poll live progress, and view the finished report — the
direct answer to the original pitch (paste a URL + repo, click a button).

Job status is tracked in-memory (a dict guarded by a lock) since a job is
only ever running on this one process — no need for a database just to
track "is this job still running." Completed-run HISTORY, on the other
hand, already lives in a real database (auditagent_history.db, written by
orchestrator.py itself) — this API reads that directly for the history
list rather than duplicating it.

Run with:
    uvicorn api:app --reload --port 8000
Then open http://localhost:8000 in a browser.
"""

import json
import os
import db
import shutil
import tempfile
import threading
import time
import uuid
from typing import Optional
from urllib.parse import urlparse

import requests

from fastapi import FastAPI, HTTPException, Depends, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import orchestrator
import code_reviewer
import active_test_report
import pdf_export
import unified_report
import report_generator
import chatbot as chatbot_module
import domain_verify
import auth
import credentials as creds
from chat_context import AuditContext
from active_tester import ActiveTester, build_authenticated_session, attempt_self_cleanup, diagnose_failure, detect_register_endpoint
from test_ledger import TestLedger, rollback as run_rollback

app = FastAPI(title="AuditAgent API")
# allow_origins=["*"] is incompatible with cookie-based sessions — browsers
# explicitly forbid a wildcard origin combined with credentialed requests
# (allow_credentials=True), so this needs an explicit list/pattern, not
# a wildcard.
#
# Hardcoding just :5173 was fragile in practice — Vite auto-increments
# to :5174, :5175, etc. whenever the default port is already taken by
# something else, and a mismatched origin fails the CORS preflight with
# a 400 (confirmed directly: curl'd the OPTIONS request from both a
# matching and a mismatched origin and got 200 vs 400 respectively) —
# which looks exactly like "registration silently doesn't work," since
# the browser blocks the real request before it's ever sent. Using
# allow_origin_regex for local dev covers any localhost/127.0.0.1 port
# instead of guessing the right one.
ALLOWED_ORIGINS = []
if os.environ.get("FRONTEND_URL"):
    ALLOWED_ORIGINS.append(os.environ["FRONTEND_URL"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- Auth -------------------------------------------------------------------

class RegisterUserRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


def _set_session_cookie(response: Response, user_id: int):
    token = auth.create_session_token(user_id)
    response.set_cookie(
        key=auth.COOKIE_NAME, value=token, httponly=True, samesite="lax",
        max_age=auth.SESSION_MAX_AGE, path="/",
    )


@app.post("/api/auth/register")
def register(req: RegisterUserRequest, response: Response):
    try:
        user = auth.register_user(req.email, req.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _set_session_cookie(response, user["id"])
    return {"id": user["id"], "email": user["email"]}


@app.post("/api/auth/login")
def login(req: LoginRequest, response: Response):
    user = auth.authenticate_user(req.email, req.password)
    if not user:
        raise HTTPException(401, "Incorrect email or password")
    _set_session_cookie(response, user["id"])
    return {"id": user["id"], "email": user["email"]}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(user_id: int = Depends(auth.get_current_user_id)):
    # Return the same shape as register/login ({id, email}), not just the
    # id — the frontend needs the email to render the header's user menu
    # after a page reload, when it only has the session cookie to work
    # from, not the original login/register response. Caught this by
    # testing session persistence across an actual reload, not just
    # within one continuous page session.
    user = auth.get_user_by_id(user_id)
    if not user:
        raise HTTPException(401, "User no longer exists")
    return user


# ---- Per-user target-site credentials (encrypted at rest) -------------------

class SaveCredentialRequest(BaseModel):
    key_name: str
    value: str


@app.post("/api/credentials")
def save_credential(req: SaveCredentialRequest, user_id: int = Depends(auth.get_current_user_id)):
    try:
        creds.save_user_credential(user_id, req.key_name, req.value)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/credentials")
def list_credential_status(user_id: int = Depends(auth.get_current_user_id)):
    """Returns which keys are configured — never the actual secret values."""
    return creds.get_user_credential_status(user_id)


@app.delete("/api/credentials/{key_name}")
def delete_credential(key_name: str, user_id: int = Depends(auth.get_current_user_id)):
    creds.delete_user_credential(user_id, key_name)
    return {"ok": True}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- In-memory job tracking (this process only ever runs one job at a
# time in practice, but the dict supports concurrent jobs if that changes) --

_jobs_lock = threading.Lock()
_jobs: dict[str, dict] = {}

_active_test_jobs_lock = threading.Lock()
_active_test_jobs: dict[str, dict] = {}

STEP_LABELS = {
    "crawling": "Crawling live app",
    "analyzing_repo": "Analyzing repo source code",
    "building_graph": "Building dependency graph",
    "generating_recommendations": "Generating scalability recommendations",
    "generating_report": "Generating final report",
    "complete": "Complete",
}


_code_review_jobs_lock = threading.Lock()
_code_review_jobs: dict[str, dict] = {}


class CodeReviewRequest(BaseModel):
    repo: str


def _run_code_review(job_id: str, repo: str, user_id: int):
    try:
        result = code_reviewer.review_code(repo, provider="gemini")
        with _code_review_jobs_lock:
            _code_review_jobs[job_id].update({"status": "complete", "result": result})
    except Exception as e:
        with _code_review_jobs_lock:
            _code_review_jobs[job_id].update({"status": "failed", "error": str(e)})


@app.post("/api/code-review")
@app.post("/api/code-review")
def start_code_review(req: CodeReviewRequest, user_id: int | None = Depends(auth.get_current_user_id_optional)):
    """Pure static, code-only review — no live URL involved at all, and
    (per the new policy) no account required either: reading public
    source code isn't invasive the way crawling or mutating a live
    deployment is, so this is open to anyone. user_id is still recorded
    when someone IS logged in, so their own review stays private to
    them — but an anonymous request gets a job scoped only by its random
    job_id, the same trust model the public demo-audit endpoint already
    uses."""
    job_id = str(uuid.uuid4())[:8]
    with _code_review_jobs_lock:
        _code_review_jobs[job_id] = {"id": job_id, "user_id": user_id, "status": "running", "result": None}
    thread = threading.Thread(target=_run_code_review, args=(job_id, req.repo, user_id), daemon=True)
    thread.start()
    return {"id": job_id, "status": "running"}


@app.get("/api/code-review/{job_id}")
def get_code_review(job_id: str, user_id: int | None = Depends(auth.get_current_user_id_optional)):
    with _code_review_jobs_lock:
        job = _code_review_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No code review job with that ID")
    # A job created by a specific logged-in user stays private to them.
    # A job created anonymously has no owner to check against — the
    # random job_id itself is the access control, same trust model as
    # the public demo-audit endpoint.
    if job.get("user_id") is not None and job.get("user_id") != user_id:
        raise HTTPException(404, "No code review job with that ID")
    return job


class CreateUnifiedReportRequest(BaseModel):
    repo: str
    code_review_result: dict


class UpdateUnifiedReportRequest(BaseModel):
    report_id: str
    url: str | None = None
    output_dir: str | None = None
    active_test: dict | None = None
    suggestions: dict | None = None


@app.post("/api/report/create")
def create_unified_report(req: CreateUnifiedReportRequest, user_id: int | None = Depends(auth.get_current_user_id_optional)):
    """One continuous report starting right after code review, growing
    through the deployed audit and active testing — not three
    disconnected reports. Open to anonymous callers, matching code
    review's own policy: reading public code isn't invasive."""
    report_id = unified_report.create_report(user_id, req.repo, req.code_review_result)
    return {"report_id": report_id}


@app.post("/api/report/update")
def update_unified_report(req: UpdateUnifiedReportRequest):
    """No auth required by design — the report_id itself (a random
    token) is the access control, same trust model as the code-review
    job it's built from, so it stays updatable through an anonymous
    flow too."""
    row = unified_report.get_report_row(req.report_id)
    if row is None:
        raise HTTPException(404, "No report with that ID")

    fields = {}
    if req.url is not None:
        fields["url"] = req.url
    if req.output_dir is not None:
        # Building the rich fragment SERVER-SIDE, from the same files
        # report_generator.py's own standalone report reads — this is
        # what makes the combined report show the same depth of detail
        # you liked in that one, instead of a watered-down summary.
        runs_root = os.path.abspath(os.path.join(SCRIPT_DIR, "runs"))
        candidate = os.path.abspath(os.path.join(SCRIPT_DIR, req.output_dir))
        if candidate.startswith(runs_root + os.sep):
            crawl_report = report_generator.load_json(os.path.join(candidate, "report.json"))
            merged = report_generator.load_json(os.path.join(candidate, "merged.json"))
            graph_image = report_generator.embed_image_base64(os.path.join(candidate, "graph.png"))
            content_html = report_generator.build_report_content(crawl_report, merged, graph_image, "")

            probe = crawl_report.get("concurrency_probe")
            bottlenecks = merged.get("metrics", {}).get("bottlenecks", [])
            never_observed = merged.get("merge_summary", {}).get("never_observed", [])
            concerning = bool(probe and probe.get("concerning"))
            top_bottleneck_ms = bottlenecks[0]["response_time_ms"] if bottlenecks else None
            score = report_generator.compute_health_score(concerning, top_bottleneck_ms, len(never_observed))
            _, badge_tone = report_generator.health_band(score)

            fields["audit_summary"] = {
                "content_html": content_html, "badge_tone": badge_tone,
                "badge_label": "issues found" if (concerning or never_observed) else "clean",
            }
    if req.active_test is not None:
        fields["active_test"] = req.active_test
    if req.suggestions is not None:
        fields["suggestions"] = req.suggestions
    unified_report.update_report(req.report_id, **fields)
    return {"ok": True}


@app.get("/api/report/view", response_class=HTMLResponse)
def view_unified_report(report_id: str):
    """Deliberately public — meant to be shareable the same way the
    active-test report is."""
    row = unified_report.get_report_row(report_id)
    if row is None:
        raise HTTPException(404, "No report with that ID")
    return unified_report.build_unified_report_html(row)


@app.post("/api/report/share")
def share_unified_report(report_id: str, request: Request):
    """Returns a shortened, shareable link to the ONE report — this is
    the single report the whole product now points at; there is no
    separate active-test-only report to generate or shorten anymore."""
    row = unified_report.get_report_row(report_id)
    if row is None:
        raise HTTPException(404, "No report with that ID")

    full_url = f"{request.url.scheme}://{request.url.netloc}/api/report/view?report_id={report_id}"
    short_url = full_url
    if SHORTENER_URL:
        try:
            resp = requests.post(f"{SHORTENER_URL}/shorten", json={"url": full_url}, timeout=5)
            if resp.status_code == 200:
                short_url = resp.json()["short_url"]
        except requests.RequestException:
            pass
    return {"report_url": full_url, "short_url": short_url}


@app.get("/api/report/pdf")
def download_unified_report_pdf(report_id: str):
    """PDF export for the same one report — renders the current state to
    a temp file, converts it, cleans up. Public, matching the report's
    own sharing model: whoever has the link can view or download it."""
    row = unified_report.get_report_row(report_id)
    if row is None:
        raise HTTPException(404, "No report with that ID")

    html = unified_report.build_unified_report_html(row)
    tmp_dir = tempfile.mkdtemp(prefix="auditagent_report_")
    html_path = os.path.join(tmp_dir, "report.html")
    pdf_path = os.path.join(tmp_dir, "report.pdf")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    try:
        pdf_export.html_to_pdf(html_path, pdf_path)
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="auditagent-report.pdf"'},
    )


class AuditRequest(BaseModel):
    url: str
    repo: Optional[str] = None
    api_base_url: Optional[str] = None
    provider: str = "gemini"
    max_pages: int = 25
    concurrency: int = 5
    patient: bool = False


# ---- Public demo (unauthenticated, deliberately constrained) ---------------
# Lets a visitor try a real, live audit before creating an account — but
# this endpoint is reachable by anyone on the internet, so it needs real
# constraints an authenticated request doesn't: no repo (avoids cloning
# arbitrary, possibly huge repos anonymously), a small forced max_pages
# cap, and the concurrency probe force-disabled regardless of domain
# verification status — an anonymous visitor triggering deliberate load
# against a domain isn't something the tool should ever allow, verified
# or not. Demo runs use user_id=None, which the runs table already
# supports (nullable) — they're simply invisible to every per-user
# history query (all of which filter WHERE user_id = ?), so no separate
# storage or cleanup logic is needed for isolation.
_demo_jobs_lock = threading.Lock()
_demo_jobs: dict[str, dict] = {}
_demo_rate_limit: dict[str, list[float]] = {}
DEMO_MAX_PAGES = 5
DEMO_RATE_LIMIT_COUNT = 3
DEMO_RATE_LIMIT_WINDOW_S = 60 * 60


def _check_demo_rate_limit(ip: str) -> bool:
    """Basic in-memory sliding-window limiter — good enough to deter casual
    abuse of a public demo endpoint, not a substitute for a real
    rate-limiting layer (Cloudflare, a proper token-bucket service, etc.)
    if this ever sees real traffic at scale. Honest about that limit
    rather than presenting this as more robust than it is."""
    now = time.time()
    recent = [t for t in _demo_rate_limit.get(ip, []) if now - t < DEMO_RATE_LIMIT_WINDOW_S]
    _demo_rate_limit[ip] = recent
    if len(recent) >= DEMO_RATE_LIMIT_COUNT:
        return False
    recent.append(now)
    return True


class DemoAuditRequest(BaseModel):
    url: str


def _run_demo_job(job_id: str, url: str):
    def on_progress(step: str):
        with _demo_jobs_lock:
            _demo_jobs[job_id]["step"] = step
            _demo_jobs[job_id]["step_label"] = STEP_LABELS.get(step, step)

    try:
        summary = orchestrator.run_pipeline(
            url=url, repo=None, api_base_url=None, provider="gemini",
            concurrency=3, max_pages=DEMO_MAX_PAGES, skip_report=False,
            on_progress=on_progress, probe_concurrency=False, user_id=None,
        )
        with _demo_jobs_lock:
            _demo_jobs[job_id].update({
                "status": "complete" if summary.get("success") else "failed",
                "summary": summary,
            })
    except Exception as e:
        with _demo_jobs_lock:
            _demo_jobs[job_id].update({"status": "failed", "error": str(e)})


@app.post("/api/demo-audit")
def start_demo_audit(req: DemoAuditRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    if not _check_demo_rate_limit(ip):
        raise HTTPException(
            429,
            f"Demo limit reached ({DEMO_RATE_LIMIT_COUNT} per hour) — sign up for unlimited audits.",
        )

    job_id = str(uuid.uuid4())[:8]
    with _demo_jobs_lock:
        _demo_jobs[job_id] = {
            "id": job_id, "url": req.url, "status": "running",
            "step": "queued", "step_label": "Queued", "summary": None, "error": None,
        }
    thread = threading.Thread(target=_run_demo_job, args=(job_id, req.url), daemon=True)
    thread.start()
    return {"id": job_id, "status": "running"}


@app.get("/api/demo-audit/{job_id}")
def get_demo_audit(job_id: str):
    with _demo_jobs_lock:
        job = _demo_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No demo job with that ID")
    return job


@app.get("/api/demo-audit/{job_id}/report", response_class=HTMLResponse)
def get_demo_report(job_id: str):
    with _demo_jobs_lock:
        job = _demo_jobs.get(job_id)
    if job is None or not job.get("summary") or not job["summary"].get("report_html"):
        raise HTTPException(404, "Report not available yet or job not found")
    report_path = job["summary"]["report_html"]
    if not os.path.isfile(report_path):
        raise HTTPException(404, "Report file missing on disk")
    return FileResponse(report_path, media_type="text/html")


def _run_job(job_id: str, req: AuditRequest, user_id: int):
    def on_progress(step: str):
        with _jobs_lock:
            _jobs[job_id]["step"] = step
            _jobs[job_id]["step_label"] = STEP_LABELS.get(step, step)

    domain = urlparse(req.url).netloc
    verified = domain_verify.is_verified(domain)

    try:
        summary = orchestrator.run_pipeline(
            url=req.url, repo=req.repo, api_base_url=req.api_base_url, provider=req.provider,
            concurrency=req.concurrency, max_pages=req.max_pages, skip_report=False,
            on_progress=on_progress, probe_concurrency=verified, user_id=user_id,
            patient=req.patient,
        )
        with _jobs_lock:
            _jobs[job_id].update({
                "status": "complete" if summary.get("success") else "failed",
                "summary": summary,
                "domain_verified": verified,
            })
    except Exception as e:
        with _jobs_lock:
            _jobs[job_id].update({"status": "failed", "error": str(e)})


@app.post("/api/audits")
def start_audit(req: AuditRequest, user_id: int = Depends(auth.get_current_user_id)):
    # New policy: auditing a DEPLOYED site requires both an account
    # (already enforced above) AND proven domain ownership — previously
    # verification only gated the invasive concurrency probe, letting
    # anyone logged in passively crawl any URL at all. Reading public
    # GitHub code needs neither (see start_code_review) since it isn't
    # invasive the same way touching a live deployment is — but running
    # any audit at all against a deployed site now requires proving you
    # actually own it, not just being logged in.
    domain = urlparse(req.url).netloc
    if not domain_verify.is_verified(domain):
        raise HTTPException(
            403,
            f"{domain} isn't verified yet. Auditing a deployed site requires proven domain "
            f"ownership — verify it first (Advanced options has a link), then run the audit.",
        )

    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "url": req.url, "repo": req.repo, "user_id": user_id,
            "status": "running", "step": "queued", "step_label": "Queued",
            "summary": None, "error": None,
        }
    thread = threading.Thread(target=_run_job, args=(job_id, req, user_id), daemon=True)
    thread.start()
    return {"id": job_id, "status": "running"}


@app.get("/api/audits/{job_id}")
def get_audit(job_id: str, user_id: int = Depends(auth.get_current_user_id)):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "No job with that ID (may have been from a previous server run)")
    if job.get("user_id") != user_id:
        raise HTTPException(404, "No job with that ID")  # 404, not 403 — don't confirm the ID exists at all
    return job


@app.get("/api/audits/{job_id}/report", response_class=HTMLResponse)
def get_report(job_id: str, user_id: int = Depends(auth.get_current_user_id)):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None or job.get("user_id") != user_id or not job.get("summary") or not job["summary"].get("report_html"):
        raise HTTPException(404, "Report not available yet or job not found")
    report_path = job["summary"]["report_html"]
    if not os.path.isfile(report_path):
        raise HTTPException(404, "Report file missing on disk")
    return FileResponse(report_path, media_type="text/html")


@app.get("/api/history")
def get_history(most_tested: bool = False, limit: int = 20, user_id: int = Depends(auth.get_current_user_id)):
    """Reads directly from orchestrator.py's own history database, scoped
    to the logged-in user's own runs — this is what makes "recent runs"
    genuinely private per account, not a shared global list.

    Calls orchestrator.init_db() (idempotent — CREATE TABLE IF NOT EXISTS)
    rather than just checking whether the DB *file* exists. Registering a
    user creates that file too (via auth.init_auth_tables()), so a brand
    new account hitting this before ever running an audit would otherwise
    find the file present but the `runs` table itself still missing —
    a real 500 caught by testing session persistence across a page
    reload, not something that showed up in earlier same-session tests."""
    conn = orchestrator.init_db()
    if most_tested:
        cur = db.execute(conn, """
            SELECT url, COUNT(*) as run_count, MAX(started_at) as last_run
            FROM runs WHERE user_id = ? GROUP BY url ORDER BY run_count DESC LIMIT ?
        """, (user_id, limit))
    else:
        cur = db.execute(conn, """
            SELECT url, repo, started_at, duration_s, top_bottleneck_ms,
                   never_observed_count, concurrency_concerning, success, output_dir
            FROM runs WHERE user_id = ? ORDER BY started_at DESC LIMIT ?
        """, (user_id, limit))
    rows = db.dict_rows(cur)
    conn.close()
    return rows


class VerifyDomainRequest(BaseModel):
    domain: str


@app.post("/api/verify/start")
def verify_start(req: VerifyDomainRequest, user_id: int = Depends(auth.get_current_user_id)):
    """Begins domain ownership verification — generates a token and
    returns instructions for both proof methods (well-known file or DNS
    TXT record). Passive crawling doesn't need this; it gates the
    concurrency probe specifically. Requires login for basic
    accountability (so this can't be hit anonymously), even though
    verification status itself is correctly a property of the DOMAIN, not
    the user — if two different users can both complete the same
    well-known-file/DNS check, they've each independently proven control,
    so there's no cross-user leak in sharing that result."""
    return domain_verify.start_verification(req.domain)


@app.post("/api/verify/check")
def verify_check(req: VerifyDomainRequest, user_id: int = Depends(auth.get_current_user_id)):
    """Actually checks both methods over the network and updates status."""
    return domain_verify.check_verification(req.domain)


@app.get("/api/verify/status")
def verify_status(domain: str, user_id: int = Depends(auth.get_current_user_id)):
    return {"domain": domain, "verified": domain_verify.is_verified(domain)}


@app.get("/api/stats")
def get_stats(user_id: int = Depends(auth.get_current_user_id)):
    """This user's own total run count — same privacy scoping as history.
    Same init_db() fix as get_history — see its docstring for why a file-
    existence check alone isn't sufficient once auth.py can also create
    that file."""
    conn = orchestrator.init_db()
    count = db.execute(conn, "SELECT COUNT(*) FROM runs WHERE user_id = ?", (user_id,)).fetchone()[0]
    conn.close()
    return {"total_runs": count}


class ChatRequest(BaseModel):
    output_dir: str
    question: str
    provider: str = "gemini"


@app.post("/api/chat")
def chat(req: ChatRequest, user_id: int = Depends(auth.get_current_user_id)):
    """Wires chatbot.py's ask() into the web app. Scoped to a specific
    run's output_dir — validates both that the path is safe (no traversal
    outside runs/) AND that this output_dir actually belongs to the
    requesting user via the history table, not just that the path looks
    well-formed. Without the ownership check, any logged-in user could
    chat about any other user's audit data just by guessing a folder
    name — same pattern as get_history_report/start_active_test."""
    runs_root = os.path.abspath(os.path.join(SCRIPT_DIR, "runs"))
    candidate = os.path.abspath(os.path.join(SCRIPT_DIR, req.output_dir))
    if not candidate.startswith(runs_root + os.sep):
        raise HTTPException(400, "Invalid output_dir")

    conn = orchestrator.init_db()
    owner_row = db.execute(
        conn, "SELECT user_id FROM runs WHERE output_dir = ?", (req.output_dir,)
    ).fetchone()
    conn.close()
    if not owner_row or owner_row[0] != user_id:
        raise HTTPException(404, "No run found for this output_dir")

    report_path = os.path.join(candidate, "report.json")
    endpoints_path = os.path.join(candidate, "endpoints.json")
    if not os.path.isfile(report_path):
        raise HTTPException(404, "No crawl data found for this run")

    endpoints_arg = endpoints_path if os.path.isfile(endpoints_path) else None

    try:
        context = AuditContext(report_path, endpoints_path=endpoints_arg)
        answer = chatbot_module.ask(context, req.question, req.provider, None, verbose=False)
    except RuntimeError as e:
        # Missing API key, provider error, etc. — a real, expected failure
        # mode, not a bug — surface it cleanly rather than a 500.
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Chat failed: {e}")

    return {"answer": answer}


class ActiveTestRequest(BaseModel):
    output_dir: str  # a completed audit run's output_dir — reuses its endpoints.json


def _run_active_test(job_id: str, req: ActiveTestRequest, user_id: int):
    runs_root = os.path.abspath(os.path.join(SCRIPT_DIR, "runs"))
    candidate = os.path.abspath(os.path.join(SCRIPT_DIR, req.output_dir))
    if not candidate.startswith(runs_root + os.sep):
        with _active_test_jobs_lock:
            _active_test_jobs[job_id].update({"status": "failed", "error": "Invalid output_dir"})
        return

    report_path = os.path.join(candidate, "report.json")
    endpoints_path = os.path.join(candidate, "endpoints.json")
    merged_path = os.path.join(candidate, "merged.json")

    try:
        base_url = None
        if os.path.isfile(merged_path):
            with open(merged_path) as f:
                merged_data = json.load(f)
            base_url = merged_data.get("merge_summary", {}).get("api_base_url_used")

        if not base_url:
            with open(report_path) as f:
                base_url = json.load(f)["start_url"]

        with open(endpoints_path) as f:
            endpoints_data = json.load(f)
        endpoints = endpoints_data.get("endpoints", [])

        if not endpoints:
            with _active_test_jobs_lock:
                _active_test_jobs[job_id].update({
                    "status": "complete",
                    "result": {"resource_groups_tested": 0, "report": []},
                    "note": "No endpoints available — this audit run had no repo/static-analysis data.",
                })
            return

        # This user's OWN encrypted credentials, decrypted just for this
        # run — never another user's, never falling back to a shared
        # server-wide .env. This is what makes per-account credentials
        # actually mean something rather than everyone sharing one
        # operator's target-site login.
        user_env = creds.get_user_credentials(user_id)
        session, auto_created_account = build_authenticated_session(env=user_env)

        # If this run has no credentials configured at all, check whether
        # a register endpoint was already discovered during static
        # analysis — no reason to make someone manually type a URL the
        # audit already found. Surfaced as a one-click suggestion in the
        # UI rather than making them visit the Credentials panel and
        # copy-paste something we already know.
        detected_register_url = None
        if not any(user_env.get(k) for k in ("AUDIT_REGISTER_URL", "AUDIT_BEARER_TOKEN", "AUDIT_LOGIN_URL")):
            detected_register_url = detect_register_endpoint(base_url, endpoints)

        tester = ActiveTester(base_url, endpoints, session=session)
        result = tester.run(dry_run=False)

        if auto_created_account:
            attempt_self_cleanup(session, base_url, endpoints, auto_created_account)

        ledger_path = os.path.join(candidate, "active_test_ledger.json")
        tester.ledger.save(ledger_path)

        with _active_test_jobs_lock:
            _active_test_jobs[job_id].update({
                "status": "complete",
                "result": result,
                "ledger_path": ledger_path,
                "output_dir": req.output_dir,
                "detected_register_url": detected_register_url,
            })
    except Exception as e:
        with _active_test_jobs_lock:
            _active_test_jobs[job_id].update({"status": "failed", "error": str(e)})


@app.post("/api/active-test")
def start_active_test(req: ActiveTestRequest, user_id: int = Depends(auth.get_current_user_id)):
    # Verify this output_dir actually belongs to the requesting user before
    # doing anything with it — same ownership check pattern as
    # get_history_report, otherwise any logged-in user could point this at
    # any run folder name they can guess.
    conn = orchestrator.init_db()
    owner_row = db.execute(
        conn, "SELECT user_id FROM runs WHERE output_dir = ?", (req.output_dir,)
    ).fetchone()
    conn.close()
    if not owner_row or owner_row[0] != user_id:
        raise HTTPException(404, "No run found for this output_dir")

    domain = None
    try:
        with open(os.path.join(SCRIPT_DIR, req.output_dir, "report.json")) as f:
            domain = urlparse(json.load(f)["start_url"]).netloc
    except Exception:
        pass

    if domain and not domain_verify.is_verified(domain):
        raise HTTPException(
            403,
            f"{domain} isn't verified yet. Active testing calls real mutating "
            "endpoints — verify domain ownership first (same as the concurrency probe).",
        )

    job_id = str(uuid.uuid4())[:8]
    with _active_test_jobs_lock:
        _active_test_jobs[job_id] = {
            "id": job_id, "user_id": user_id, "status": "running", "result": None, "error": None,
        }
    thread = threading.Thread(target=_run_active_test, args=(job_id, req, user_id), daemon=True)
    thread.start()
    return {"id": job_id, "status": "running"}


@app.get("/api/active-test/{job_id}")
def get_active_test(job_id: str, user_id: int = Depends(auth.get_current_user_id)):
    with _active_test_jobs_lock:
        job = _active_test_jobs.get(job_id)
    if job is None or job.get("user_id") != user_id:
        raise HTTPException(404, "No active-test job with that ID")
    return job


class RollbackRequest(BaseModel):
    output_dir: str
    execute: bool = False  # dry-run (safe preview) unless explicitly true


class DiagnoseRequest(BaseModel):
    method: str
    url: str
    body: dict | None = None
    patient: bool = False


@app.post("/api/active-test/diagnose")
def diagnose_active_test_failure(req: DiagnoseRequest, user_id: int = Depends(auth.get_current_user_id)):
    """Opt-in only — never runs automatically. Re-sends the exact same
    failing request several times with pauses in between and classifies
    the pattern (consistent bug / cold start / intermittent load issue).
    Doesn't require an output_dir since it's a standalone retry against a
    URL the frontend already has from a completed active-test result —
    but does need the current user's credentials to retry authenticated
    the same way the original request was."""
    user_env = creds.get_user_credentials(user_id)
    session, _ = build_authenticated_session(env=user_env)
    result = diagnose_failure(session, req.method, req.url, req.body, patient=req.patient)
    return result


@app.post("/api/active-test/rollback")
def rollback_active_test(req: RollbackRequest, user_id: int = Depends(auth.get_current_user_id)):
    runs_root = os.path.abspath(os.path.join(SCRIPT_DIR, "runs"))
    candidate = os.path.abspath(os.path.join(SCRIPT_DIR, req.output_dir))
    if not candidate.startswith(runs_root + os.sep):
        raise HTTPException(400, "Invalid output_dir")

    conn = orchestrator.init_db()
    owner_row = db.execute(
        conn, "SELECT user_id FROM runs WHERE output_dir = ?", (req.output_dir,)
    ).fetchone()
    conn.close()
    if not owner_row or owner_row[0] != user_id:
        raise HTTPException(404, "No run found for this output_dir")

    ledger_path = os.path.join(candidate, "active_test_ledger.json")
    if not os.path.isfile(ledger_path):
        raise HTTPException(404, "No active-test ledger found for this run")

    ledger = TestLedger.load(ledger_path)
    user_env = creds.get_user_credentials(user_id)
    session, _ = build_authenticated_session(env=user_env)

    result = run_rollback(ledger, session=session, dry_run=not req.execute)
    return result


class GenerateActiveTestReportRequest(BaseModel):
    output_dir: str
    url: str
    report: list[dict]
    unified_report_id: str | None = None


SHORTENER_URL = os.environ.get("SHORTENER_URL")  # e.g. http://localhost:8080 — optional


@app.post("/api/active-test/report")
def generate_active_test_report(req: GenerateActiveTestReportRequest, request: Request, user_id: int = Depends(auth.get_current_user_id)):
    """Generates the improvement/scalability suggestions and reflects
    them — along with the active-test findings — into the ONE unified
    report. There is no separate standalone active-test report anymore;
    everything lives in the same continuous document that started at
    code review, and that document is what gets shared and downloaded."""
    if not req.unified_report_id:
        raise HTTPException(400, "unified_report_id is required — there's no separate report to generate without it")

    runs_root = os.path.abspath(os.path.join(SCRIPT_DIR, "runs"))
    candidate = os.path.abspath(os.path.join(SCRIPT_DIR, req.output_dir))
    if not candidate.startswith(runs_root + os.sep):
        raise HTTPException(400, "Invalid output_dir")

    conn = orchestrator.init_db()
    owner_row = db.execute(
        conn, "SELECT user_id FROM runs WHERE output_dir = ?", (req.output_dir,)
    ).fetchone()
    conn.close()
    if not owner_row or owner_row[0] != user_id:
        raise HTTPException(404, "No run found for this output_dir")

    suggestions = active_test_report.generate_suggestions(req.report)
    unified_report.update_report(
        req.unified_report_id,
        active_test={"report": req.report}, suggestions=suggestions,
    )

    full_report_url = f"{request.url.scheme}://{request.url.netloc}/api/report/view?report_id={req.unified_report_id}"
    short_url = full_report_url
    if SHORTENER_URL:
        try:
            resp = requests.post(f"{SHORTENER_URL}/shorten", json={"url": full_report_url}, timeout=5)
            if resp.status_code == 200:
                short_url = resp.json()["short_url"]
        except requests.RequestException:
            pass  # shortener unreachable — fall back to the full URL, not a failure

    return {"report_url": full_report_url, "short_url": short_url, "suggestions_error": suggestions.get("error")}


def _html_report_to_pdf_response(html_path: str, pdf_filename: str) -> FileResponse:
    """Shared by both PDF download endpoints — converts on demand rather
    than pre-generating/caching, since report generation is infrequent
    enough that a few extra seconds per download isn't worth the added
    complexity of cache invalidation."""
    if not os.path.isfile(html_path):
        raise HTTPException(404, "No report found to convert")
    pdf_path = html_path.rsplit(".", 1)[0] + ".pdf"
    pdf_export.html_to_pdf(os.path.abspath(html_path), pdf_path)
    return FileResponse(
        pdf_path, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{pdf_filename}"'},
    )


@app.get("/api/history/report/pdf")
def download_audit_report_pdf(output_dir: str, user_id: int = Depends(auth.get_current_user_id)):
    runs_root = os.path.abspath(os.path.join(SCRIPT_DIR, "runs"))
    candidate = os.path.abspath(os.path.join(SCRIPT_DIR, output_dir))
    if not candidate.startswith(runs_root + os.sep):
        raise HTTPException(400, "Invalid output_dir")

    conn = orchestrator.init_db()
    owner_row = db.execute(conn, "SELECT user_id FROM runs WHERE output_dir = ?", (output_dir,)).fetchone()
    conn.close()
    if not owner_row or owner_row[0] != user_id:
        raise HTTPException(404, "No run found for this output_dir")

    return _html_report_to_pdf_response(os.path.join(candidate, "report.html"), "auditagent-report.pdf")


@app.get("/api/history/report", response_class=HTMLResponse)
def get_history_report(output_dir: str, user_id: int = Depends(auth.get_current_user_id)):
    """Serves a report from a past run (by output_dir, from the history
    table) — separate from /api/audits/{id}/report because history rows
    persist across server restarts (SQLite) while in-memory job IDs don't.
    Validates the path stays inside runs/ to prevent directory traversal —
    this endpoint takes a client-supplied path, so that check is load-bearing,
    not decorative. Also verifies this output_dir actually belongs to the
    requesting user via the history table, not just that the path is safe —
    otherwise any logged-in user could view any other user's report just by
    guessing/copying a folder name."""
    runs_root = os.path.abspath(os.path.join(SCRIPT_DIR, "runs"))
    candidate = os.path.abspath(os.path.join(SCRIPT_DIR, output_dir))
    if not candidate.startswith(runs_root + os.sep):
        raise HTTPException(400, "Invalid output_dir")

    conn = orchestrator.init_db()
    owner_row = db.execute(conn, "SELECT user_id FROM runs WHERE output_dir = ?", (output_dir,)).fetchone()
    conn.close()
    if not owner_row or owner_row[0] != user_id:
        raise HTTPException(404, "No run found for this output_dir")  # 404, not 403 — don't confirm existence

    report_path = os.path.join(candidate, "report.html")
    if not os.path.isfile(report_path):
        raise HTTPException(404, "Report file not found for this run")
    return FileResponse(report_path, media_type="text/html")


# ---- Frontend ---------------------------------------------------------------

STATIC_DIR = os.path.join(SCRIPT_DIR, "static")


@app.get("/")
def serve_index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
