"""
AuditAgent — Orchestrator
Runs the full pipeline end-to-end from one command: crawl -> static
analysis -> merge -> Architect recommendations -> HTML report. This is
the direct answer to the original pitch ("I provide a URL and a GitHub
repo") — every prior stage worked, but required 5-6 separate manual
commands. This ties them together.

Design choice: this shells out to each stage's own CLI via subprocess,
rather than importing and calling their functions directly. Reasons:
  - crawler.py's core is async (asyncio.run) while everything else is
    sync — mixing them in-process is more fragile than just running each
    stage as its own process, the same way you'd run them by hand.
  - Each stage's intermediate output (report.json, endpoints.json, etc.)
    stays inspectable on disk, exactly like manual runs — good for
    debugging when something in a 6-stage pipeline doesn't look right.
  - Loose coupling: any single stage can be swapped or upgraded without
    the orchestrator needing to change, as long as its CLI contract holds.

Also adds a SQLite-backed run history (auditagent_history.db) — a real
database instead of one-off JSON files per run, per the project's own
"v3+ ideas" list. Tracks every run's URL, repo, timestamp, and headline
findings, and can answer "what have I tested most" / "show recent runs."

Usage:
    python orchestrator.py https://example.com
    python orchestrator.py https://example.com --repo https://github.com/user/repo
    python orchestrator.py https://example.com --repo https://github.com/user/repo --api-base-url https://api.example.com
    python orchestrator.py history
    python orchestrator.py history --most-tested
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ---- History -----------------------------------------------------------

def init_db():
    conn = db.get_connection()
    db.execute(conn, f"""
        CREATE TABLE IF NOT EXISTS runs (
            id {db.AUTOINCREMENT_PK},
            user_id INTEGER,
            url TEXT NOT NULL,
            repo TEXT,
            started_at TEXT NOT NULL,
            duration_s REAL,
            output_dir TEXT,
            pages_crawled INTEGER,
            top_bottleneck_ms REAL,
            never_observed_count INTEGER,
            concurrency_concerning INTEGER,
            success INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn


def record_run(conn, **fields):
    columns = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    db.execute(conn, f"INSERT INTO runs ({columns}) VALUES ({placeholders})", list(fields.values()))
    conn.commit()


def print_history(conn, most_tested: bool = False, limit: int = 15):
    if most_tested:
        rows = db.execute(conn, """
            SELECT url, COUNT(*) as runs, MAX(started_at) as last_run
            FROM runs GROUP BY url ORDER BY runs DESC LIMIT ?
        """, (limit,)).fetchall()
        print(f"\n{'='*70}\nMost-tested URLs\n{'='*70}")
        if not rows:
            print("No runs recorded yet.")
        for url, count, last_run in rows:
            print(f"  {count:>3}x  {url}   (last: {last_run})")
    else:
        rows = db.execute(conn, """
            SELECT url, started_at, duration_s, top_bottleneck_ms, never_observed_count,
                   concurrency_concerning, success
            FROM runs ORDER BY started_at DESC LIMIT ?
        """, (limit,)).fetchall()
        print(f"\n{'='*70}\nRecent runs\n{'='*70}")
        if not rows:
            print("No runs recorded yet.")
        for url, started_at, duration, bottleneck_ms, never_obs, concerning, success in rows:
            status = "✅" if success else "❌"
            flags = []
            if concerning:
                flags.append("⚠️ concurrency")
            if never_obs:
                flags.append(f"{never_obs} unreached endpoints")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            print(f"  {status} {started_at}  {url}  ({duration:.1f}s, bottleneck: {bottleneck_ms or '—'}ms){flag_str}")


# ---- Pipeline stages ---------------------------------------------------------

def run_step(description: str, cmd: list[str]) -> bool:
    print(f"\n{'─'*60}\n▶ {description}\n{'─'*60}")
    result = subprocess.run(cmd, cwd=SCRIPT_DIR if False else None)
    if result.returncode != 0:
        print(f"✗ Step failed: {description}")
        return False
    return True


def detect_api_base_url(report_path: str, crawled_url: str) -> str | None:
    """Scans the crawl's passively-observed XHR/fetch calls for a
    different origin than the site itself — this is the exact 'my
    frontend and backend are on different domains, and I have to go hunt
    down the backend's URL myself' pain point, except we already have
    the data to answer it automatically: crawler.py records every API
    call a page makes while loading, including its full URL. If most of
    those calls go to one consistent other domain, that's almost
    certainly the real API host — no manual detective work needed.

    Deliberately conservative: only returns a value if there's a clear,
    consistent majority (not just one stray cross-origin request), and
    the caller is expected to report this as a detected/confirmed value,
    not apply it silently without saying so."""
    try:
        with open(report_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    crawled_netloc = urlparse(crawled_url).netloc
    origin_counts: dict[str, int] = {}
    for page in data.get("pages", []):
        for call in page.get("api_calls", []):
            call_netloc = urlparse(call.get("url", "")).netloc
            if call_netloc and call_netloc != crawled_netloc:
                origin_counts[call_netloc] = origin_counts.get(call_netloc, 0) + 1

    if not origin_counts:
        return None

    best_netloc, best_count = max(origin_counts.items(), key=lambda kv: kv[1])
    # Require it to be a clear majority, not just one stray call among many
    total = sum(origin_counts.values())
    if best_count / total < 0.5:
        return None

    scheme = urlparse(crawled_url).scheme or "https"
    return f"{scheme}://{best_netloc}"


RUN_RETENTION_DAYS = int(os.environ.get("AUDITAGENT_RUN_RETENTION_DAYS", "14"))


def cleanup_old_runs():
    """Deletes run output directories (report.json, merged.json,
    graph.png, report.html — the files backing shared report links and
    PDF downloads) older than RUN_RETENTION_DAYS. Nothing previously
    cleaned these up at all — they'd accumulate forever on local disk,
    a real problem on a space-constrained deployment. Runs on a cheap
    timer (once per pipeline execution, not a fixed background job) so
    this needs no extra infrastructure — the DB row stays (small, cheap)
    so history/stats still show the run happened; only the on-disk files
    get pruned."""
    conn = init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RUN_RETENTION_DAYS)).isoformat()
    old_rows = db.execute(
        conn, "SELECT output_dir FROM runs WHERE started_at < ? AND output_dir IS NOT NULL", (cutoff,)
    ).fetchall()
    conn.close()

    deleted = 0
    for (output_dir,) in old_rows:
        full_path = os.path.join(SCRIPT_DIR, output_dir) if output_dir else None
        if full_path and os.path.isdir(full_path):
            shutil.rmtree(full_path, ignore_errors=True)
            deleted += 1
    if deleted:
        print(f"🧹 Cleaned up {deleted} run director{'y' if deleted == 1 else 'ies'} older than {RUN_RETENTION_DAYS} days")


def run_pipeline(
    url: str,
    repo: str | None,
    api_base_url: str | None,
    provider: str,
    concurrency: int,
    max_pages: int,
    skip_report: bool,
    on_progress=None,
    probe_concurrency: bool = True,
    user_id: int | None = None,
    patient: bool = False,
) -> dict:
    """on_progress, if given, is called with a short step-name string as
    each stage starts — this is how api.py drives live progress in the
    frontend without needing to parse subprocess stdout.

    probe_concurrency: whether the crawler's concurrency-degradation probe
    runs. api.py sets this to False for unverified domains — the probe
    deliberately sends concurrent load, which is a legitimate diagnostic
    against your own site but indistinguishable from abuse against
    someone else's, so it's gated behind proven domain ownership when
    this tool is running for people other than just the operator.

    patient: waits up to 90s per request instead of the default 15s —
    for apps on free-tier hosting (Render, Vercel, etc.) that cold-start.
    Explicitly opt-in (never silently applied), since it makes a genuinely
    slow/broken page take much longer to actually report as such — the
    whole audit will simply take longer with this on, which is the
    tradeoff for getting a real number instead of a premature timeout."""
    def progress(step: str):
        if on_progress:
            on_progress(step)
    conn = init_db()
    cleanup_old_runs()
    started = time.time()
    started_at = datetime.now(timezone.utc).isoformat()

    domain = urlparse(url).netloc.replace(":", "_")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = os.path.join("runs", f"{domain}_{run_id}")
    os.makedirs(output_dir, exist_ok=True)
    print(f"Output directory: {output_dir}")

    report_path = os.path.join(output_dir, "report.json")
    endpoints_path = os.path.join(output_dir, "endpoints.json")
    merged_path = os.path.join(output_dir, "merged.json")
    graph_path = os.path.join(output_dir, "graph.png")
    recommendations_path = os.path.join(output_dir, "recommendations.md")
    report_html_path = os.path.join(output_dir, "report.html")

    success = False
    summary = {}

    try:
        # 1. Crawl
        progress("crawling")
        crawl_cmd = [
            sys.executable, os.path.join(SCRIPT_DIR, "crawler.py"), url,
            "--output", report_path, "--concurrency", str(concurrency), "--max-pages", str(max_pages),
        ]
        if not probe_concurrency:
            crawl_cmd.append("--no-probe")
        if patient:
            crawl_cmd.append("--patient")
        if not run_step("Crawling live app", crawl_cmd):
            return {"success": False, "output_dir": output_dir}

        # 2. Static analysis (optional — only if a repo was given)
        if repo:
            progress("analyzing_repo")
            static_cmd = [
                sys.executable, os.path.join(SCRIPT_DIR, "static_analysis.py"), repo,
                "--output", endpoints_path,
            ]
            if not run_step("Analyzing repo source code", static_cmd):
                return {"success": False, "output_dir": output_dir}
        else:
            # Always give merge.py a valid (empty) endpoints file so the
            # merged.json shape stays consistent whether or not a repo was
            # provided — report_generator.py expects one consistent shape.
            with open(endpoints_path, "w") as f:
                json.dump({"endpoints": []}, f)

        # 3. Merge + graph
        progress("building_graph")
        detected_api_base_url = None
        api_base_url_source = "manual" if api_base_url else "same_domain_fallback"
        if not api_base_url:
            detected_api_base_url = detect_api_base_url(report_path, url)
            if detected_api_base_url:
                print(f"🔍 Detected API base URL from observed calls: {detected_api_base_url}")
                api_base_url = detected_api_base_url
                api_base_url_source = "detected"
            else:
                # This is the transparency gap that mattered: detection
                # running and finding nothing looks IDENTICAL from the
                # outside to never running at all, unless we say so
                # explicitly. A shallow, unauthenticated crawl often just
                # never observes enough cross-origin traffic to detect a
                # split deployment — silently falling back to same-domain
                # in that case is indistinguishable from "detection says
                # same-domain is correct," which is a very different,
                # much more confident claim.
                print("🔍 Auto-detection found no clear cross-origin API pattern in this crawl — "
                      "assuming same-domain. If your API is actually on a different host, set "
                      "it manually in Advanced options; a shallow, unauthenticated crawl may "
                      "simply not have observed enough real API traffic to detect it.")
        merge_cmd = [
            sys.executable, os.path.join(SCRIPT_DIR, "merge.py"), report_path, endpoints_path,
            "--output", merged_path, "--visualize", graph_path,
        ]
        if api_base_url:
            merge_cmd += ["--api-base-url", api_base_url]
        if not run_step("Building dependency graph", merge_cmd):
            return {"success": False, "output_dir": output_dir}

        # 4. Architect recommendations
        progress("generating_recommendations")
        architect_cmd = [
            sys.executable, os.path.join(SCRIPT_DIR, "architect.py"), merged_path,
            "--provider", provider, "--output", recommendations_path,
        ]
        architect_ok = run_step("Generating scalability recommendations", architect_cmd)
        if not architect_ok:
            print("  (continuing without recommendations — check your API key/provider setup)")

        # 5. Report
        if not skip_report:
            progress("generating_report")
            report_cmd = [
                sys.executable, os.path.join(SCRIPT_DIR, "report_generator.py"), report_path,
                "--merged", merged_path, "--graph", graph_path, "--output", report_html_path,
            ]
            if architect_ok and os.path.isfile(recommendations_path):
                report_cmd += ["--recommendations", recommendations_path]
            run_step("Generating final HTML report", report_cmd)

        success = True
        progress("complete")

    finally:
        duration = time.time() - started

        # Pull headline numbers for the history DB and final summary.
        crawl_data = json.load(open(report_path)) if os.path.isfile(report_path) else {}
        merged_data = json.load(open(merged_path)) if os.path.isfile(merged_path) else {}
        bottlenecks = merged_data.get("metrics", {}).get("bottlenecks", [])
        never_observed = merged_data.get("merge_summary", {}).get("never_observed", [])
        probe = crawl_data.get("concurrency_probe") or {}

        summary = {
            "output_dir": output_dir,
            "report_html": report_html_path if os.path.isfile(report_html_path) else None,
            "pages_crawled": crawl_data.get("pages_visited"),
            "top_bottleneck_ms": bottlenecks[0]["response_time_ms"] if bottlenecks else None,
            "never_observed_count": len(never_observed),
            "concurrency_concerning": bool(probe.get("concerning")),
            "probe_skipped_unverified": not probe_concurrency,
            "detected_api_base_url": detected_api_base_url,
            "api_base_url_source": api_base_url_source,
            "success": success,
            "duration_s": round(duration, 1),
        }

        record_run(
            conn,
            user_id=user_id,
            url=url, repo=repo, started_at=started_at, duration_s=summary["duration_s"],
            output_dir=output_dir, pages_crawled=summary["pages_crawled"],
            top_bottleneck_ms=summary["top_bottleneck_ms"],
            never_observed_count=summary["never_observed_count"],
            concurrency_concerning=int(summary["concurrency_concerning"]),
            success=int(success),
        )
        conn.close()

    return summary


def print_final_summary(summary: dict):
    print(f"\n{'='*60}")
    print("AuditAgent — Pipeline Complete" if summary["success"] else "AuditAgent — Pipeline Failed")
    print(f"{'='*60}")
    print(f"Duration: {summary.get('duration_s')}s")
    print(f"Output directory: {summary['output_dir']}")
    if summary.get("pages_crawled") is not None:
        print(f"Pages crawled: {summary['pages_crawled']}")
    if summary.get("top_bottleneck_ms"):
        print(f"Top bottleneck: {summary['top_bottleneck_ms']}ms")
    if summary.get("never_observed_count"):
        print(f"Endpoints never observed live: {summary['never_observed_count']}")
    if summary.get("concurrency_concerning"):
        print("⚠️  Concurrency degradation detected — see report for details")
    if summary.get("report_html"):
        print(f"\n📄 Full report: {summary['report_html']}")
        print("   Host this somewhere reachable, then shorten it with the shortener service.")


def main():
    parser = argparse.ArgumentParser(description="AuditAgent — run the full pipeline end-to-end")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run the full pipeline (default if no subcommand given)")
    run_parser.add_argument("url", help="Live URL of the deployed app")
    run_parser.add_argument("--repo", type=str, default=None, help="GitHub repo URL (optional — enables static analysis + endpoint-gap findings)")
    run_parser.add_argument("--api-base-url", type=str, default=None, help="Base URL where the API actually lives, if different from --url")
    run_parser.add_argument("--provider", type=str, default="gemini", choices=["anthropic", "gemini"])
    run_parser.add_argument("--concurrency", type=int, default=5)
    run_parser.add_argument("--max-pages", type=int, default=25)
    run_parser.add_argument("--skip-report", action="store_true", help="Skip HTML report generation")

    history_parser = subparsers.add_parser("history", help="Show past runs")
    history_parser.add_argument("--most-tested", action="store_true", help="Show most-tested URLs instead of recent runs")
    history_parser.add_argument("--limit", type=int, default=15)

    # Allow `python orchestrator.py <url> ...` without requiring the `run` subcommand.
    if len(sys.argv) > 1 and sys.argv[1] not in ("run", "history", "-h", "--help"):
        sys.argv.insert(1, "run")

    args = parser.parse_args()

    if args.command == "history":
        conn = init_db()
        print_history(conn, most_tested=args.most_tested, limit=args.limit)
        conn.close()
        return

    if args.command != "run":
        parser.print_help()
        return

    summary = run_pipeline(
        url=args.url, repo=args.repo, api_base_url=args.api_base_url, provider=args.provider,
        concurrency=args.concurrency, max_pages=args.max_pages, skip_report=args.skip_report,
    )
    print_final_summary(summary)


if __name__ == "__main__":
    main()
