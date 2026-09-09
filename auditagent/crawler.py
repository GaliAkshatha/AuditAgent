"""
AuditAgent v0/v3 — Tester Agent
Crawls a deployed app starting from a URL, checks every page it finds on the
same domain, and reports: broken links, error status codes, slow pages,
console errors, and basic page info.

This version does NOT call an LLM — it's pure deterministic crawling +
checking. That's intentional: per the project plan, the crawl/check layer
stays code-only, and the LLM only gets involved later (Architect agent) to
interpret the structured output this script produces.

v3 change: the crawl loop is now async, with a worker pool pulling from a
shared queue — pages are fetched concurrently instead of one at a time.
This is the single biggest lever for crawl speed on any real app with more
than a handful of pages. Broken-link checking is similarly parallelized
with a thread pool, since it was the other fully-sequential phase.

Usage:
    python crawler.py https://example.com
    python crawler.py https://example.com --max-pages 30 --max-depth 3
    python crawler.py https://example.com --concurrency 8
    python crawler.py https://example.com --output report.json
"""

import argparse
import asyncio
import json
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Query params that don't change page content — stripped during normalization
# so tracking links don't create duplicate crawl targets.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref",
}


@dataclass
class PageResult:
    url: str
    status_code: int | None = None
    response_time_ms: float | None = None
    title: str | None = None
    console_errors: list[str] = field(default_factory=list)
    links_found: list[str] = field(default_factory=list)
    api_calls: list[dict] = field(default_factory=list)
    error: str | None = None  # e.g. "timeout", "dns_failure"


@dataclass
class LinkCheckResult:
    url: str
    found_on: str
    status_code: int | None
    ok: bool


@dataclass
class ConcurrencyProbeResult:
    url: str
    sequential_avg_ms: float
    concurrent_avg_ms: float
    degradation_ratio: float  # concurrent_avg / sequential_avg — >1 means it got slower under load
    concerning: bool          # ratio above DEGRADATION_THRESHOLD
    sample_size: int


@dataclass
class CrawlReport:
    start_url: str
    pages: list[PageResult] = field(default_factory=list)
    broken_links: list[LinkCheckResult] = field(default_factory=list)
    slow_pages: list[dict] = field(default_factory=list)
    concurrency_probe: ConcurrencyProbeResult | None = None
    crawl_duration_s: float = 0.0
    pages_visited: int = 0


SLOW_THRESHOLD_MS = 1500  # flag pages slower than this
DEGRADATION_THRESHOLD = 1.5  # concurrent/sequential ratio above this = "concerning"


def same_domain(url: str, root_domain: str) -> bool:
    try:
        return urlparse(url).netloc == root_domain
    except Exception:
        return False


def normalize_url(base: str, link: str) -> str | None:
    """Resolve a possibly-relative link against base, strip fragments,
    drop tracking query params, and collapse trailing-slash variants — so
    example.com, example.com/, and example.com/page/ all normalize the same
    way instead of being treated as different pages (root URL with no path
    at all vs. root path "/" was previously counted as two separate pages)."""
    if not link or link.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    resolved = urljoin(base, link)
    parsed = urlparse(resolved)
    if parsed.scheme not in ("http", "https"):
        return None

    kept_params = [(k, v) for k, v in parse_qsl(parsed.query) if k not in TRACKING_PARAMS]
    query = urlencode(kept_params)

    path = parsed.path
    if path == "":
        path = "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    return urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))


def get_robots_parser(start_url: str) -> urllib.robotparser.RobotFileParser | None:
    """Fetch and parse robots.txt for the target domain. Returns None if it
    can't be fetched (fail-open — we still crawl, just without robots checks)."""
    parsed = urlparse(start_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
        return rp
    except Exception:
        return None


async def attempt_page(context, url: str, timeout_ms: int, max_retries: int) -> PageResult:
    """Load a single page, retrying transient failures once (or `max_retries`
    times) with a short backoff before giving up and marking it as errored.
    A one-off flaky timeout no longer nukes that page's data entirely.

    Also passively observes every XHR/fetch call the page makes on its own
    while loading (method, URL, status, timing) — this is how we discover
    real backend API endpoints (`calls` edges for the future dependency
    graph) without ever triggering anything ourselves. Purely observational:
    we never submit forms or click anything, so this stays zero-risk."""
    result = PageResult(url=url)

    attempt = 0
    while attempt <= max_retries:
        attempt += 1
        page = await context.new_page()
        console_errors: list[str] = []
        api_calls: list[dict] = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        async def handle_request_finished(request):
            if request.resource_type in ("xhr", "fetch"):
                try:
                    response = await request.response()
                    status = response.status if response else None
                except Exception:
                    status = None
                api_calls.append({
                    "method": request.method,
                    "url": request.url,
                    "status_code": status,
                    "resource_type": request.resource_type,
                })

        page.on("requestfinished", lambda request: asyncio.create_task(handle_request_finished(request)))

        t0 = time.time()
        try:
            response = await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            result.response_time_ms = round((time.time() - t0) * 1000, 1)
            result.status_code = response.status if response else None
            result.title = await page.title()
            result.error = None

            hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
            links = []
            seen_links = set()
            for href in hrefs:
                normalized = normalize_url(url, href)
                if normalized and normalized not in seen_links:
                    seen_links.add(normalized)
                    links.append(normalized)
            result.links_found = links

            # requestfinished handlers fire async and may not all have
            # completed by the moment goto() resolves — give any in-flight
            # ones a brief moment to land before we snapshot the list.
            await asyncio.sleep(0.05)
            result.console_errors = console_errors
            result.api_calls = api_calls

            await page.close()
            return result  # success — no need to retry

        except PlaywrightTimeoutError:
            result.error = "timeout"
        except Exception as e:
            result.error = f"error: {e}"
        finally:
            await page.close()

        if attempt <= max_retries:
            await asyncio.sleep(1.0 * attempt)  # small backoff before retrying

    return result  # exhausted retries, return whatever error state we ended on


async def run_concurrency_probe(context, url: str, samples: int, timeout_ms: int) -> ConcurrencyProbeResult | None:
    """A small, controlled experiment: fetch the same page `samples` times
    sequentially (one at a time, small gap between), then fetch it
    `samples` times concurrently (all at once), and compare average
    response time.

    This exists because "the crawler is faster with more concurrency" is
    NOT universally true — it depends entirely on whether the target's
    backend can actually handle concurrent load (connection pooling,
    non-blocking I/O, enough server capacity) or falls over under it
    (single shared DB connection, serverless cold-start contention, a
    proxy that throttles concurrent connections per IP). Found this by
    hand testing against a real app before automating it here: a real run
    against suzume.akshathag.in was measurably SLOWER at concurrency=8
    (13.88s) than concurrency=1 (3.76s) for the exact same 4-page crawl —
    a real, useful finding about that app's backend, not a crawler bug.

    Kept intentionally small (default 4+4=8 extra requests to one page)
    to stay a lightweight check, not a load test — this is meant to
    surface a red flag, not stress-test the target."""
    if samples < 2:
        return None

    async def fetch_once() -> float | None:
        page = await context.new_page()
        # Cache-bust with a unique query param per request — without this,
        # the browser's HTTP cache can serve repeat navigations to the same
        # URL instantly regardless of real server-side latency, which
        # silently defeats the entire point of this probe. Caught this by
        # testing against a server with deliberately simulated contention
        # (verified via direct curl timing to show real 250ms->1s queueing)
        # and seeing the probe report near-instant, uniform times that
        # didn't match reality at all.
        cache_bust_url = url + ("&" if "?" in url else "?") + f"_auditagent_probe={time.time_ns()}"
        t0 = time.time()
        try:
            await page.goto(cache_bust_url, timeout=timeout_ms, wait_until="domcontentloaded")
            return (time.time() - t0) * 1000
        except Exception:
            return None
        finally:
            await page.close()

    # Sequential phase — one request at a time, small gap so they genuinely
    # don't overlap.
    sequential_times: list[float] = []
    for _ in range(samples):
        t = await fetch_once()
        if t is not None:
            sequential_times.append(t)
        await asyncio.sleep(0.2)

    # Concurrent phase — fire all requests at once.
    concurrent_results = await asyncio.gather(*[fetch_once() for _ in range(samples)])
    concurrent_times = [t for t in concurrent_results if t is not None]

    if len(sequential_times) < 2 or len(concurrent_times) < 2:
        return None  # too many failures to draw a reliable conclusion

    seq_avg = sum(sequential_times) / len(sequential_times)
    conc_avg = sum(concurrent_times) / len(concurrent_times)
    ratio = round(conc_avg / seq_avg, 2) if seq_avg > 0 else 1.0

    return ConcurrencyProbeResult(
        url=url,
        sequential_avg_ms=round(seq_avg, 1),
        concurrent_avg_ms=round(conc_avg, 1),
        degradation_ratio=ratio,
        concerning=ratio > DEGRADATION_THRESHOLD,
        sample_size=samples,
    )


async def _crawl_worker(
    worker_id: int,
    queue: asyncio.Queue,
    context,
    visited: set[str],
    all_discovered_links: dict[str, str],
    report_pages: list,
    robots,
    root_domain: str,
    max_pages: int,
    max_depth: int,
    timeout_ms: int,
    max_retries: int,
    delay_s: float,
) -> None:
    """One worker in the crawl pool: pulls (url, depth) off the shared
    queue, fetches it, and enqueues any newly-discovered same-domain links.
    Multiple workers run concurrently — this is what actually parallelizes
    the crawl versus the old one-page-at-a-time loop."""
    while True:
        url, depth = await queue.get()
        try:
            # Check-then-add on `visited` has no `await` between the check
            # and the add below, so it's safe without an explicit lock —
            # asyncio only switches tasks at await points.
            if url in visited or depth > max_depth or len(visited) >= max_pages:
                continue
            if robots is not None and not robots.can_fetch("*", url):
                continue
            visited.add(url)

            if delay_s > 0:
                await asyncio.sleep(delay_s)

            result = await attempt_page(context, url, timeout_ms, max_retries)
            report_pages.append(result)

            for link in result.links_found:
                all_discovered_links.setdefault(link, url)
                if same_domain(link, root_domain) and link not in visited and len(visited) < max_pages:
                    await queue.put((link, depth + 1))
        finally:
            queue.task_done()


async def crawl(
    start_url: str,
    max_pages: int = 25,
    max_depth: int = 3,
    timeout_ms: int = 15000,
    delay_s: float = 0.3,
    max_retries: int = 1,
    respect_robots: bool = True,
    concurrency: int = 5,
    probe_concurrency: bool = True,
    probe_samples: int = 4,
) -> CrawlReport:
    root_domain = urlparse(start_url).netloc
    report = CrawlReport(start_url=start_url)

    robots = get_robots_parser(start_url) if respect_robots else None

    visited: set[str] = set()
    all_discovered_links: dict[str, str] = {}
    report_pages: list[PageResult] = []

    started = time.time()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put((normalize_url(start_url, start_url), 0))

        workers = [
            asyncio.create_task(_crawl_worker(
                i, queue, context, visited, all_discovered_links, report_pages,
                robots, root_domain, max_pages, max_depth, timeout_ms, max_retries, delay_s,
            ))
            for i in range(concurrency)
        ]

        await queue.join()  # blocks until every enqueued (url, depth) has been processed

        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        if probe_concurrency:
            probe_url = normalize_url(start_url, start_url)
            report.concurrency_probe = await run_concurrency_probe(
                context, probe_url, samples=probe_samples, timeout_ms=timeout_ms
            )

        await browser.close()

    report.pages = report_pages
    report.slow_pages = [
        {"url": p.url, "response_time_ms": p.response_time_ms}
        for p in report_pages if p.response_time_ms and p.response_time_ms > SLOW_THRESHOLD_MS
    ]

    # Check off-site / not-yet-visited links for broken status (parallelized
    # across a thread pool — this was the other fully-sequential phase).
    report.broken_links = check_links(all_discovered_links, visited)

    report.crawl_duration_s = round(time.time() - started, 2)
    report.pages_visited = len(visited)
    return report


def check_links(all_links: dict[str, str], already_crawled: set[str]) -> list[LinkCheckResult]:
    """Check status of links that weren't already crawled as full pages
    (external links, or same-domain links beyond max_pages/max_depth).
    Parallelized across a thread pool — this was the other fully-sequential
    phase, and with many broken/external links it added up."""
    import requests

    def check_one(item: tuple[str, str]) -> LinkCheckResult | None:
        url, found_on = item
        try:
            resp = requests.head(url, timeout=8, allow_redirects=True)
            status = resp.status_code
            if status >= 400:
                resp = requests.get(url, timeout=8, allow_redirects=True)
                status = resp.status_code
        except Exception:
            status = None
        ok = status is not None and status < 400
        return None if ok else LinkCheckResult(url=url, found_on=found_on, status_code=status, ok=ok)

    to_check = [(url, found_on) for url, found_on in all_links.items() if url not in already_crawled]
    if not to_check:
        return []

    results: list[LinkCheckResult] = []
    with ThreadPoolExecutor(max_workers=min(10, len(to_check))) as executor:
        for result in executor.map(check_one, to_check):
            if result is not None:
                results.append(result)
    return results


def print_summary(report: CrawlReport) -> None:
    print(f"\n{'='*60}")
    print(f"AuditAgent — Tester Report for {report.start_url}")
    print(f"{'='*60}")
    print(f"Pages crawled: {report.pages_visited}  |  Duration: {report.crawl_duration_s}s\n")

    error_pages = [p for p in report.pages if p.error or (p.status_code and p.status_code >= 400)]
    if error_pages:
        print(f"⚠ Pages with errors ({len(error_pages)}):")
        for p in error_pages:
            reason = p.error or f"HTTP {p.status_code}"
            print(f"  - {p.url}  →  {reason}")
        print()

    console_err_pages = [p for p in report.pages if p.console_errors]
    if console_err_pages:
        print(f"⚠ Pages with console errors ({len(console_err_pages)}):")
        for p in console_err_pages:
            print(f"  - {p.url}  ({len(p.console_errors)} error(s))")
            for e in p.console_errors[:3]:
                print(f"      {e[:120]}")
        print()

    if report.slow_pages:
        print(f"🐢 Slow pages (> {SLOW_THRESHOLD_MS}ms) ({len(report.slow_pages)}):")
        for sp in sorted(report.slow_pages, key=lambda x: -x["response_time_ms"]):
            print(f"  - {sp['url']}  →  {sp['response_time_ms']}ms")
        print()

    all_api_calls = [(p.url, call) for p in report.pages for call in p.api_calls]
    if all_api_calls:
        print(f"🔌 API calls observed ({len(all_api_calls)}):")
        for page_url, call in all_api_calls:
            print(f"  - {call['method']} {call['url']}  →  {call['status_code']}  (from {page_url})")
        print()

    if report.broken_links:
        print(f"🔗 Broken links ({len(report.broken_links)}):")
        for bl in report.broken_links:
            print(f"  - {bl.url}  (found on {bl.found_on})  →  status {bl.status_code}")
        print()

    if not error_pages and not console_err_pages and not report.slow_pages and not report.broken_links:
        print("✅ No issues found in this pass.\n")

    if report.concurrency_probe:
        p = report.concurrency_probe
        if p.concerning:
            print(f"⚠️  CONCURRENCY DEGRADATION DETECTED on {p.url}:")
            print(f"    Sequential avg: {p.sequential_avg_ms}ms  |  Concurrent avg: {p.concurrent_avg_ms}ms  "
                  f"|  {p.degradation_ratio}x slower under concurrent load")
            print(f"    This app's backend appears to get WORSE under concurrent requests, not just linearly")
            print(f"    slower. Common causes: no DB connection pooling, serverless cold-start contention,")
            print(f"    or a proxy throttling concurrent connections per IP. Worth investigating directly.\n")
        else:
            print(f"✅ Concurrency probe: {p.url} handled concurrent load fine "
                  f"({p.sequential_avg_ms}ms sequential vs {p.concurrent_avg_ms}ms concurrent, "
                  f"{p.degradation_ratio}x)\n")


def main():
    parser = argparse.ArgumentParser(description="AuditAgent v0 — crawl and test a deployed app")
    parser.add_argument("url", help="Starting URL of the deployed app")
    parser.add_argument("--max-pages", type=int, default=25, help="Max pages to crawl (default: 25)")
    parser.add_argument("--max-depth", type=int, default=3, help="Max link depth from start URL (default: 3)")
    parser.add_argument("--output", type=str, default=None, help="Write JSON report to this file")
    parser.add_argument("--delay", type=float, default=0.3, help="Seconds each worker waits between its own requests (default: 0.3, politeness delay)")
    parser.add_argument("--retries", type=int, default=1, help="Retries per page on transient failure (default: 1)")
    parser.add_argument("--ignore-robots", action="store_true", help="Skip robots.txt checks (off by default — robots.txt is respected)")
    parser.add_argument("--concurrency", type=int, default=5, help="Number of pages to crawl in parallel (default: 5). Higher = faster but more load on the target server.")
    parser.add_argument("--no-probe", action="store_true", help="Skip the concurrency-sensitivity probe (on by default — a small controlled check of whether the target degrades under concurrent load)")
    parser.add_argument("--probe-samples", type=int, default=4, help="Number of requests per phase (sequential/concurrent) in the concurrency probe (default: 4)")
    parser.add_argument("--patient", action="store_true",
                         help="Wait up to 90s per request instead of the default 15s — for apps on "
                              "free-tier hosting (Render, Vercel, etc.) that cold-start and can genuinely "
                              "take a long time to wake up. Off by default since it makes a slow/broken "
                              "page take much longer to actually report as such; explicitly opt in when "
                              "you suspect cold start, not as a default safety margin.")
    args = parser.parse_args()

    report = asyncio.run(crawl(
        args.url,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        delay_s=args.delay,
        # Patient mode means ONE full-length wait per page, then move on —
        # not multiple 90s attempts compounding into minutes per page.
        # Retrying at 90s each defeats the actual goal here (a bounded,
        # predictable wait), so patient mode overrides --retries to 0
        # regardless of what was passed.
        max_retries=0 if args.patient else args.retries,
        respect_robots=not args.ignore_robots,
        concurrency=args.concurrency,
        probe_concurrency=not args.no_probe,
        probe_samples=args.probe_samples,
        timeout_ms=90000 if args.patient else 15000,
    ))
    print_summary(report)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(asdict(report), f, indent=2, default=str)
        print(f"Full JSON report written to {args.output}")


if __name__ == "__main__":
    main()
