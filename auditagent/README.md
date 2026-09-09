# AuditAgent — v0 (Tester Agent)

This is step 1 of the build plan: a crawler that visits a deployed app,
follows same-domain links, and reports what's broken — no LLM involved yet.
It's pure deterministic code, which is intentional (see project plan §5):
the crawl/check layer stays code-only so later agent layers get clean,
structured input instead of raw noise.

## What it checks per page
- HTTP status code
- Response time (flags anything over 1.5s as slow)
- Page title
- Browser console errors
- All links found on the page (feeds the crawl queue + link checker)

## What it checks across the whole crawl
- Broken links (any link — internal or external — that returns 4xx/5xx or fails to resolve)
- Slow pages
- Pages with console errors
- Pages that errored out entirely (timeout, DNS failure, etc.)

## Setup

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium     # downloads the headless browser Playwright drives
```

## Usage

```bash
python crawler.py https://example.com
```

Options:
```bash
python crawler.py https://example.com --max-pages 30 --max-depth 3 --output report.json
```

- `--max-pages` — cap on how many pages to visit (default 25)
- `--max-depth` — how many link-hops from the start URL to follow (default 3)
- `--output` — also write the full structured report as JSON (this JSON is what
  the future Architect agent and Dependency Graph Engine will consume)

## Example output

```
============================================================
AuditAgent — Tester Report for https://example.com
============================================================
Pages crawled: 12  |  Duration: 8.43s

⚠ Pages with errors (1):
  - https://example.com/old-page  →  HTTP 404

🐢 Slow pages (> 1500ms) (2):
  - https://example.com/dashboard  →  2340.5ms
  - https://example.com/reports    →  1820.1ms

🔗 Broken links (1):
  - https://example.com/deleted-resource  (found on https://example.com/home)  →  status 404
```

## Resolved since first draft

- **Duplicate URLs** (trailing slash, tracking params like `utm_source`) now
  collapse to one crawl target instead of being visited multiple times.
- **Flaky timeouts** no longer kill a page's data permanently — each page
  gets `--retries` attempts (default 1 retry) with a short backoff before
  being marked as errored.
- **Politeness delay** (`--delay`, default 0.5s) between requests — avoids
  hammering someone's server or looking like abusive/DoS traffic.
- **robots.txt is respected by default** — disallowed paths are skipped.
  Use `--ignore-robots` to turn this off if you're auditing your own app and
  want full coverage regardless of robots rules.

## Known limitations still open

- **No auth support yet** — pages behind login won't be reachable. Natural
  v0.5 addition: accept cookies/headers, or a scripted login step.
- **No JS-rendered link discovery beyond `domcontentloaded`** — for heavy
  client-side-rendered apps, links added after initial render won't be seen.
  Fix is switching `wait_until` to `"networkidle"`, but that's slower, so
  it should become a flag rather than the default.
- **Only follows `<a href>` links** — doesn't click buttons or submit forms.
  This is intentionally conservative for now: the moment this crawler starts
  *interacting* with the app (clicking things) rather than just reading it,
  it risks triggering real actions (deletes, payments, logouts). Before that
  expansion, it needs an allowlist/blocklist for interactive elements and
  should default to staging environments only — not something to skip.
- **Sequential crawling** — one page at a time. This is deliberately left
  for v2.5 (per the project plan) once there's a working baseline to
  optimize; parallelizing now would add complexity before it's needed.
- **No screenshots on error** — would help a lot with the report; planned
  for v1 alongside the dependency graph work.
- **Doesn't build the dependency graph yet** — that's v1. Right now this
  script collects the raw data (pages, links, timings, errors) that the
  graph engine will consume next.

## Next steps (per the build plan)

1. **v0.5** — structure this output more richly and render it as a report page (not just terminal + JSON).
2. **v1** — feed `links_found` + timing/error data per page into the Dependency Graph Engine (NetworkX) to compute centrality, bottlenecks, cycles.
3. **v1.5** — Architect agent reasons over the graph + this report.

---

# v1 — Dependency Graph Engine (`graph_engine.py`)

Takes the JSON report from `crawler.py` and builds an actual graph of the
app using NetworkX: pages and API endpoints as nodes, navigation and API
calls as edges. Then computes the metrics that make the graph useful
instead of just a picture.

## What it computes

- **Betweenness centrality** — which pages/endpoints sit on the most
  traversal paths through the app. These are your single-point-of-failure
  candidates: if one goes down, the most other stuff breaks.
- **Bottlenecks** — nodes that are both high-centrality *and* slow
  (> 1.5s). This is the ranked priority list for what to fix first.
- **API call cycles** — circular API calls (A calls B calls A). A real bug
  smell and a retry-storm risk at scale. Note: ordinary page navigation
  cycles (login ↔ home ↔ register) are *not* flagged as issues — that's
  completely normal site structure, just reported as a count.
- **Broken link nodes** — surfaced from the graph directly, so you can run
  impact analysis on them.
- **Impact / blast-radius analysis** — given a node, what else in the app
  depends on it (directly or transitively)? Answers "what breaks if this
  fails."

## Usage

```bash
python graph_engine.py report.json
```

Options:
```bash
python graph_engine.py report.json --output graph_report.json
python graph_engine.py report.json --visualize graph.png
python graph_engine.py report.json --impact "https://example.com/login"
```

- `--output` — write full computed metrics as JSON (this is what the
  Architect agent will consume next — structured, not raw graph data)
- `--visualize` — save a PNG of the graph, color-coded by node type (pages
  blue, API endpoints green, broken links red, uncrawled/external gray) and
  sized by centrality, so bottlenecks are visually obvious
- `--impact <url>` — run blast-radius analysis on a specific node

## Design notes

- Graph construction and all metrics here are **pure code, no LLM** — this
  is deliberate. The Architect agent (next milestone) only ever receives
  the *already-computed* results (top bottlenecks, cycles, centrality
  ranking) as structured input. Never make an LLM do graph traversal itself.
- Cycle detection is split by edge type on purpose. Running it on the whole
  graph produced 8 "cycles" on a 4-page test site — all of them just normal
  back-and-forth navigation links, zero signal. Only cycles within `calls`
  edges (API-to-API) are real findings.
- Node IDs for API endpoints include the HTTP method (`GET /api/users` and
  `DELETE /api/users` are different nodes) — they can have very different
  blast radii.

## Next steps

- **v1.5** — Static Analysis Agent: clone the GitHub repo, parse routing
  files, feed every defined endpoint into this same graph tagged
  `source: repo_defined`, so endpoints that exist in code but were never
  reached by the crawl show up as findings too.
- **v2** — Architect agent reasons over this graph's output + a system-design
  RAG corpus to produce scalability recommendations.

---

# v1.5 — Static Analysis Agent (`static_analysis.py`)

Clones a GitHub repo (or reads a local path) and extracts every route the
source code actually defines — regardless of whether the live crawler ever
reached it. This is what makes "every endpoint" true: a crawler only sees
what's linked from the UI; this reads the routing code directly.

## Supported frameworks (auto-detected)

- **Express / Node.js** — `app.get/post/put/delete/patch(...)`, `router.*`
- **Flask** — `@app.route(...)`, `@app.get/post/etc(...)`
- **FastAPI** — `@app.get/post/put/delete/patch(...)`
- **Django** — `path()` / `re_path()` in `urls.py`
- **Next.js** — App Router (`app/**/route.ts`) and Pages Router
  (`pages/api/**/*.ts`), path derived from file structure

Detection uses both dependency manifests (`package.json`,
`requirements.txt`, etc.) **and** a fallback scan of actual import
statements in the source — manifests alone aren't reliable (e.g. a
`requirements.txt` that just does `-r requirements/base.txt` won't mention
Flask by name at all, but the code still imports it).

## Usage

```bash
python static_analysis.py https://github.com/user/repo
python static_analysis.py /local/path/to/repo
python static_analysis.py https://github.com/user/repo --output endpoints.json
```

Validated against real repos during development:
- An Express boilerplate app → correctly found 9 auth endpoints
  (register/login/logout/etc.) with correct methods and line numbers.
- A production-style Flask app (miguelgrinberg/flasky) → correctly found 50
  endpoints across auth flows and a REST API, including multi-method routes
  (`GET`+`POST` on the same path) and Flask URL converters (`<int:id>`).

## Resolved since first draft

- **Mount-prefix resolution now traces the FULL chain, not just one hop.**
  First version only resolved a router's prefix if it was imported and
  mounted directly in the same file. Real apps commonly nest through an
  aggregator (`app.ts` mounts `routes/index.ts` at `/api`, which itself
  mounts `auth.routes.ts` at `/auth` — the real live path is
  `/api/auth/login`, not `/auth/login`). Cloning a real repo and tracing
  the actual mount chain surfaced this; the resolver now walks the whole
  chain recursively.
- **Combined `import X, { Y } from '...'` syntax is now matched.** The
  original import regex only handled a plain `import X from '...'` —
  the combined default+named form (common when a router file also exports
  a secondary router, e.g. `import experienceRoutes, { experienceByRoundRouter }
  from '...'`) was invisible to it entirely, silently dropping those
  routers from resolution. Found via testing against a real repo where two
  routers used exactly this pattern.
- Together these took a real repo from 3 false "confirmed live" matches
  (actually two routers colliding on identical unresolved bare paths) down
  to 44/44 correctly resolved, zero collisions.

## Known limitations

- **Regex/heuristic-based, not a full AST parse.** Covers common,
  idiomatic route-definition patterns per framework. Won't catch routes
  built dynamically in a loop, heavily macro'd includes, or unusual
  registration patterns. A proper AST parser per language is a natural
  upgrade once this proves the concept out — not needed for v1.5.
- **Test and example directories are skipped by default**
  (`tests/`, `test/`, `__tests__/`, `spec/`, `examples/`, `docs/`) — test
  files often define route-like fixtures (e.g. Flask's own `test_json.py`
  defines `@app.route('/json')` for testing purposes) that would otherwise
  show up as false-positive "endpoints." This was caught during testing:
  scanning Flask's own repo without this filter found 318 "endpoints,"
  almost all noise from its test suite; with it, that dropped to 7 (its
  own internal docstring examples — expected, since that repo is the
  framework itself, not an app built on it).
- **Django routes are method-agnostic** (`ANY`) since Django doesn't bind
  an HTTP method at the URL-pattern level — the view function decides.
- **Mount-prefix resolution is still heuristic**, not a full module
  resolver — ambiguous multi-mount routers resolve to the first match
  found, and prefixes assembled dynamically (built from a variable at
  runtime rather than a literal string) won't be caught.
- Requires `git` installed and available on PATH for cloning remote repos.

## Next steps

- **v1.5 (continued)** — merge these endpoints into the same dependency
  graph from `graph_engine.py`, tagged `source: repo_defined`, and
  cross-reference against `source: crawl_observed` nodes. An endpoint that
  exists in code but was never hit by the crawl is either dead code or an
  undocumented/untested route — worth flagging either way.
- **v2** — Architect agent reasons over the merged graph + a system-design
  RAG corpus to produce scalability recommendations.

---

# v1.5 (continued) — Merge (`merge.py`)

Combines a crawl report (`crawler.py`) and a repo's endpoint list
(`static_analysis.py`) into one graph, tagging every node:

- `crawl_observed` — reachable and actually seen live
- `repo_defined` — exists in code, never observed live
- `both` — confirmed in code AND observed live

## Usage

```bash
python merge.py report.json endpoints.json
python merge.py report.json endpoints.json --api-base-url https://api.example.com
python merge.py report.json endpoints.json --output merged_graph.json --visualize merged.png
```

`--api-base-url` matters a lot: without it, the tool assumes your API
lives on the same domain as the crawled frontend, which is often wrong
(subdomain, different port, monorepo split). Pass it explicitly whenever
you know it — the tool tells you clearly when it's guessing.

## Validated against a real project

Ran this against a real app (crawl: 4 pages, 0 API calls observed live;
repo: 44 endpoints via Express). Result: **0 confirmed live, 41 defined-but-
never-observed.** This is the exact scenario the two-source architecture
exists for — the app's real backend (auth flows, a REST API for companies/
preparation/experiences/etc.) is completely invisible to a crawl-only audit
because it's behind auth or only triggered by form submission. A tool that
only crawled would have wrongly concluded "this app barely has a backend."

## A real bug this caught, and its fix

The first version of this script reported "3 confirmed live" for that same
run — which was wrong, since the crawl found zero API calls. Root cause:
two Express routers (`experiences`, `learnings`) both had unresolved mount
prefixes, so they fell back to bare paths like `GET /` and `GET /:id` and
collided *with each other* during the merge — not with anything the
crawler saw. Fixed the immediate symptom by snapshotting which nodes were
crawl-observed *before* adding any repo endpoints.

The root cause itself (unresolved mount prefixes) was fixed properly since
— see the "resolved" note in `static_analysis.py`'s section below.
Cloning the real repo and tracing the actual code showed the prefixes were
nested two levels deep through an aggregator file
(`app.ts` → `/api` → `routes/index.ts` → `/auth` → `auth.routes.ts`), and
two routers used a combined-import syntax (`import X, { Y } from '...'`)
the resolver's regex didn't match at all. Both are fixed now — a
re-run against the real repo produced 44 endpoints, 44 unique, zero
collisions, all correctly prefixed with `/api/...`.

## Known limitations

- **API base URL is a guess by default.** Same-domain assumption is wrong
  for a lot of real deployments. Always pass `--api-base-url` when you know
  where the API actually lives, for a trustworthy cross-reference.
- **Mount-prefix resolution is still heuristic** (see `static_analysis.py`
  below) — much more robust now, but still not a full module resolver.

## Next steps

- **v2** — Architect agent reasons over this merged graph (both the crawl
  data — timings, bottlenecks, centrality — and the "defined but never
  observed" set) plus a system-design RAG corpus, to produce concrete
  scalability recommendations.

---

# v2 — Architect Agent (`architect.py`, `corpus.py`, `retrieval.py`)

Takes `merge.py`'s output and turns structural findings into actual,
prioritized scalability recommendations — grounded in a small hand-curated
pattern corpus, not generic advice. This is the **only** component in the
pipeline that calls an LLM; everything upstream (crawler, graph engine,
static analysis, merge) is pure code specifically so this step only ever
receives small, pre-digested findings instead of raw data.

## Setup

Two provider options — pick one (you don't need both):

**Option A — Anthropic (Claude):** best output quality, but only a
one-time ~$5 trial credit for new accounts, no ongoing free tier.
```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...        # macOS/Linux
$env:ANTHROPIC_API_KEY="sk-ant-..."        # Windows PowerShell
```

**Option B — Gemini (Google AI Studio):** genuinely free, no card
required, and the request quota **resets daily** rather than being a
one-time trial — better for repeated testing while developing this.
```bash
pip install google-genai
export GEMINI_API_KEY=...                  # macOS/Linux — get a free key at aistudio.google.com/apikey
$env:GEMINI_API_KEY="..."                  # Windows PowerShell
```

## Usage

```bash
python architect.py merged_report.json --output recommendations.md
python architect.py merged_report.json --provider gemini --output recommendations.md
```

Always try `--dry-run` first — it builds and prints the exact prompt
without calling the API, so you can sanity-check what's being sent (and
its rough token cost) before spending anything:

```bash
python architect.py merged_report.json --dry-run
```

## How it works

1. **`build_findings`** extracts a handful of structured findings from the
   merged graph — top bottlenecks, highest-centrality nodes, API-call
   cycles, and the "defined but never observed" endpoint set. This is
   deliberately small: on a 45-node graph, this produced only 3 findings,
   not 45.
2. **`retrieval.py`** (pure Python TF-IDF, no embedding API call) retrieves
   the 2 most relevant patterns per finding from `corpus.py`'s ~20
   hand-curated system-design patterns.
3. **`architect.py`** builds one compact prompt (findings + retrieved
   patterns only — tested at ~500 words for 3 findings) and sends it to
   Claude, which produces prioritized, specific recommendations.

## Validated (up to the API call boundary)

Tested end-to-end against the real merged data from this project:
- Findings correctly extracted: the 5.4s homepage bottleneck, login's
  0.42 betweenness centrality (highest in the app), and the 41
  defined-but-never-observed endpoints.
- Retrieval correctly surfaced "Cache high-traffic read endpoints" and
  "Reduce single points of failure" for the bottleneck/centrality
  findings, and "Investigate endpoints defined but never observed live"
  for the endpoint-gap finding — all with clear score margins over
  irrelevant patterns.
- Error handling verified for both missing-package and missing-API-key
  cases — clean, actionable messages rather than stack traces.

The actual LLM call itself needs a real Anthropic API key to test, which
this development environment doesn't have — run it locally with your key
to see the final recommendations.

## Design notes

- **Why TF-IDF instead of an embedding API:** the corpus is only ~20
  patterns. Plain term-frequency retrieval works fine at this scale and
  needs no network call, no API key, no extra cost — an embedding-based
  RAG (or GraphRAG) is a reasonable v3+ upgrade once the corpus grows
  large enough that concept relationships start to matter for retrieval
  quality.
- **Why findings are pre-digested, not raw graph data:** keeps the prompt
  small (a few hundred words, not a full graph dump) and keeps the LLM
  from having to do graph reasoning itself — that stays deterministic code
  upstream, per the project's core design rule.

## Next steps

- **v2.5** — Your own URL shortener (Go) for sharing the final report.
- **v3** — Caching, model tiering, parallel crawling — now that a full
  pipeline exists end-to-end, this is where to optimize.
- **v3.5** — GraphRAG upgrade once the pattern corpus grows past hand-curated size.
- **v4** — Chatbot layer on top of the existing graph/RAG/report infra.

---

# v2.5 — URL Shortener (`shortener/`, Go)

A small, dependency-free HTTP service for turning AuditAgent report links
into short, shareable URLs (Slack, PR descriptions, client emails) — the
"deliverable moment" of the whole pipeline per the project plan.

Written in Go on purpose: this is a narrow, high-throughput, I/O-bound
redirect service — exactly Go's sweet spot — and it compiles to a single
static binary with zero runtime dependencies. Deliberately standard-library
only (no external Go modules), which also means no `go mod` fetching
required to build it.

## Build & run

```bash
cd shortener
go build -o shortener .
./shortener --port 8080
```

Flags:
- `--port` — port to listen on (default `8080`)
- `--base-url` — base URL used when constructing short links, e.g.
  `https://short.yoursite.com` (defaults to `http://localhost:<port>`)
- `--store` — path to the JSON file used for persistence (default
  `shortener_data.json`)

## API

```bash
# Shorten a URL
curl -X POST http://localhost:8080/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://your-report-host.com/reports/run-42"}'
# -> {"short_code":"3","short_url":"http://localhost:8080/3","long_url":"..."}

# Use it — redirects (302) to the original URL
curl -i http://localhost:8080/3

# Check click stats (does NOT itself count as a click)
curl http://localhost:8080/stats/3
# -> {"long_url":"...","created_at":"...","clicks":1}

curl http://localhost:8080/health
```

## Design notes

- **Storage:** an in-memory map guarded by a `sync.RWMutex`, persisted to a
  JSON file on every write (atomic rename, so a crash mid-write can't
  corrupt the file). This is intentionally simple for personal-project
  scale. The project plan's real scaling path — Postgres for the mapping
  table, Redis in front for hot-link lookups — is a drop-in replacement
  for the `Store` type's internals; the HTTP layer wouldn't need to change.
- **Same URL shortened twice returns the same code**, not a new one —
  avoids link sprawl if the same report gets shortened multiple times.
- **Short codes use a base62 alphabet that skips ambiguous characters**
  (no `0`/`O`, `1`/`l`/`I`) since these are meant to be read and typed by
  humans, not just clicked.
- **`/stats` is read-only** — checking analytics doesn't itself inflate
  the click count, verified explicitly during testing (called it twice in
  a row, count didn't change).

## Validated

Built and ran the actual binary, then tested against a live local server
(not just compiled — actually exercised):
- Health check, shorten, redirect (real 302 with correct `Location`
  header), click counting (incremented correctly across 3 real requests),
  `/stats` confirmed non-mutating, 404 for unknown codes, and input
  validation (rejects non-URLs) — all passed.
- **Deduplication confirmed:** shortening the same URL twice returned the
  identical code both times.
- **Restart persistence confirmed:** killed the running server, restarted
  it pointed at the same store file, and both the click count (3) and the
  internal counter (so the next code wouldn't collide with an existing
  one) loaded correctly from disk.

## Known limitations

- Single-node in-memory store — fine for personal use, not for
  multi-instance deployment (two instances wouldn't share state without
  the Postgres/Redis upgrade mentioned above).
- No auth on the `/shorten` endpoint — anyone who can reach the service
  can create short links. Fine for local/personal use behind your own
  network; add an API key check before exposing this publicly.
- No expiry/cleanup for old links — everything persists forever. A TTL or
  manual cleanup endpoint would be a reasonable v3 addition if this sees
  real usage.

## Next steps

- **v3** — Caching, model tiering, parallel crawling on the Python side —
  now that a full pipeline exists end-to-end, this is where to optimize.
- **v3.5** — GraphRAG upgrade once the pattern corpus grows past
  hand-curated size.
- **v4** — Chatbot layer on top of the existing graph/RAG/report infra.

---

# v3 — Parallel Crawling (`crawler.py`, updated)

The crawler is now async, with a worker pool pulling from a shared queue —
pages are fetched concurrently instead of one at a time. This is the
single biggest lever for crawl speed on any real app with more than a
handful of pages.

## What changed

- Rewritten on `playwright.async_api` instead of `sync_api`.
- New `--concurrency` flag (default 5) — number of pages crawled in
  parallel.
- `check_links` (broken-link checking) is now parallelized across a thread
  pool too — it was the other fully-sequential phase.
- Same JSON output shape as before — `graph_engine.py`, `merge.py`, etc.
  all still work unchanged against the new crawler's output.

## Usage

```bash
python crawler.py https://example.com --concurrency 8
```

**Higher concurrency is NOT universally faster — this is a real, corrected
claim, not a hedge.** It helps on most sites, but it actively hurt on the
real app this was built against: a live run on `suzume.akshathag.in` at
`--concurrency 8` took **13.88s**, while the identical crawl at
`--concurrency 1` took **3.76s** — concurrency made it **3.7x slower**,
not faster. That result is why the automated concurrency probe below
exists: whether concurrency helps or hurts depends entirely on whether
the target's backend can actually handle concurrent requests (connection
pooling, non-blocking I/O) or falls over under them (single shared DB
connection, serverless cold-start contention, per-IP connection
throttling). Start at a low concurrency (2-3) for an unfamiliar target,
check what the probe below reports, and only raise it if the probe
doesn't flag a problem.

## Validated — a real, measured speedup (on a backend that can take it)

Built a local test server with realistic artificial latency (200-500ms
per response, meant to approximate a real backend rather than
near-instant local disk reads) and crawled the same 20-page site twice:

- **concurrency=1** (sequential, old behavior): **11.1s**
- **concurrency=8**: **4.43s**
- **≈2.5x speedup**, both runs correctly crawled all 20 pages

Note: an early test against near-zero-latency local pages showed almost
no speedup at all — that turned out to be because Playwright/Chromium's
own fixed per-page overhead dominates when there's no real network wait
to overlap. Concurrency's value shows up on realistic sites with real
latency AND a backend that can actually serve concurrent requests — see
the concurrency probe section below for what happens when that second
condition doesn't hold.

Also re-verified full downstream compatibility: the new crawler's JSON
output was fed directly into `graph_engine.py` with zero changes needed
there — the output shape didn't change, only how it's produced.

## Design notes

- Safe to run lock-free on the shared `visited` set — asyncio only
  switches between tasks at `await` points, so the check-then-add on
  `visited` (no `await` in between) can't race.
- Uses the standard `asyncio.Queue` + `queue.join()` pattern for BFS with
  an unknown-in-advance amount of work: workers keep pulling and
  discovering new links until the queue is fully drained, then get
  cancelled.
- `requestfinished` handlers for passive API-call observation are
  dispatched via `asyncio.create_task` since Playwright's async event
  callbacks can't themselves be `async def` directly — a small
  `asyncio.sleep(0.05)` gives any in-flight handler a moment to land
  before the result is finalized.

## Known limitations

- Concurrency is shared across one browser context — very high
  concurrency (e.g. 20+) may hit diminishing returns from
  Playwright/Chromium-side contention rather than continuing to scale
  linearly. 5-10 is a reasonable range for most sites.
- No per-host rate limiting beyond the simple `--delay` — if you crawl
  multiple different domains in future versions, this would need to
  become per-host rather than global.

---

# v3 (continued) — Concurrency-Sensitivity Probe

After the real `suzume.akshathag.in` run above showed concurrency making
the crawl *slower*, this became an automated check rather than something
you have to notice by hand. Runs by default after every crawl (add
`--no-probe` to skip it).

## How it works

A small, controlled experiment against a single page (the start URL):
1. Fetch it `--probe-samples` times (default 4) **sequentially**, one at a
   time, recording the average response time.
2. Fetch it the same number of times **concurrently**, all at once,
   recording the average.
3. Compare the two. If the concurrent average is more than 1.5x the
   sequential average, it's flagged as `concerning`.

This is deliberately small — 8 total extra requests to one page by
default, a red-flag check, not a load test.

## Usage

```bash
python crawler.py https://example.com                      # probe runs automatically
python crawler.py https://example.com --no-probe            # skip it
python crawler.py https://example.com --probe-samples 6     # more samples, steadier average
```

The result feeds all the way through the pipeline: `crawler.py` →
`merge.py` (passes it through into the merged report) → `architect.py`
(becomes an actual finding, retrieving the "database connection pooling"
pattern from the corpus and producing a real recommendation grounded in
the measured numbers) — not just something printed to the terminal and
forgotten.

## Validated — and a real bug caught along the way

First implementation tested against two local servers: one simulating a
single shared DB connection (requests queue up behind each other — real
degradation), one with independent per-request latency and no shared
resource (should NOT be flagged). The first test run reported **both as
fine** — a false negative on the server that should have been flagged.

Root cause: the probe fetches the *same URL* multiple times in one
browser context, and Chromium's HTTP cache was serving requests 2-4 from
cache after the first one — completely bypassing the server, regardless
of its real backend behavior. Verified independently with direct `curl`
timing that the "bad" server genuinely did degrade as designed (0.25s →
0.5s → 0.75s → 1.0s under real concurrent load) — the browser-based probe
just wasn't seeing it.

Fixed by appending a unique cache-busting query parameter to every probe
request. Re-tested against both servers:
- **Well-behaved server:** 277ms sequential vs. 285ms concurrent (1.03x) — correctly NOT flagged.
- **Poorly-behaved server:** 279ms sequential vs. 596ms concurrent (2.14x) — correctly flagged.

## Known limitations

- Only probes the start URL, not every page — a reasonable proxy (usually
  the highest-traffic page) but a different page could behave differently.
- Small sample size (4 by default) trades precision for speed/politeness —
  `--probe-samples` can be raised for a steadier average at the cost of
  more requests to the target.
- A single noisy network blip during either phase could produce a
  misleading ratio — this is a lightweight signal to investigate further,
  not a certified benchmark.

## Next steps

- **v3.5** — GraphRAG upgrade once the pattern corpus grows past
  hand-curated size.
- **v4** — Chatbot layer on top of the existing graph/RAG/report infra.

---

# v4 — Chatbot (`chatbot.py`, `chat_context.py`, `llm_provider.py`)

A conversational interface over the actual dependency graph, not just the
static report. Ask things like "what breaks if login fails?" and it runs
real impact analysis on the graph to answer — this is the payoff of
having built the graph, RAG corpus, and report data as reusable
infrastructure rather than a one-shot report generator.

## Setup

Same API key setup as `architect.py` (Anthropic or Gemini — see that
section above). No new credentials needed.

## Usage

```bash
# Interactive
python chatbot.py report.json --endpoints endpoints.json --api-base-url https://api.example.com

# One-shot (no REPL, useful for scripting)
python chatbot.py report.json --question "what breaks if login fails?"

# Gemini instead of Anthropic
python chatbot.py report.json --provider gemini
```

`--endpoints` and `--api-base-url` are optional — without them you get a
chatbot over just the crawl data (no merged repo endpoints), which still
answers plenty of questions (bottlenecks, centrality, concurrency probe).

## How it works

- **`chat_context.py`** rebuilds the actual graph (reusing `graph_engine.py`
  and `merge.py` — same code, not a reimplementation) and exposes 7 tools:
  `impact_analysis`, `get_bottlenecks`, `get_top_central_nodes`,
  `search_endpoints`, `get_concurrency_probe`, `get_never_observed_endpoints`,
  `search_patterns`.
- **`chatbot.py`** runs a portable ReAct-style loop: the model responds
  with either `TOOL: name / ARGS: {...}` or `ANSWER: ...`, parsed and
  executed in a loop (capped at 5 iterations to prevent a confused model
  looping forever). This format works identically against both Anthropic
  and Gemini, since their *native* tool-calling APIs differ enough that
  supporting both natively would mean real code duplication for this
  project's scope.
- **`llm_provider.py`** is a small shared multi-turn chat abstraction —
  separate from `architect.py`'s single-turn `call_architect_model`, since
  the chatbot needs to carry conversation history back and forth across
  tool calls.

## Validated (up to the real-API-call boundary, same as architect.py)

No API key available in this development environment, so the actual
model's responses couldn't be tested — but everything up to that boundary
was tested against real reconstructed data from this project:

- **All 7 tools tested directly**, not just through the chat loop: fuzzy
  node matching (`"login"` correctly resolves to the full node ID),
  impact analysis (correctly found the 3 pages depending on login),
  bottleneck/centrality lookups, endpoint search (correctly returned the
  real `/api/auth/login` endpoint with its `repo_defined` source tag),
  concurrency probe pass-through, never-observed-endpoint lookup, and
  pattern retrieval — all correct against real data shapes from this
  project's actual runs.
- **ReAct response parsing tested** against a real tool-call format, a
  final-answer format, messy whitespace, and a malformed response that
  ignores the format entirely (falls back to treating the whole thing as
  an answer rather than erroring).
- **Full conversation loop tested with a mocked provider**: simulated a
  realistic exchange (model calls `impact_analysis`, gets a real result
  back, then gives a grounded final answer) — completed correctly in
  exactly 2 calls.
- **Infinite-loop safety cap tested**: a mocked provider that always calls
  a tool and never answers correctly stops after `MAX_TOOL_ITERATIONS`
  rather than hanging.
- **Missing-API-key error path tested** — clean message, not a stack trace.

## Known limitations

- ReAct-style prompting (vs. native function calling) is somewhat less
  reliable than a provider's purpose-built tool-calling API — the model
  could in principle ignore the format. The fallback (treat any
  unparseable response as a final answer) handles this without crashing,
  but a native tool-calling implementation per provider would be more
  robust if this becomes a heavily-used feature later.
- No conversation memory across separate `chatbot.py` invocations — each
  run/question starts fresh. Fine for the current REPL-per-session model,
  would need explicit history persistence to change.
- `impact_analysis`'s fuzzy substring matching on node names could match
  the wrong node on an app with many similarly-named routes — it returns
  the first match, not a ranked list.

## Next steps

This completes the pipeline as originally scoped: URL + repo in → crawl +
static analysis → merged graph → scalability recommendations → shareable
link → conversational Q&A over the results. Remaining ideas from the
original plan (GraphRAG once the pattern corpus grows, native per-provider
tool calling, multi-domain crawling) are optional refinements at this
point, not missing core functionality.

---

# Report Generator (`report_generator.py`)

Ties everything the pipeline produces into **one self-contained,
shareable HTML file** — the actual "deliverable" this project was pitched
around from the start ("a shareable, shortened report link"). This was
flagged as unfinished back at v0.5 ("structure this output more richly
and render it as a report page") and never actually got built until now —
every report before this was scattered across separate `report.json`,
`graph.png`, and `recommendations.md` files with nothing tying them
together, which meant the shortener never had a real report to point at.

## Usage

```bash
# Minimal — just the crawl data
python report_generator.py report.json --output report.html

# Full — everything the pipeline can produce
python report_generator.py report.json \
  --merged merged.json \
  --graph merged.png \
  --recommendations recommendations.md \
  --output report.html
```

All flags except the crawl report are optional — the report degrades
gracefully, showing only what it has data for (tested explicitly: a
report with zero optional flags still renders correctly, just without
the graph/recommendations sections and with "—" for graph node count).

## What's in it

- Header stats: pages crawled, crawl duration, graph node count, unreached endpoint count
- Findings cards, color-coded by severity: slow pages, concurrency degradation (if detected), bottlenecks, highest-centrality nodes, endpoints never observed live, broken links
- The dependency graph image (if provided) — **base64-embedded directly into the HTML**, so the single output file is genuinely standalone (no broken image links if you move it, email it, or hand its path to the shortener)
- The Architect agent's recommendations, rendered from markdown to proper HTML

## This is what the shortener should actually point at

```bash
python report_generator.py report.json --merged merged.json --graph merged.png --recommendations recommendations.md --output report.html
# host report.html somewhere reachable, then:
curl -X POST http://localhost:8080/shorten -H "Content-Type: application/json" -d '{"url": "https://your-host.com/report.html"}'
```

## Validated

Generated a full report using real reconstructed data from this project
(your actual homepage response time, the real concurrency probe numbers,
the real 44-endpoint gap, real Architect recommendations text), then
**actually rendered it in a real headless browser and screenshotted it**
to visually verify — not just checked that the HTML didn't error:

- Caught and fixed a real bug this way: the recommendations section
  initially showed "Scalability Recommendations" as a heading **twice** —
  once as this report's own section header, once from `architect.py`'s
  output file (which starts with its own `# Scalability Recommendations`
  line). Fixed by stripping a leading H1 from the markdown before
  rendering it. This is exactly the kind of visual bug that only shows up
  by actually looking at the rendered output, not by checking the HTML is
  well-formed.
- Verified the minimal-input case (crawl data only) degrades correctly —
  no graph section, no recommendations section, "—" for graph node count
  specifically, while everything crawl-derived (stats, slow pages,
  concurrency probe) still renders.

## Known limitations

- `markdown` package required for proper recommendations rendering; falls
  back to a `<pre>` block (readable but unstyled) if it's not installed,
  rather than failing outright.
- No pagination or collapsing for very long endpoint-gap lists — shows the
  first 10 with a "...and N more" note, which is fine for a report this
  size but would need real pagination for an app with hundreds of unreached
  endpoints.

---

# Orchestrator (`orchestrator.py`)

Runs the full pipeline end-to-end from one command — the direct answer to
the original pitch ("I provide a URL and a GitHub repo... it checks
everything"). Every stage worked before this, but required 5-6 separate
manual commands; this ties them together.

## Usage

```bash
# Full pipeline, URL + repo
python orchestrator.py https://example.com --repo https://github.com/user/repo --api-base-url https://api.example.com

# URL only (skips static analysis, still runs crawl + graph + recommendations + report)
python orchestrator.py https://example.com

# View run history
python orchestrator.py history
python orchestrator.py history --most-tested
```

Every run's output (crawl data, endpoints, merged graph, graph image,
recommendations, final report) goes into its own timestamped folder under
`runs/`, so nothing overwrites a previous run.

## Design choice: subprocess, not direct imports

Shells out to each stage's own CLI rather than importing and calling
functions in-process. Two real reasons: `crawler.py`'s core is async while
everything else is sync (mixing them in-process is more fragile than
running each as its own process), and every intermediate file stays on
disk and inspectable — exactly like running the stages by hand, which
matters a lot when debugging a 5-stage pipeline.

## Run history (SQLite, not one-off JSON files)

Every run gets logged to `auditagent_history.db` — URL, repo, timestamp,
duration, and headline findings (bottleneck ms, unreached endpoint count,
whether concurrency was concerning). This is a real, queryable database
rather than scattered JSON files, and it's what powers `history` and
`history --most-tested`.

## Graceful degradation

If a stage fails (e.g. `architect.py` fails because no API key is set),
the pipeline continues rather than aborting — you still get a report,
just without that stage's contribution. Verified directly: ran with no
`GEMINI_API_KEY` set, the Architect step failed cleanly, and the pipeline
still produced a complete HTML report without recommendations rather than
crashing.

## Validated

Ran the full pipeline against a real local test site end-to-end multiple
times, including:
- Confirmed all 4-5 stages run in sequence with correct data hand-off
  between them (same report.json → merged.json → report.html chain as
  manual runs).
- Confirmed graceful degradation when the Architect step fails.
- Ran 3 times against the same URL and confirmed the history database
  correctly shows all 3 runs individually (`history`) and correctly
  aggregates them (`history --most-tested` → "3x").

---

# Frontend (`api.py`, `static/`)

A real web UI — paste a URL and repo, click a button, watch it run, view
the report. This is the direct answer to the original pitch ("I provide
a URL and GitHub repo to it in the frontend"), and the last structural
gap versus that original vision.

## Setup

```bash
pip install fastapi uvicorn
uvicorn api:app --reload --port 8000
```

Open **http://localhost:8000** in a browser.

## Design

Extends the navy/blue/amber palette already established in
`report_generator.py` rather than inventing a disconnected new look —
the frontend and the reports it links to feel like one product, not two.
Sans-serif for UI text; monospace reserved for actual technical
values (URLs, node identifiers) since that's functionally what they are,
not a decorative choice. The 5-step pipeline progress uses numbered
markers because the pipeline genuinely is a fixed sequence — crawl,
analyze, merge, recommend, report, always in that order.

## Architecture

- **`api.py`** — FastAPI backend. Jobs run in a background thread (the
  pipeline is fundamentally blocking subprocess work, not a good fit for
  async), tracked in an in-memory dict for live polling. Completed-run
  history is read directly from `orchestrator.py`'s own SQLite database
  (`auditagent_history.db`) — no duplicate storage.
- **`static/index.html` / `style.css` / `app.js`** — plain HTML/CSS/JS,
  no build step, no Node.js required. Given how much friction PATH/
  environment setup has already caused in this project (Go, Playwright),
  keeping the frontend dependency-free was a deliberate practical choice,
  not just a simplicity default.
- **`orchestrator.py`** gained an `on_progress` callback parameter so the
  API can drive live step-by-step UI updates without needing to parse
  subprocess output.

## Validated — genuinely end-to-end, not mocked

Ran the actual server and hit it with real HTTP requests, then loaded the
real frontend in an actual browser and drove it through a real form
submission — not unit tests of isolated pieces:

- **API layer:** `POST /api/audits` → polled `GET /api/audits/{id}`
  through real status transitions (`crawling` → `building_graph` →
  `complete`) against a real local test site. Verified `GET
  /api/audits/{id}/report` returns the actual 94KB report HTML. Verified
  `GET /api/history` and `?most_tested=true` return correct, real
  aggregated data.
- **Frontend, in a real browser:** submitted the form, screenshotted
  mid-progress (correctly showed step 1 active), screenshotted on
  completion (all 5 steps correctly marked done, real stats shown,
  history table auto-refreshed with the new run).
- **Caught and fixed a real bug this way**: the step-completion
  checkmarks initially rendered as "✓1", "✓2" etc. instead of replacing
  the number — the CSS assumed a nested `<span>` inside the step-number
  element that didn't actually exist in the HTML, so the hide-the-number
  rule silently matched nothing. Only visible by actually looking at a
  screenshot, not by checking the code was well-formed. Fixed by properly
  nesting the number in its own span and correcting the CSS selector.
- **Verified the "View full report" link** actually resolves to a working
  report page end-to-end, not just that the button renders.

## Known limitations

- In-memory job tracking means jobs are lost on server restart — fine for
  local/personal use, would need a persistent job queue (Redis, or a
  jobs table in the existing SQLite DB) for anything longer-running.
- No auth on the API — anyone who can reach it can start an audit
  (including the concurrency probe hitting whatever URL they give it).
  Fine for local use behind your own machine; would need auth before
  exposing this publicly, same caution as the shortener's `/shorten`
  endpoint.
- Single job runs at a time in practice (one background thread per
  request, no queue/concurrency limit) — fine for personal use, would
  need a real task queue for multiple simultaneous audits.

---

# React Frontend (`frontend/`)

A genuinely interactive rebuild of the frontend, using React + Vite. Talks
to the exact same `api.py` backend — no backend changes needed beyond one
new endpoint (below). The plain HTML/JS version in `static/` still works
as a zero-build-step fallback; this is the richer, animated version.

## Setup

Requires Node.js (this needs `npm`, unlike everything else in this
project — check with `node --version`, get it from nodejs.org if missing).

```bash
cd frontend
npm install
npm run dev
```

Opens on `http://localhost:5173` by default. The FastAPI backend
(`uvicorn api:app --port 8000`) needs to be running separately — the React
app talks to it directly at `localhost:8000` (CORS is already open on the
backend for this).

## What's actually interactive here (not just decorative)

- **Live terminal-style log** — a real, readable transcript of what the
  pipeline is doing, appended as each stage actually starts (driven by the
  same `on_progress` callback added to `orchestrator.py`), not a canned
  animation.
- **Animated step timeline** — a connecting line between steps that fills
  in as they complete, with the active step pulsing. The step sequence is
  genuinely fixed (crawl → analyze → merge → recommend → report, always in
  that order), so numbered markers are actually justified here, not a
  generic default.
- **Count-up stats** on the result card — pages crawled, bottleneck ms,
  and unreached-endpoint count animate up to their real values.
- **Clickable, searchable history** — click any past run (even from a
  previous server session — history persists in SQLite) to open its
  report directly. Added a new backend endpoint,
  `GET /api/history/report?output_dir=...`, for this specifically, since
  in-memory job IDs don't survive a server restart but the history table
  does.
- **Animated dependency-graph motif** in the header — a small SVG of
  pulsing, connected nodes. This is the actual visual language of the
  product (the same shape `graph_engine.py`'s `visualize()` produces),
  not generic decoration — it's meant to be the one signature visual
  moment, deliberately restrained everywhere else.

## Validated — real browser, real backend, not mocked

Ran `npm run build` to confirm a clean production build, then tested the
**dev server** against the real running FastAPI backend and a real local
test site, driven through an actual browser end-to-end:

- Submitted the form, confirmed the terminal log filled in correctly and
  all 5 steps animated to "done" with the connecting line.
- Confirmed the "View full report" link resolves to a real 200 response
  with the correct page title.
- Confirmed clicking a history table row opens the *new*
  `/api/history/report` endpoint with a correctly URL-encoded path, and
  that it actually serves the report.
- **Caught and fixed a real CSS bug this way**: the "Advanced options"
  collapse animation left a large empty gap when collapsed — the
  grid-collapse technique needs the *container* to clip overflow and
  exactly one grid child, but the advanced section had two direct
  children and only the children (not the container) had `overflow:
  hidden`. Fixed by wrapping both fields in one inner div and moving
  `overflow: hidden` onto the actual grid container. Only visible by
  actually looking at a screenshot, same as the earlier checkmark bug in
  the plain-JS version.

## Security note (backend addition)

The new `/api/history/report` endpoint takes a client-supplied
`output_dir` path — validated directly (tested with real traversal
attempts like `../../etc/passwd`) to ensure it resolves inside `runs/`
before serving anything, not just checked in passing.

## Known limitations

- Same in-memory job tracking / no-auth caveats as `api.py` in general
  (see the Frontend section above) — this is a richer UI over the same
  backend, so the same limitations apply.
- Requires Node.js/npm specifically for this piece — everything else in
  the project deliberately avoided that dependency (see the earlier
  design note on why `static/` was originally built dependency-free).
  Worth it here since interactivity was explicitly requested, but it's a
  real added setup step versus the rest of the project.

---

# Mascot, Health Score & Achievement Chips (React frontend addition)

The gaming/interactive layer requested on top of the React frontend —
a small crawler-bot companion ("Scout") and a gamified site-health score,
both genuinely driven by real pipeline data, not decoration bolted on top.

## What's real about it

- **Scout's mood is driven entirely by actual job state** — `scanning`
  while crawling/analyzing/merging, `thinking` during recommendation/report
  generation, `worried` the instant real findings land (concurrency issue,
  bottleneck, unreached endpoints), settling into `proud` after a beat, or
  `happy` if the run came back clean. `error` on a real pipeline failure.
  None of this is a canned animation loop — it's props derived from the
  same `job.status`/`job.step`/`job.summary` state everything else uses.
- **Health score is computed from real findings**, not a fake number:
  starts at 100, penalized for a concerning concurrency result, a slow
  bottleneck (scaled by how far over the 1500ms threshold), and unreached
  endpoints (capped). Deliberately simple and transparent — this is meant
  to be a fun, legible summary, not a scientific metric, and isn't
  presented as one.
- **Achievement-style chips** on the result card stagger in with a pop
  animation, each showing one real finding (pages crawled, bottleneck ms,
  unreached count, concurrency flag) — same data as before, presented with
  more visual weight and personality.

## Validated with real pipeline runs, both branches

Ran a real audit against a local test site and screenshotted every mood
transition as it actually happened:
- **Idle** → **scanning** (during the real crawl) → **worried**, with a
  "Found something…" speech bubble, at the exact moment a real
  concurrency-degradation finding landed → **proud** ("Got it all.") after
  a short beat.
- Confirmed both the `worried→proud` branch (findings present) and that
  `happy` shares the same rendering path (verified via the shared
  `eyes === "happy"` code path both moods use — not independently
  re-screenshotted since the render logic is identical, only the speech
  text differs).

## A real bug this caught

Checked the mobile viewport (380px) specifically, since the mascot is
fixed-positioned and could plausibly interact badly with narrow layouts.
Found a real, separate, pre-existing readability bug in the history
table: `word-break: break-all` combined with a fixed column width was
wrapping URLs one character per line (`htt` / `p://l` / `ocalh`...) —
nearly unreadable. Fixed with `overflow-wrap: anywhere` +
`word-break: break-word` (breaks at sensible points, not mid-character)
and a mobile-specific rule that drops the less-critical "Bottleneck"
column to give the URL room. Re-verified with a fresh screenshot.

## Known limitations

- Mascot is fixed-position and will sit above scrolled content by design
  (same pattern as a chat widget) — minor visual overlap with content
  directly behind it is expected, not a bug.
- Health score formula is a simple, transparent heuristic — don't read it
  as anything more rigorous than "a fun, at-a-glance summary of the real
  findings," which is what it's meant to be.

---

# Visual Polish Round 2: Scrollbars, Topbar, Chatbot Wiring, Dark Report

Four fixes/additions requested after seeing the app running for real.

## 1. Scrollbars fixed
Default browser scrollbars were rendering light/white in the terminal log
and horizontal pipeline flow, breaking the dark theme. Added global
custom scrollbar styling (thin, dark track, glowing cyan thumb) via both
`scrollbar-color` (Firefox) and `::-webkit-scrollbar` (Chrome/Safari/Edge).

## 2. Topbar enhanced
- Graph motif widened to span the full header (13 nodes, 16 edges, up
  from 7/8) instead of sitting in a small corner.
- Added a subtle animated "scan sweep" — a soft light band drifting
  across the header on a 6s loop.
- Added a **real** live-stat badge ("N audits run") in the header,
  sourced from a new `GET /api/stats` endpoint (`COUNT(*)` on the history
  table) — deliberately not reusing the paginated history list's length,
  since that's capped at 20 and would silently under-report once you'd
  run more audits than that.

## 3. Chatbot wired into the app (it existed, wasn't connected)
`chatbot.py` was a fully working, tested CLI tool that was never actually
reachable from the web app — a real gap. Fixed:
- **New backend endpoint**, `POST /api/chat` — takes `{output_dir,
  question, provider}`, rebuilds the real `AuditContext` from that run's
  `report.json`/`endpoints.json`, and calls `chatbot.py`'s existing
  `ask()` directly (no reimplementation). Same path-traversal validation
  pattern as `/api/history/report`, since this also takes a
  client-supplied path.
- **Scout (the mascot) is now clickable** — opens a small chat popup
  (`ChatWidget.jsx`) scoped to the most recently completed audit in the
  session. A small chat-bubble hint badge makes the click affordance
  discoverable.
- **Validated the full real chain through a browser**: clicked the
  mascot, chat opened correctly (detecting the completed job vs. an
  empty "run an audit first" state), sent a real question, and got back
  a properly-styled error bubble from the actual missing-API-key error
  path — proving the whole stack (click → popup → HTTP POST → real
  `AuditContext` construction → `chatbot.ask()` → error surfaced
  cleanly) works end to end. A real API key on your machine turns this
  into actual grounded answers instead of that error.

## 4. Report redesigned to match the dark theme
`report_generator.py`'s HTML was still using the old light navy/white
palette from before the visual direction changed — a real inconsistency
between the report and everything else. Rewrote its CSS to the same
tokens as the frontend (dark navy background, cyan/purple glow accents,
glassmorphic cards).

**Caught a real bug doing this properly** (rendered a real report in a
headless browser and looked at it, same as every other visual change in
this project): the embedded dependency-graph PNG kept its default
matplotlib white background, creating a jarring white rectangle in an
otherwise dark report. Fixed by giving `graph_engine.py`'s `visualize()`
function a dark background, light label text, and glow-matched node/edge
colors — the graph now visually blends into the report instead of
looking like a separate, mismatched artifact. Re-verified with another
screenshot after the fix.

---

# Domain Ownership Verification (`domain_verify.py`)

Since you're planning to deploy this for other people to use, letting
anyone submit any URL and have the tool immediately hammer it with a
deliberate concurrent-load probe is a real problem — that's
indistinguishable from a small DoS attempt against a site you don't
control. This gates that specific capability behind proof of ownership,
using the same standard, well-established patterns as Google Search
Console and Let's Encrypt — no email/SMTP infrastructure required.

## How it works

1. `POST /api/verify/start {domain}` — generates a random token, returns
   instructions for two proof methods.
2. The site owner proves control via **either**:
   - **Well-known file**: publish the token at
     `https://{domain}/.well-known/auditagent-verify.txt`
   - **DNS TXT record**: add a TXT record at `_auditagent.{domain}` with
     the token as its value
3. `POST /api/verify/check {domain}` — checks both methods (DNS via
   Cloudflare's DNS-over-HTTPS resolver, no `dnspython` dependency
   needed), marks verified if either passes. Verification expires after
   30 days.
4. `is_verified(domain)` is a fast, network-free check used to gate
   features — `orchestrator.py`'s `run_pipeline` now takes a
   `probe_concurrency` flag, and `api.py` sets it based on verification
   status before every run.

## What's gated vs. not

- **Passive crawling** (the default, unauthenticated crawl) is **not**
  gated — it's no more invasive than a normal search-engine crawler, and
  it already respects `robots.txt`.
- **The concurrency probe** (deliberate concurrent load, used to detect
  backend degradation under load) **is** gated — this is the part that
  can look like abuse without permission.

## Validated

- **Well-known file method fully tested** against a real local server:
  correct token → verified; wrong file content → correctly rejected, not
  a false positive; unstarted domain → correctly reports "not started."
- **DNS TXT method** is written using the standard, stable DNS-over-HTTPS
  JSON API (same format Cloudflare/Google have used for years), but
  **could not be tested from this development sandbox** — its network is
  restricted to an allowlist that doesn't include DNS-over-HTTPS
  resolvers. This needs verification on a real network (yours) before
  you fully trust it; the well-known-file method is confirmed working
  end-to-end and can be your primary/fallback method regardless.
- **Full gating loop tested through the real HTTP API**: ran an audit
  against an unverified domain → probe correctly skipped
  (`probe_skipped_unverified: true`). Verified the domain for real
  (placed the actual file, called the real check endpoint) → re-ran the
  same audit → probe correctly ran this time.
- **Full UI flow tested through a real browser**: typed a URL, saw the
  "not verified" state appear, clicked "Verify ownership," saw real
  instructions with a real token, placed the actual file, clicked
  "check now," watched it flip to the green "verified — deep scan
  enabled" state.

## On authenticated crawling (login credentials) — a deliberate design decision

You asked about checking "inside" the app via login. I'm **not** building
this as something people submit through your hosted web form, and this
is worth understanding why: having strangers type their own site's
username and password into a tool they don't operate is a real trust and
security liability for you as the operator — you'd become a target for
credential theft, and you'd be asking people to trust an unfamiliar tool
with live credentials. That risk is categorically different from "prove
you own this domain."

The responsible version of this feature: authenticated crawling should
be a **self-hosted, run-it-yourself capability** — for auditing your
*own* sites, where credentials live in your own `.env` file on your own
machine, never touching a request body or a database. This is buildable
(cookie-based session injection into the crawler is the safer mechanism
— avoids ever needing to know a real password, since you'd export a
session cookie from your own already-logged-in browser), but it's scoped
to local/personal use, not the shared multi-user deployment. Flagging
this now rather than building credential collection into the public
form; happy to build the self-host version (cookie-based auth in
`crawler.py`, env-var-only, no API exposure) as a follow-up if useful.

---

# Test Ledger & Rollback (`test_ledger.py`)

Records every mutating request made during *active* testing (as opposed
to today's passive crawling), and can undo them afterward — so testing a
real, demo-account-backed app doesn't leave garbage or damaged state
behind. This is what makes actively calling POST/PATCH/DELETE endpoints
during testing safe enough to consider, instead of either never touching
them or touching them with no safety net.

## Compensating-action rules

| Original action | Compensating action | Requires |
|---|---|---|
| POST (create) | DELETE the created resource | An id/location in the POST response |
| PATCH/PUT (update) | PATCH/PUT back to pre-mutation state | A GET captured **before** the mutation |
| DELETE | POST the pre-deletion state back | A GET captured **before** the delete |

Rollback runs in **reverse order** — last action undone first, same
principle as an undo stack.

## Validated against a real CRUD API, not synthetic data

Built a real FastAPI CRUD service, ran actual POST/PATCH/DELETE requests
against it, built a real ledger from the real responses, then executed
real rollback and checked the API's actual state before/after:

- **PATCH → revert**: confirmed the target's value returned to exactly
  its pre-mutation state.
- **POST → delete**: confirmed a created-then-left resource was correctly
  cleaned up.
- **DELETE → recreate**: confirmed the resource came back — with a
  **new id**, exactly the documented limitation (anything referencing the
  old id won't auto-repair).

## A real bug this caught, and the fix

First test run: create a resource, then delete that *same* resource
later in the same test run (a natural "test the full CRUD cycle"
pattern). Naive independent rollback failed a step — recreating the
deleted resource assigned it a new id, which made the original POST's
"delete this id" compensation stale by the time it ran (404). The
rollback report **correctly surfaced this as a failure** rather than
silently claiming success, which is the whole point of not trusting
rollback blindly — but the failure itself was fixable at the root.

Fixed by detecting **self-canceling pairs**: if a resource was created
and later deleted within the same ledger, both actions are skipped as a
matched pair (net effect on that resource was already zero) instead of
being compensated independently. Re-tested the same scenario — zero
failed rollbacks, final state exactly matched the original. Also
re-verified the fix didn't break the ordinary non-paired case.

## Honest limitations (read before relying on this)

- **New ids on recreation break references.** If resource B was created
  referencing resource A's id, and A gets deleted-then-recreated during
  rollback, B still points at A's old (now-gone) id. No ledger can fix
  this automatically — it would need to walk the actual relational
  structure of the target app, which isn't knowable generically.
- **Side effects with no data representation can't be undone.** An email
  sent, a webhook fired, a payment charged, a cache invalidated
  elsewhere — rollback only reverses what's visible through the API
  itself.
- **A POST with no id/location in its response can't be auto-compensated**
  — reported honestly as non-reversible, not silently skipped.
- **Rollback can itself partially fail** (network issue, the resource was
  independently modified by something else mid-test) — every rollback
  run returns a per-action report, not just a boolean success flag.

## Graph-based rollback ordering (upgrade from simple reversal)

You asked whether this uses graph concepts — the honest first answer was
"not yet, and here's why it might not even help": since an action can
only ever reference a resource created earlier in the same list, simple
reverse-chronological order is *already* a valid dependency-respecting
order for a normal, strictly sequential test run. Building a graph on
top of that would've been decoration.

Where it's genuinely needed: once a ledger's list order can't be trusted
to match true causal order — merged logs from parallel test workers,
reconstructed history — chronological reversal silently breaks, while
the actual dependency structure (which resource really references which)
doesn't. Built `build_dependency_graph()` (nodes = actions, edges =
"this action references a resource that action created", using NetworkX)
and `compute_rollback_order()` (topological sort of the reversed graph,
falling back to simple reversal if a cycle somehow appears in messy
real-world data).

**Two real bugs caught building this, not just one:**

1. **The first version was ordering-blind by construction.** It built
   the "who created this resource" lookup with a single forward pass over
   the list — silently assuming list order matched creation order, which
   is exactly the assumption this feature exists to NOT need. Tested with
   a deliberately shuffled ledger (comment-then-post in list order, even
   though the post was truly created first) and the dependency detection
   found **zero edges** — it silently fell back to wrong behavior instead
   of correct. Fixed by determining creation order from each action's
   actual recorded **timestamp**, not its position in the list.

2. **Proved the fix matters with a real failure, not a theoretical one.**
   Added a genuine foreign-key constraint to a test API ("can't delete a
   post that still has comments" — an extremely common real-world rule),
   then ran the same shuffled ledger through both approaches:
   - **Naive `reversed(list)`**: tried to delete the post before its
     comment → real **409 Conflict**, a genuine failure.
   - **Graph-based `rollback()`**: correctly deleted the comment first,
     then the post → **zero failures**, state exactly restored.

   Regression-tested the original simple linear scenario too, to confirm
   the graph-based version produces identical output there — this is a
   strict improvement, not a behavior change for the common case.

---

# Active Tester (`active_tester.py`) — the integration is built now

The active-testing crawler mode described as "not built yet" above is
now built: it discovers mutating endpoints from `static_analysis.py`'s
output, groups them by resource (collection endpoints like `POST
/tasks` vs. member endpoints like `GET/PATCH/DELETE /tasks/:id`), and
for each group with a creatable resource: creates one with a clearly
tagged test payload, exercises its GET/PATCH/DELETE endpoints with the
real created id substituted in, and records every mutation through
`test_ledger.py` — all opt-in, all gated the same way discussed above
(env-var-only credentials, meant for verified domains and demo/test
accounts, never exposed through the shared web app).

## Usage

```bash
# See what WOULD be tested, no requests made
python active_tester.py https://staging.example.com endpoints.json --dry-run

# Actually run it (set AUDIT_COOKIES in .env first if login is required)
python active_tester.py https://staging.example.com endpoints.json

# Review what a rollback would do (default, safe)
python active_tester.py --rollback active_test_ledger.json

# Actually execute the rollback
python active_tester.py --rollback active_test_ledger.json --execute-rollback
```

## Validated end-to-end against a real API, not synthetic data

Built a real FastAPI task service, ran the actual tool against it (not a
mock), and verified real outcomes at every step:

- **Endpoint grouping**: correctly grouped `GET/POST /tasks` with
  `GET/PATCH/DELETE /tasks/{task_id}` into one resource, using the exact
  JSON shape `static_analysis.py` produces.
- **Real HTTP execution**: POST created a real task (real `200`, real
  assigned id), GET verified it, PATCH updated it, DELETE removed it —
  all with the real created id correctly substituted into the path-param
  placeholders.
- **Full rollback cycle**: ran the actual saved ledger back through
  rollback and confirmed the API's real state exactly matched what it
  was before the test run.

## A second real bug this caught (building on the self-canceling-pair fix)

Testing the full POST → PATCH → DELETE cycle (not just POST → DELETE)
surfaced a gap in the earlier self-canceling-pair fix: it correctly
paired the create and delete of the same resource, but a **PATCH that
happened in between** was still treated as independently compensable.
Running the real rollback produced a genuine **404** — the PATCH's
compensation tried to restore a resource that, per the pair decision,
was being treated as if it had never existed.

Fixed by expanding self-canceling-pair detection: once a create+delete
pair on one resource is found, every *other* action on that same
resource in between is pulled into the same canceled group — the whole
lifecycle of a resource that nets out to "never existed" gets skipped
together, not just its bookends. Re-tested the exact failing scenario —
zero failed rollbacks, real final state exactly matched the original.
Re-ran the earlier, simpler regression scenario too, to confirm this
didn't break what was already working.

## Known limitations

- **Test payload generation is a simple, honest heuristic** — sends a
  generic, clearly-tagged payload (`{"name": "AuditAgent Test", ...}`)
  since there's no schema to work from. An endpoint requiring specific
  fields will reject it (422/400) and get reported as skipped, not
  silently retried with guesses. A real schema source (OpenAPI/Swagger,
  if the target app has one) would meaningfully improve this — not
  built here, since most early-stage demo apps won't have one yet.
- **Only tests resources reachable via a discoverable collection POST.**
  Endpoints that need data from elsewhere first (a resource that can
  only be created as a side effect of another action) aren't exercised.
- Same authentication scoping as discussed above: cookies only, via
  `AUDIT_COOKIES`, never through the API — this by construction can't be
  driven by the shared multi-user web app.

---

# Active Testing UI (`api.py` endpoints + `ActiveTestPanel.jsx`)

Wires `test_ledger.py`/`active_tester.py` into the actual app — before
this, they were CLI-only tools you'd have to run in a terminal and read
raw JSON from. Now there's a full UI: run the test, watch real results
come in, preview and confirm rollback, all from the browser.

## New backend endpoints

- `POST /api/active-test {output_dir}` — starts a real active-test run
  against a completed audit's endpoints, gated behind domain verification
  (same requirement as the concurrency probe — a 403 if unverified).
  Credentials still come only from the server's own `AUDIT_COOKIES` env
  var, never from this request.
- `GET /api/active-test/{id}` — poll status/results.
- `POST /api/active-test/rollback {output_dir, execute}` — preview
  (default) or actually execute rollback for a run's saved ledger.

## Validated fully end-to-end, real click through to real API calls

Built a real FastAPI task service, verified its domain for real (actual
well-known file check), ran a real audit through the actual browser,
injected a real endpoints list into that exact run, then **clicked "Run
active test" in the live UI** and watched it:

- Show the correct gated state (button disabled with an explanation) when
  a domain isn't verified, and the real enabled state once it is.
- Actually call the real API: `POST /tasks` (200), `GET /tasks/2` (200),
  `PATCH /tasks/2` (200), `DELETE /tasks/2` (200) — all shown live in the
  results table with real status codes.
- Correctly reflect the ledger's actual state in the "Preview rollback"
  panel: *"3 of 3 action(s) can be undone (1 self-canceling pair(s) —
  created and deleted within this run, nothing to undo)"* — the exact
  self-canceling-pair logic validated at the module level earlier, now
  visible and understandable in plain language in the actual app.

One thing worth knowing from testing this: the active tester's own
DELETE step already cleans up what it created as part of the normal test
cycle — rollback exists for everything *else* the run might have
changed (a PATCH that landed on a real pre-existing resource, say), not
because the tester leaves a mess by default.

## Known limitations

- Same in-memory job tracking as the audit pipeline — active-test job
  status is lost on server restart (the saved ledger file on disk is
  not, though, so rollback still works after a restart).
- The panel currently only appears for the audit you just ran in this
  session (needs `job.summary.output_dir`) — there's no UI yet to trigger
  active testing against a past run from the history list, only via a
  fresh `POST /api/active-test` call with that run's `output_dir`
  directly if you know it.

## Authenticating against a real demo/test account

Real, common case: your app has a seeded demo account and you want
active testing to actually exercise the endpoints that require login,
instead of getting a wall of 401s. Three credential modes, via
environment variables in your `.env` (never through the web app's
request body — same self-hosted-only design as everywhere else in this
project):

```bash
AUDIT_BEARER_TOKEN="eyJhbGc..."       # already have a token — use it directly
AUDIT_COOKIES="session=abc123"        # cookie-based session auth
AUDIT_LOGIN_URL="https://your-api.com/api/auth/login"   # log in automatically
AUDIT_EMAIL="demo@example.com"        # (or AUDIT_USERNAME, whichever your app uses)
AUDIT_PASSWORD="demo-password"
```

**Auto-login exists because there was no good reason not to build it.**
The manual "curl your login endpoint, copy the token, paste it in"
workflow added a step for no real security benefit — this only ever runs
against your own self-hosted instance, with credentials that only ever
live in your own `.env`, same trust boundary as pasting a token in by
hand. So: set `AUDIT_LOGIN_URL` + `AUDIT_EMAIL`/`AUDIT_USERNAME` +
`AUDIT_PASSWORD`, and the tool logs in itself before testing.

It tries common field names (`email`, `username`, `user`) and common
token-response shapes (`accessToken`, `access_token`, `token`, `jwt`,
including nested under a `data`/`user` wrapper) automatically. If your
app uses something unusual, override explicitly:
`AUDIT_LOGIN_FIELD_USER=...` / `AUDIT_LOGIN_FIELD_PASS=...`.

**Figuring out which credential mode your app needs**: check your
backend's own env vars/config. Anything with `JWT_ACCESS_SECRET`,
`JWT_SECRET`, or similar is a strong signal you need Bearer-token auth
(via `AUDIT_LOGIN_URL` or `AUDIT_BEARER_TOKEN`), not cookies — this is
exactly how a real gap got caught: `suzume`'s own `render.yaml` had
`JWT_ACCESS_SECRET`/`JWT_REFRESH_SECRET`, meaning the cookie-only
`AUDIT_COOKIES` support built first would never have actually
authenticated anything against it.

## Auto-registering a fresh test account (opt-in, real tradeoffs)

Instead of a pre-existing demo account, you can have the tool create a
**fresh, throwaway account** on your own app and use it for testing:

```bash
AUDIT_REGISTER_URL="https://your-api.com/api/auth/register"
AUDIT_REGISTER_EMAIL="you@yourdomain.com"   # optional — see below
AUDIT_PASSWORD="..."                         # optional — auto-generated if omitted
```

**Why this is reasonable, not reckless**: creating throwaway test
accounts for automated testing is completely standard practice — every
real E2E test suite does exactly this. And it's gated behind the same
domain-ownership verification as everything else invasive in this
project: if you can't prove you control the domain, you can't get to
this point at all.

**Two real risks worth knowing, which verification doesn't remove:**

1. **Side effects outside your database can't be rolled back.**
   Registration often triggers a real welcome email, an analytics event,
   a webhook to a third-party service — the ledger can undo *data*, not
   an email that already landed in someone's inbox.
2. **Email verification creates a chicken-and-egg problem.** If your app
   requires clicking a link before an account is usable, auto-registration
   will create the account but testing may fail afterward — we don't have
   access to that inbox to click the link. If this applies to your app,
   `AUDIT_REGISTER_EMAIL` with a real address you can check (using the
   `+` alias trick to keep each run unique, e.g. `you+test@yourdomain.com`
   becomes `you+testauditagent1234@yourdomain.com`) is worth using instead
   of the default placeholder, so you *could* manually verify if needed.

**Best-effort self-cleanup**: after testing, the tool searches your
discovered endpoints for a DELETE route matching common self-deletion
patterns (`/me`, `/self`, `/account`) and calls it automatically. This is
genuinely best-effort — if your app doesn't expose self-deletion via the
API at all (common), you'll get a clear message that the test account
needs manual removal, not a false claim of success.

## Validated end-to-end with a real register/login/self-delete API

Built a real FastAPI service with actual register, login, and
`DELETE /api/users/me` endpoints, and confirmed the full real chain:

- Auto-register succeeded, correctly extracted the token directly from
  the register response (the common "auto-login on signup" pattern).
- Self-cleanup found the `/api/users/me` DELETE endpoint automatically
  and called it — real `200`.
- **Verified the deletion was real, not just claimed**: attempted to log
  back in with the deleted account's credentials afterward — real `401
  Invalid credentials`, confirming the account was genuinely gone.

**Two real bugs caught in this same round of testing:**

1. **`--dry-run` was still creating a real account.** Auto-registration
   ran unconditionally before the dry-run check, meaning a "preview,
   nothing will happen" run was silently registering a real user anyway
   — completely defeating the point of dry-run. Fixed by skipping
   credential setup entirely (using a bare, unauthenticated session
   instead) whenever `--dry-run` is set.
2. **Timestamp collision on rapid successive runs.** The test-email
   generator used whole-second timestamps; running dry-run immediately
   followed by a real run (exactly what happened while testing this)
   generated the *identical* email both times, and the second
   registration attempt failed with a real `409 Conflict` — silently
   producing "0 resource groups tested" with a misleading generic error
   instead of the real cause. Fixed by adding a random suffix alongside
   the timestamp for genuine uniqueness even within the same second.

## Validated with a real login-protected API (JWT Bearer mode)

Built a real FastAPI service with an actual `/api/auth/login` endpoint
(realistic nested response shape: `{"user": {...}, "accessToken": "...",
"expiresIn": 3600}`) protecting its task endpoints, and confirmed every
real path:

- **Wrong password**: auto-login correctly fails with a clear message
  (tried all field-name candidates, none worked) — the subsequent test
  correctly gets real `401`s, no fake success.
- **Correct credentials**: `✓ Auto-login succeeded (field: "email")`,
  token correctly extracted from the nested response shape, and the full
  POST → GET → DELETE cycle succeeded with real `200`s.
- **Wrong token** (Bearer-token mode, tested earlier): still correctly
  `401`s.

**A real bug caught in this same round of testing**: the "no credentials
configured" warning was checking raw environment variables directly
rather than whether authentication actually succeeded — so it printed a
confusing false warning even right after a *successful* auto-login.
Fixed to check the actual session state (`Authorization` header /
cookies present) instead of guessing from which env vars happen to be
set. Re-verified: clean output now on a successful login, no false
warning.

## A real bug this caught on a real deployed app (`_run_active_test`)

Found by actually running this against a real split-domain deployment
(frontend on one host, API on another — exactly the `suzume` setup):
active testing was silently using `report.json`'s `start_url` (the
**crawled frontend's** URL) as its base, completely ignoring the
`--api-base-url` the audit was actually run with. Every mutating request
went to the frontend's domain instead of the real API — producing a
uniform 405 across every single endpoint that looked like a target-app
problem but was entirely this wiring defaulting to the wrong source of
truth.

Fixed by reading `merged.json`'s `merge_summary.api_base_url_used`
instead — the exact value `merge.py` already resolved and used to build
the endpoint-gap findings, so active testing now stays consistent with
the rest of the report instead of quietly using a different URL. Falls
back to `start_url` only if no merged data exists (matches the same-domain
default used everywhere else when no API base URL was given).

Re-tested with a real split-domain setup end-to-end: verified a
frontend's domain via the well-known-file method, ran a real audit with
`api_base_url` pointing at a separate backend, and confirmed every
active-test request correctly landed on the backend host — real `200`s
on POST/GET/PATCH/DELETE, not the frontend's domain.

---

# .env Live Reload (fixing real friction, not the security boundary)

You correctly identified real friction: editing `.env` required a full
server restart before the new value took effect. Fixed — `.env` now
re-reads fresh on every call to `build_authenticated_session()`
(`load_dotenv(override=True)`), so editing a token/credential and
re-running active testing picks up the change immediately, no restart.

**What this deliberately does NOT change**: credentials still only ever
come from your own `.env`, never through the web app's request body. If
this tool is deployed for other people to use, letting the frontend
accept target-site credentials would let any user of the deployed tool
submit *any* site's credentials — including stolen ones — and use your
server as the one making the authenticated requests. That's a materially
different risk than "editing a file is annoying," and the friction fix
above solves the actual complaint without touching that boundary.

**Validated properly** — not just "it compiles": ran
`build_authenticated_session()` twice within one continuous process
(matching how your real long-running `uvicorn` server behaves), editing
`.env` between the two calls with no restart. First call picked up the
original value, second call picked up the edited one — confirmed the fix
actually works, not just that it looks right on paper.

---

# Failure Diagnostics — "Dig deeper" (`diagnose_failure`, opt-in)

When active testing finds a failure, you often can't tell from a single
data point whether it's a real, deterministic bug or something
environmental (a cold-starting free-tier service, a small database
connection pool getting exhausted under a burst of test requests). This
adds an explicit, opt-in "Dig deeper" action per failed result — never
runs automatically — that re-sends the exact same failing request
several times with pauses in between and classifies the pattern.

## Classification logic

| Pattern | Classification | What it suggests |
|---|---|---|
| All attempts fail identically | `consistent_failure` | A real, deterministic bug — reproduces the same way every time |
| Only the first attempt fails, rest succeed | `cold_start_pattern` | The service (or a DB connection it depends on) needed to wake up |
| Scattered failures, not just the first | `intermittent_failure` | Load/timing-dependent — a small connection pool, a race condition, rate limiting |
| Doesn't reproduce at all | `not_reproduced` | The original failure may have been a one-off blip |

## Validated against three real servers, each built to exhibit exactly one pattern

Not trusted on logic alone — built three real FastAPI test servers, each
engineered to fail in one specific, known way, and confirmed the
classifier correctly identified all three:

- **Always-fails server** → correctly classified `consistent_failure`
  (4/4 identical `500`s).
- **Fails-once-then-recovers server** (simulating cold start) → correctly
  classified `cold_start_pattern` (`[503, 200, 200, 200]`).
- **Randomly-scattered-failure server** (simulating connection pool
  exhaustion) → correctly classified `intermittent_failure` across 5
  separate trials with different random patterns each time — including
  correctly *not* misclassifying a pattern where attempt 1 happened to
  fail but the rest didn't all succeed (`[500, 200, 500, 500, 500, 200]`)
  as cold-start just because the first attempt failed.

## A real gap caught while wiring this into the UI

The "Dig deeper" button needs to retry with the *exact* payload that
caused the original failure — otherwise it's not a faithful
reproduction. Found that real (non-dry-run) execution results were
**never recording the request body at all** — only the dry-run preview
included it. This meant "Dig deeper" would have retried with an empty
`{}` body instead of whatever actually failed. Fixed by recording the
body on every real POST/PATCH/PUT result too, then validated end-to-end:
built a server that rejects our generic test payload for a specific
reason (missing a required field), confirmed the recorded step captured
the exact rejected body, fed that same body into `diagnose_failure`, and
confirmed it faithfully reproduced the identical `422` three times in a
row — correctly classified as `consistent_failure`.

## New endpoint

`POST /api/active-test/diagnose {method, url, body}` — takes ~7-10
seconds (4 real requests with pauses between them), synchronous like the
existing chat endpoint. Doesn't need `output_dir` since it works directly
off a completed active-test result already in the frontend's hands.

---

# Diagnose Timing (`diagnose_failure` enhancement)

Direct answer to "does it note response time, and is there a timeout":
**yes to both, now** — each retry attempt has a 15-second timeout (no
indefinite waiting), and as of this update, each attempt's actual elapsed
time is recorded and shown, not just its status code. Real cold-start
evidence looks very different from a fast, immediate rejection, and the
UI now shows that difference directly (e.g. `503 · 2517ms` next to
`200 · 53ms`).

## Validated against a genuinely slow server, not a fake timing number

Built a test server that actually takes 2.5 real seconds on its first
request (simulating real cold-start latency — a DB connection actually
being established) and responds in ~50ms once warm. Confirmed the
recorded timing reflects reality: `2517ms` on the failing first attempt
vs. a `53ms` average afterward — and the cold-start explanation now cites
these real numbers directly as corroborating evidence, not just the
status-code pattern alone.

---

# Multi-User Accounts (`auth.py`, `credentials.py`)

This is the pivot from a self-hosted personal tool to something other
people can actually sign up and use — real accounts, private history,
and encrypted per-user target-site credentials instead of one shared
server `.env`.

## What's built

- **Real user accounts**: bcrypt-hashed passwords (never stored in
  plaintext, never even handled as plaintext beyond the request itself),
  signed session tokens (itsdangerous) in an httponly cookie — the
  browser can't read or tamper with it via JS.
- **Private history**: `runs` gained a `user_id` column; every history,
  stats, and report endpoint now scopes to the logged-in user. Attempting
  to access another user's run (even with a correctly-guessed folder
  name) returns 404, not the data.
- **Encrypted per-user target-site credentials**: `AUDIT_REGISTER_URL`
  and friends now live per-account, encrypted at rest (Fernet/AES) with a
  server-side master key — not a shared `.env` file. `active_tester.py`
  was refactored so its credential-reading functions accept a plain
  `dict` instead of hardcoding `os.environ`, which is what makes one
  user's target-site login never leak into another user's test run.
- **A real login/register UI**, gating the whole app — land on the
  login form if not authenticated, full app if you are.

## A real CORS bug caught before it broke everything

The original CORS setup (`allow_origins=["*"]`) is fundamentally
incompatible with cookie-based sessions — browsers explicitly forbid a
wildcard origin combined with credentialed requests. Since the frontend
(`:5173` in dev) and backend (`:8000`) are different origins, this would
have silently broken every single login. Fixed with an explicit origin
allowlist and `allow_credentials=True`, and every frontend `fetch()` call
was centralized through one wrapper (`apiFetch` in `api.js`) that always
sets `credentials: "include"` — instead of needing to remember it
correctly across a dozen separate call sites.

## Validated with real, adversarial-style tests — not just happy paths

**Security primitives, tested in isolation before building anything on
top of them:**
- Password hashing: correct password verifies, wrong password rejected,
  hash is never the plaintext.
- Registration: duplicate email rejected, weak password (<8 chars)
  rejected, invalid email format rejected.
- Session tokens: tampered token rejected, garbage token rejected, and
  genuine time-based expiry confirmed (with a real elapsed-time gap —
  caught my own first test being a false negative because it checked
  expiry with effectively zero elapsed time).
- Encryption: verified via **raw database inspection** that stored
  credentials are genuinely encrypted, not just displayed differently —
  the plaintext literally isn't present in the stored value.

**Multi-user isolation, tested end-to-end through real HTTP, with two
actual separate accounts:**
- User A ran a real audit against a real local target — appears in User
  A's history, **does not appear** in User B's.
- User B attempting to fetch User A's report via a correctly-guessed
  `output_dir` → real 404.
- User B attempting to start active testing against User A's run → real
  404.
- User A's saved credential is invisible to User B's credential-status
  check.

**Full auth loop, through an actual browser, not just the API:**
Registered a real account, watched the login gate correctly unlock into
the full app, clicked logout, confirmed it correctly kicked back to the
login form, logged back in with the same credentials, confirmed it
correctly returned to the app. Caught and fixed a real CSS bug along the
way (input fields for email/password rendered with a jarring white
background — the dark-theme input styling only covered `url`/`number`/
`search` types, not `email`/`password`).

## What's NOT built yet (explicitly deferred, not forgotten)

- **A settings UI for managing credentials** — the backend endpoints
  (`/api/credentials`) are built and tested, but there's no frontend
  panel yet for a user to actually type in their `AUDIT_REGISTER_URL`
  etc. through the UI. Currently only reachable via direct API calls.
- **Forgot password** — needs a decision on an email-sending provider
  (Resend, SendGrid, SES, etc.) before it can be built; sending a real
  reset email requires that infrastructure choice from you first.
- **Landing page + public demo section** — a real marketing/explainer
  page with an unauthenticated "try a basic audit" flow, as originally
  requested. This is a substantial, separate UI project on its own.
- **`domain_verify.py` remains intentionally NOT user-scoped** — proving
  control of a domain is a property of the domain itself, not something
  that should differ per account; two different users who can each
  independently pass the same ownership check haven't leaked anything to
  each other.

---

# Credentials Settings Panel + Two Real Bugs Found Testing It

Closed the functional gap from the multi-user pivot: the encrypted
per-user credentials system was fully built and tested at the API level,
but there was no way to actually *use* it — no UI to type in
`AUDIT_REGISTER_URL` and friends. Built `CredentialsPanel.jsx`: all
credential types grouped by strategy (auto-register / login / direct
token), each field showing live "configured" status without ever
re-displaying a saved secret, with Change/Remove controls.

## Two real, compounding bugs found by testing session persistence across a reload — not visible in any single-session test

Every earlier test in this whole auth build (register → app unlocks →
logout → login form → re-login → app unlocks) happened **within one
continuous browser session**, where React state carries the user across
screens without ever needing the session cookie to actually prove
anything. Testing an actual page **reload** — which forces the app to
restore its session purely from the cookie, the only path a real user
takes when they close and reopen the app — surfaced two real bugs that
no prior test had exercised:

**Bug 1 — `get_history`/`get_stats` assumed "DB file exists" meant
"`runs` table exists."** That was safe before user accounts existed,
but registering a user creates the database *file* too (via
`auth.init_auth_tables()`) — for a brand new account that file exists
before any audit has ever run, so the `runs` table genuinely isn't there
yet. Real `500` on the very first page load after registering. Fixed by
calling `orchestrator.init_db()` (idempotent) everywhere instead of
checking file existence — found and fixed the same latent pattern in
four other endpoints while at it, before they could cause the same bug
under slightly different conditions.

**Bug 2 — an `IndexError` in `init_auth_tables()`'s own migration
logic**, `r[1]` on a query that only ever returns one column. This bug
was *dormant* until Bug 1 was fixed — with the old file-existence check,
the `runs` table was rarely created early enough to trigger this code
path; fixing Bug 1 meant `runs` now gets created earlier, which is
exactly what exposed this second, previously-invisible bug. A classic
compounding-bug scenario, only found by testing the *interaction*
between two changes, not either one in isolation.

**Bug 3 (smaller) — `/api/auth/me` returned `{id}` only**, while
`register`/`login` return `{id, email}`. Harmless within one session
(React state already has the email from the original login/register
response) but broke the header's user-menu specifically after a reload,
when `/me` is the *only* source of truth available. Fixed to match
shape, added `auth.get_user_by_id()`.

## Fully re-validated after all three fixes, same real scenario

Registered a real account → reloaded the page → header correctly showed
email/Credentials/Logout (not just "still on the app screen," genuinely
re-authenticated via cookie) → saved a real credential through the UI →
reloaded again → credential still showed "configured" — genuine backend
persistence surviving a real reload, not React state that happened to
still be in memory.

## What's still deferred

Forgot-password (needs an email provider decision from you) and the
landing page + public demo section remain explicitly not built, same as
noted in the previous update.

---

# Landing Page + Public Demo (`LandingPage.jsx`, `/api/demo-audit`)

The last explicitly-deferred piece from the multi-user pivot (minus
forgot-password, which still needs an email provider decision from you).
A real marketing page with a genuinely live, unauthenticated demo — not
a canned example — followed by a clear path to signing up.

## What's real about the demo

Unlike a typical marketing page's "see how it works" mockup, this
actually runs your real crawler against whatever URL a visitor types in.
Deliberately constrained since it's reachable by anyone on the internet
without an account:

- **No repo field** — avoids anonymously cloning arbitrary (possibly
  huge) repos.
- **Capped at 5 pages**, forced regardless of what a request might ask
  for.
- **Concurrency probe force-disabled** — always, regardless of domain
  verification status. An anonymous visitor triggering deliberate load
  against any domain isn't something this tool should ever allow.
- **Basic in-memory rate limiting**: 3 demo runs per IP per hour.
  Explicitly a first line of defense, not a substitute for a real
  rate-limiting layer if this ever sees meaningful public traffic.
- **Invisible to every real user's history** — demo runs use
  `user_id=None`, which the `runs` table already supports; since every
  per-user query filters `WHERE user_id = ?`, no separate storage or
  cleanup logic was needed for this isolation — it falls out of the
  existing schema for free.

## Validated end-to-end, not just "the button exists"

- **Real demo audit**: confirmed `probe_skipped_unverified: true` (the
  concurrency probe forced off), a real, viewable report generated and
  served.
- **Rate limiting**: 3 requests succeeded, the 4th correctly got a real
  `429` with a clear message.
- **Isolation**: ran a demo audit, then registered a genuinely separate
  real account and confirmed its history came back empty — the demo run
  never leaked in.
- **Full browser flow**: typed a URL into the actual landing page,
  watched a real live crawl run, got real stats and a working report
  link, clicked through to sign-up, landed correctly on the register
  form.

## What's still deferred

Forgot-password remains the one piece genuinely blocked on a decision
from you — which email provider (Resend, SendGrid, SES, or something
else) you want to send real reset emails through. Everything else from
the original multi-user request list is now built and tested.

---

# Patient Mode (`--patient`, cold-start-aware timeouts)

Direct fix for a real problem: apps deployed on free-tier hosts (Render,
Vercel, Railway, etc.) commonly spin down when idle, and a cold wake-up
can take far longer than a normal request timeout. Without this, a
perfectly fine app gets misreported as broken just because the crawler
gave up too early.

## Design

An explicit opt-in checkbox in Advanced Options — never applied silently
— that waits up to 90 seconds per request instead of the default 15,
across the crawler, the audit pipeline, and the "Dig deeper" diagnostic
tool. The tradeoff is stated plainly in the UI: the whole audit takes
longer with this on, in exchange for real numbers instead of premature
timeouts.

## Validated with a genuinely slow server, not a simulated status code

Built a test server that actually sleeps 40 real seconds on its first
request:
- **Default mode**: gave up after ~16s, reported the page as a `timeout`
  error — a false negative on an app that's actually fine.
- **Patient mode**: waited the full 40 seconds, got the real response,
  correctly reported it as a slow page with its real timing
  (`40052.5ms`) instead of "broken."

## A real bug caught before it shipped

With the default retry count, patient mode could have compounded to 180
seconds per page (two attempts × 90s each) — directly contradicting the
"wait up to 1.5 min, then move on" goal. Fixed by forcing patient mode
to a single 90-second attempt per page, not multiple.

## Full stack, end-to-end confirmed

`crawler.py` (`--patient` flag) → `orchestrator.py` (`patient` param) →
`api.py` (`AuditRequest.patient`) → `AuditForm.jsx` (checkbox with clear
explanatory copy). Also extended to "Dig deeper" (`diagnose_failure`),
with a second "🐢 wait longer" button option alongside the normal one.

Confirmed the whole chain works by capturing the actual network request
sent from a real browser after checking the box — `"patient":true`
correctly reaches the backend, which was already proven separately to
produce the right behavior against a genuinely slow server.

---

# One-Click Register-Endpoint Suggestion (no typing required)

Direct response to "can you make this easier for the user." The old
path required manually finding a register URL and typing it into the
Credentials panel. But the audit already *discovers* this endpoint via
static analysis — there was no reason to make anyone retype something
the tool already knows.

## How it works

`detect_register_endpoint()` scans the already-discovered endpoints list
for a POST route with "register"/"signup" in its path. If Active Testing
runs with no credentials configured at all and finds one, the result
includes it — the frontend shows a banner: *"Found a register endpoint
in your source code: `{url}` — want to auto-register a test account with
this and re-run, with zero typing?"* One click (`Yes, use it & retest`)
saves it as `AUDIT_REGISTER_URL` and immediately re-runs Active Testing,
now authenticated. No new backend endpoint was needed — this just chains
the already-tested `saveCredential()` and `startActiveTest()` calls.

## Validated with a real before/after comparison, not just "the banner shows up"

Built a real app with a register endpoint and three endpoints requiring
Bearer auth, ran the actual flow through a browser:

- **First run (no credentials)**: real `401`s on the protected
  endpoints, and the suggestion banner correctly appeared with the
  exact real register URL, auto-detected from the endpoints list.
- **Clicked "Yes, use it & retest"** — one click, no typing anywhere.
- **Second run**: the exact same endpoints that were `401` before now
  returned real `200`s — `POST /api/items` (created), `GET
  /api/items/2` (fetched), `DELETE /api/items/2` (removed). Genuine
  before/after proof the one-click flow actually authenticates and
  unlocks real coverage, not just that a button exists.

Also worth noting from this same test run: the register endpoint itself
still gets tested as an ordinary resource group too (got a real `422`
from the generic test payload not matching its actual required fields)
— that's correct, expected behavior, not a conflict with the
credential-detection path, which uses the *right* payload shape via the
dedicated auto-register logic instead.

---

# Real Fixes: Auth Blindness + Silent Coverage Gaps

Two concrete "this is actually broken" issues, fixed and proven with
real tests, not just reasoning about the code.

## 1. Auto-register/login token extraction — was silently blind on failure

**The exact symptom reported**: an account gets created, but every
subsequent request stays unauthenticated, with no indication why. Root
cause: token extraction only checked a fixed list of field names and, on
failure, said nothing about what the response actually contained.

**Fixed two ways:**
- Broadened the recognized field list (`sessionId`, `session_token`,
  `bearerToken`, `idToken`, etc.) and extended nested-search from one
  level to two (`{"data": {"tokens": {"accessToken": "..."}}}` now
  works).
- **Made failure diagnosable.** When extraction fails, the message now
  shows the actual keys the response contained
  (`Response contained: ['user', 'sessionId', 'email (nested)']`)
  instead of a dead-end "failed." That's the difference between a silent
  wall and something you can actually act on.

**Validated with real, deliberately unusual response shapes**: a server
returning the token under `sessionId` (not in the original recognized
list) — confirmed the diagnostic correctly surfaced the real field name,
then confirmed it now succeeds directly once added to the recognized
list. A separate server nesting the token two levels deep — confirmed
the broadened search finds it.

## 2. Active testing silently skipped entire endpoint groups

**The exact gap**: any GET/PATCH/PUT/DELETE endpoint that wasn't part of
a group with a matching `POST` (to create a test resource) was dropped
from the report with **zero trace** — not "tested and failed," just
absent, as if it never existed. Standalone endpoints like `GET
/api/settings` or `PATCH /api/user/profile` (no matching `POST`) were
invisible.

**Fixed by splitting this into what's actually safe vs. actually
unsafe to auto-test:**
- **Standalone GETs are safe** (read-only, nothing to create or clean
  up) — now tested directly, no POST required.
- **Standalone mutating endpoints (PATCH/PUT/DELETE with no matching
  POST) genuinely can't be tested safely** — there's no way to know what
  real resource sits behind that id, and blindly modifying it isn't
  something to do automatically. But now this is **explicitly reported**
  with the real reason, visible in the UI as a `skipped` badge with
  explanation, instead of vanishing.

**Validated end-to-end through a real browser**: built a target with
exactly this shape (`GET /api/settings`, `PATCH /api/user/profile`, no
POST for either), ran a real audit and active test — confirmed the GET
now shows a real `200`, and the PATCH now shows a clear `skipped` badge
with the full safety explanation, both visible in the results instead of
the "0 resource groups tested" silence from before.

---

# Flow Simplification: Auto-Detect API Base URL

Direct response to "too many steps, this should just work." The exact
`suzume` situation — frontend and backend on different domains, having
to manually go find and type the backend's real URL — is now handled
automatically, using data the crawler was already collecting but never
acting on.

## How it works

`crawler.py` already passively records every XHR/fetch call a page makes
while loading (method, URL, status). `detect_api_base_url()` scans those
observed calls: if a clear majority go to one consistent domain
different from the site you're auditing, that's almost certainly the
real backend — no manual hunting required. Deliberately conservative:
requires a genuine majority, not just one stray cross-origin request, and
only activates when you haven't already typed an API base URL yourself
(never silently overrides an explicit value).

## Validated with a real split-origin app, both directions

Built a real frontend making an actual `fetch()` call to a separate
backend on a different port, crawled it for real:
- Crawler genuinely observed the cross-origin call
  (`GET http://localhost:9081/api/data (from http://localhost:9080/)`)
- `detect_api_base_url()` correctly identified the real backend origin
- **Confirmed no false positive**: ran the same detection against a
  normal same-origin app and got a clean `None`, not a wrong guess

**Full UI test, zero manual URL entry**: typed only the frontend's URL,
left "API base URL" completely blank, ran a real audit — the result
correctly showed *"🔍 Detected your API is on a different host —
automatically used `http://localhost:9091` for endpoint checks, no
manual entry needed."* Transparent, not silent — you can see exactly
what was detected and used.

---

# Dashboard Shell Rebuild — Production-Grade Layout

Direct response to "should use full window size, looks maximally good,
production grade." The real gap: the very first design reference you
gave (the HomeService dashboard image) had a persistent left sidebar
with navigation — I'd taken its color palette and glow aesthetic early
on, but never actually adopted its *structure*. Everything since then
was one long scrolling single-column page. That's the actual difference
between "a styled page" and "a production dashboard."

## What changed

- **`Sidebar.jsx`** — persistent left navigation (New Audit / History /
  Credentials), user avatar + email + logout pinned to the bottom, a
  live "N audits run" stat card, active-state highlighting. Replaces the
  old top bar entirely for the logged-in app (the top bar still exists
  and is unchanged for the public landing page).
- **Real view switching** — History and Credentials are now their own
  dedicated pages, not a single-page swap crammed above/below everything
  else. `App.jsx`'s `view` state drives this.
- **`StatsOverview.jsx`** — a real stat-card row (Audits run, Sites
  tracked, Clean runs, Concurrency flags) computed from your actual run
  history, not placeholder numbers. This is what actually fixed "full
  window looks empty" — the fix wasn't stretching existing cards wider,
  it was adding real content sized to fill the space meaningfully.

## A real layout bug caught by actually looking at a wide screenshot

First attempt at "use full window size" just widened the existing
2-column grid (`1fr 380px`) inside the new, much wider shell. Looked
broken immediately: the audit form's inputs stretched to nearly 900px
wide — uncomfortable to read, clearly not intentional — while the page
was still mostly empty below and to the right. Capping the main column
to a sane `minmax(0, 720px)` fixed the stretching; adding the stats row
fixed the emptiness. Confirmed by re-screenshotting at the same 1920×1080
viewport and comparing directly against the broken version.

## Validated across all three views, at production scale (1920×1080)

Ran a real audit end-to-end in the new shell and confirmed: the stats
row shows real aggregate numbers matching actual history, the sidebar's
live stat updates, "Recent runs" in the side panel shows the real
completed run. Clicked through to both History (shows the full table,
correct active nav state) and Credentials (its existing internal
structure rendered correctly inside the new shell with zero conflicts,
confirming the old `.main-col` wrapper class was harmless to reuse
rather than needing a rewrite).

---

# Auto-Detection Silent-Failure Fix (found via a real `suzume` run)

Your real audit surfaced this precisely: two genuinely great wins (8 new
GET endpoints working, GET/PATCH-with-no-POST correctly skipped) sitting
right next to every POST hitting `suzume.akshathag.in` instead of the
real backend — the exact wrong-domain symptom from much earlier,
resurfacing through a different path.

## Root cause

Auto-detection only has data to work with if the passive crawl actually
*observes* cross-origin API calls. A shallow, unauthenticated crawl of
an app where most functionality sits behind login — exactly `suzume`'s
situation — may simply never see that traffic. Detection correctly found
nothing and fell back to same-domain, which was silently indistinguishable
from "detection ran and confirmed same-domain is correct." Those are
very different levels of confidence, and only one of them was visible.

## Fixed

Three real, distinguishable states are now tracked and shown:
`manual` (you set it), `detected` (found real evidence), or
`same_domain_fallback` (no evidence either way). The UI now shows a
clear warning specifically when the fallback happened *and* there's a
suspiciously high unreached-endpoint count — the exact combination that
suggests the wrong domain, not just "nothing to report."

## Validated by reproducing the exact real symptom, not a synthetic one

Used your actual `suzume` repo (44 real defined endpoints) against a
local frontend with zero cross-origin calls — guaranteed to make
detection find nothing, exactly like your real shallow crawl. Confirmed
end-to-end through the real UI: `44 unreached endpoints` plus the exact
warning that would have told you, before running Active Testing at all,
that the API base URL needed to be set manually.

**Practical takeaway for your `suzume` audits specifically**: until
`suzume` requires login for most of its real functionality, a fresh
unauthenticated crawl likely won't have enough evidence to auto-detect
the backend. Setting `https://suzume-tn4e.onrender.com` manually in
Advanced options remains the reliable path for now — the warning will
tell you exactly when that's needed instead of leaving you guessing.

---

# Code-First Flow + Glanceable Results Redesign

Two changes, both direct responses to real feedback: the flow order was
backwards for a code-focused audience, and results required too much
scrolling to understand.

## New flow: code review first, live audit is opt-in after

- **`code_reviewer.py`** — pure static, code-only review. No live URL
  involved. Clones the repo (reusing `static_analysis.py`'s existing
  clone/file-walk logic), samples a representative set of files, and
  asks an LLM for short, scannable good/bad findings — explicitly
  prompted for single-line bullets, not paragraphs, since developers
  scan for key points.
- **`CodeReviewFlow.jsx`** — the new default first screen: just a GitHub
  URL field, nothing about a deployed site. Results render as a 2-column
  grid of color-coded cards (green ✅ good, amber/red ⚠️ risky), then a
  single prompt: *"Want to also check the deployed version?"* — Yes
  carries the repo forward with zero re-typing into the existing full
  audit form; No or Skip goes straight there too.

**Two real bugs caught building this:**
1. **File selection only grabbed frontend files from a monorepo.**
   Tested against the real `suzume` repo (apps/web + apps/api +
   packages/) and confirmed all 12 selected files were frontend-only —
   zero backend route handlers, missing exactly the files most likely to
   explain a server-side bug. Fixed with round-robin selection across
   detected top-level directories — then found the fix itself was
   incomplete (grouped `apps/web` and `apps/api` into the same "apps"
   bucket, since only the first path segment was checked), fixed to
   group by two segments when the first is a generic container.
2. **A missing-API-key error wasn't caught**, crashing the endpoint with
   an unhandled `RuntimeError` instead of failing gracefully. Fixed and
   confirmed a real graceful failure — cloned the actual repo, selected
   real files, then returned a clean error instead of a stack trace.

**Validated end-to-end through a real browser**, including the full
handoff: code review → "audit deployed too?" → live audit → completed
result, confirmed with the repo field genuinely pre-filled (checked its
actual input value, not just visually).

## Active Testing results: summary bar + collapsible grid, not one long scroll

Replaced always-expanded per-group tables (23 groups × full detail = the
exact "so much scrolling" complaint) with:
- **A summary bar first** — real counts by outcome (`2 OK`, `16 Auth
  required`, `3 Server error`), giving the one-glance understanding
  requested before any detail at all.
- **A compact grid of collapsed cards** — just path + status badge per
  card, click to expand for full detail (steps, Dig deeper, skip
  reasons). All 21+ resource groups now fit in roughly the space the old
  design needed for 4-5.

**Validated by replicating your exact real `suzume` result** (21
endpoints, the same real mix of 2xx/401/500 outcomes) end-to-end through
a real browser: confirmed the summary bar correctly shows `2 OK`, `16
Auth required`, `3 Server error` at a glance, and confirmed clicking a
collapsed card correctly expands to reveal full detail including a real
`500` and working Dig deeper / wait-longer buttons.

---

# Postgres Migration + Deployment Readiness

Real, tested migration from SQLite to Postgres, plus the actual
deployment configs for Vercel (frontend) + Render (backend + database).

## Why this was necessary, not optional

Render's filesystem is ephemeral — anything written to disk disappears
on every redeploy. The old SQLite file would have been silently wiped
every time you pushed a change, taking every user account, every run
history entry, and every stored credential with it. This isn't a
scaling nice-to-have; without it, the app cannot survive a single
redeploy in production.

## `db.py` — one abstraction, both dialects genuinely tested

Local development stays exactly as it was — SQLite, zero setup. Set
`DATABASE_URL` (Render provides this automatically once a Postgres
instance is attached) and every module transparently switches to
Postgres instead. Real dialect differences handled:

- `?` → `%s` placeholder translation
- `sqlite_master`/`PRAGMA table_info` → `information_schema` introspection
- `cursor.lastrowid` (sqlite-only) → `INSERT ... RETURNING id`
- `sqlite3.Row` → a portable dict-rows helper

**Installed a real local Postgres 16 server in this sandbox specifically
to test against**, not just SQLite — every migrated module
(`auth.py`, `credentials.py`, `orchestrator.py`, `domain_verify.py`) was
tested against both databases individually, then the **entire app** was
smoke-tested end-to-end against real Postgres: two real user accounts,
a real audit run, and confirmed the exact same per-user isolation
guarantees (private history, private encrypted credentials) that were
originally proven against SQLite much earlier in this project.

## Two real bugs caught before they could reach production

1. **`cursor.lastrowid` doesn't work reliably on psycopg2.** Found by
   directly testing `auth.register_user()` against real Postgres —
   fixed with `db.insert_returning_id()`, which branches to `RETURNING
   id` on Postgres.
2. **A platform's "auto-generate a secret" is not a valid Fernet key.**
   Tested this directly: a generic random string throws `Fernet key must
   be 32 url-safe base64-encoded bytes` and crashes the app on startup.
   `render.yaml` deliberately does NOT use Render's `generateValue` for
   `CREDENTIALS_MASTER_KEY` — it requires you to generate a real one
   yourself and set it manually. `credentials.py` also now catches this
   specific failure and raises a clear, actionable error instead of the
   cryptic default one, tested with both an invalid and a valid key to
   confirm both paths work correctly.

## Deployment

### Backend (Render)

`render.yaml` defines a web service + a managed Postgres instance,
wired together automatically via `fromDatabase`. Before deploying:

1. Push this repo to GitHub, connect it in Render's dashboard as a
   Blueprint (reads `render.yaml` automatically).
2. Set `GEMINI_API_KEY` in Render's dashboard (marked `sync: false` —
   Render won't set this for you).
3. Generate a real Fernet key locally and set it as `CREDENTIALS_MASTER_KEY`:
   ```
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
4. Once you have your Vercel frontend URL, set it as `FRONTEND_URL` in
   Render's dashboard too (see CORS note below).
5. `SESSION_SECRET_KEY` is auto-generated by Render's blueprint — no
   action needed.

### Frontend (Vercel)

`frontend/vercel.json` configures the build. Before deploying:

1. Import the `frontend/` folder as a new Vercel project (framework
   auto-detected as Vite).
2. Set `VITE_API_BASE_URL` in Vercel's project settings to your real
   deployed Render backend URL — confirmed this correctly gets baked
   into the production build by testing it directly (built with a real
   URL set, then grepped the output bundle to confirm it was actually
   embedded, not just trusting the build succeeded).

### The CORS chicken-and-egg step

The backend needs to know the frontend's URL (`FRONTEND_URL`) and the
frontend needs to know the backend's URL (`VITE_API_BASE_URL`) — you'll
likely deploy one first with a placeholder, then circle back and set the
other's real URL once you have it. Cookie-based login will fail with a
CORS error from the real frontend until `FRONTEND_URL` is set correctly
on the backend — this isn't a bug, it's the same wildcard-plus-credentials
restriction documented earlier in this project.

## Still not done

The UI changes (top bar instead of sidebar, an interactive "game-like"
live-audit visualization) and the new authorization policy (public
GitHub repos open to anyone without an account; deployed-site audits
still requiring login + domain verification) from the same request are
not yet built — this pass focused entirely on the infrastructure work
required to actually ship anywhere.

---

# Top Bar + Interactive Live-Audit Visualization

Direct response to "I asked for a full UI change" — the two pieces that
were set aside for infrastructure work earlier: switching from the
persistent sidebar back to a top bar, and making the running-audit state
genuinely interactive instead of a plain text log.

## Top bar (`TopBar.jsx`)

Same nav destinations as the sidebar (New Audit / History / Credentials)
plus the user avatar and logout, now horizontal with a glowing
active-state highlight matching the game aesthetic. `App.jsx`'s shell
structure was updated to match — confirmed via a real browser screenshot
at 1400×900 that the active-state glow, avatar, and "N XP" badge
(renamed from "audits run" for the more game-like framing) all render
correctly.

## Live scanner during an actual running audit

Previously: a flat step tracker plus a static text log. Now: a
pulsing radar-ring animation centered on the current stage's icon, with
short flavor text that rotates every ~2 seconds while that stage is
active ("Following every link it can find…", "Consulting the AI
oracle…", etc.) — genuinely tied to which real stage is running, not
decorative filler shown regardless of state. Completed steps get a pop
animation; the active step gets a pulsing glow outline.

**Deliberately honest about what this is and isn't**: the backend only
reports stage-level progress (which of the 5 pipeline stages is
running), not fine-grained sub-progress within a stage — so this doesn't
fabricate a fake percentage or fake item counts ticking up. The
animation and flavor text are genuine, freely-offered visual interest
during a real wait, not simulated data pretending to be real progress.

**Validated end-to-end**: ran a real audit through the actual browser,
confirmed the scanner correctly showed the crawling stage's icon with
its rings and flavor text while crawling was genuinely in progress, then
confirmed the same run completed successfully afterward with no
regressions — real report data, real stats, all five steps correctly
marked complete.

---

# Six-Item List: Closed Out — Authorization Policy

The last open item from the original six-part request. Two real
distinctions, both tested end-to-end through real HTTP calls and a real
browser, not just written:

## Public GitHub code review — no account needed

`/api/code-review` now accepts anonymous requests (`get_current_user_id_optional`
instead of the required version). Confirmed: an anonymous session got a
real `200` and a running job with zero login. A logged-in user's review
still stays private to them — only anonymous jobs are accessible to
anyone holding the random `job_id`, the same trust model the public
demo-audit endpoint already uses.

## Deployed-site audits — now require BOTH login and domain verification

Previously, verification only gated the invasive concurrency probe —
anyone logged in could passively crawl any URL at all without proving
ownership. That's now a real requirement for the audit itself, not just
the probe: `POST /api/audits` checks `domain_verify.is_verified()` and
returns a clean `403` if not verified. Confirmed all three states
directly: an unverified domain correctly blocked, then verified via the
well-known-file flow, then confirmed the *same* request now succeeds.

**Frontend updated to match**: the "Run audit" button is now genuinely
disabled (not just decorated with a warning) until the domain is
verified, with corrected copy — the old text said "the crawl will run
anyway," which was no longer true and would have been actively
misleading. Confirmed via a real browser screenshot that the disabled
state and new messaging render correctly.

---

# Game-Style Interactivity (top bar + live scanner) — see earlier entries

The top bar redesign and the live radar-scanner visualization during a
running audit were built and validated in the previous update — see
"Top Bar + Interactive Live-Audit Visualization" above.

## Honest scope note on "full UI change, landing to end report"

This request — a complete visual overhaul across every screen (landing
page, auth, all three dashboard views, AND the separately-generated
report HTML file, which has its own independent styling untouched since
the very first dark-theme pass) — is genuinely more than one pass can
responsibly cover with the same testing rigor used throughout this
project. This update focused on finishing the concrete, well-defined
piece (the auth policy) and validating it properly. The report file's
visual refresh and further landing-page polish remain open — flagging
this directly rather than claiming a "full" redesign that wasn't
actually built and tested to the same standard as everything else here.

---

# Report File Visual Refresh — the actual "end" of "landing to end report"

The generated report HTML hadn't been touched since the very first
dark-theme pass, long before the game-style scanner/topbar aesthetic
existed anywhere else in the app. Closing that gap.

## What changed

- **A real, computed health score** — the report never showed one at
  all before, just raw numbers (pages crawled, duration, etc.) with no
  overall verdict. `compute_health_score()` in `report_generator.py`
  is a direct line-for-line port of the dashboard's
  `HealthScore.jsx` logic — same penalties, same bands — specifically so
  the shared report link never disagrees with what the dashboard showed
  for that exact run.
- **A circular glowing health ring** in the header, color-matched to the
  verdict (green/amber/red), plus glowing bordered stat chips replacing
  the old plain numbers.
- **Subtle CSS-only animations** — a scan-line sweep across the header
  background, cards rising into place with a staggered fade-in. Pure CSS
  (`@keyframes`), no JavaScript dependency, since this file needs to work
  standalone anywhere it's opened (email, Slack preview, a downloaded
  file) without relying on a script actually executing.

## Validated by actually generating and screenshotting a real report

Built real crawl/merge JSON matching the shape of data seen earlier in
this project (a slow page, a passed concurrency check, a bottleneck, 44
unreached endpoints), ran it through the real `report_generator.py`, and
rendered the actual output through a real browser. Confirmed the health
score computed to exactly `66` — verified by hand (100 − 9 for the
bottleneck overage penalty − 25 for the capped unreached-endpoint
penalty) — and that the ring, verdict label ("Solid"), and all four stat
chips rendered correctly and consistently with the color language used
throughout the rest of the app.

---

# Layout Fix: Less Scrolling, Full-Width Results, Real Chart

Direct response to specific, concrete layout complaints — de-prioritize
Recent runs, stop making people scroll to see results, use the full
window width, add real visuals, and fix the top bar's sizing/alignment.

## What changed

- **Compact completion card, not the full result inline.** Finishing an
  audit now shows a small "Audit complete" card with a "View full
  results →" button — not the entire ResultCard + ActiveTestPanel
  dumped into the page, which was the actual cause of the scrolling
  complaint.
- **A dedicated, full-width results page.** Clicking through opens a
  proper wide layout — `1fr 360px` grid, main results on the left, a
  real chart on the right — using the full window width instead of a
  narrow single column.
- **A real chart** (`ResultsCharts.jsx`, using `recharts`, a new real
  dependency) — a health-score trend bar chart comparing this run
  against your last 7 audits of any site, computed with the exact same
  scoring function the badge itself uses (`computeHealthScore`), not
  fabricated data.
- **Recent runs pushed down and visually quieted** — moved below
  everything else on the New Audit page instead of sitting prominently
  beside the form, with slightly reduced opacity.
- **Top bar height reduced** and **corner alignment actually fixed** —
  confirmed via a real 1920px-wide screenshot that the brand sits
  genuinely flush left, nav is centered, and the avatar/logout controls
  sit flush against the right edge, not just visually close to it.

## Validated end-to-end at 1920×1080, not just described

Ran a real audit through the actual browser: confirmed the compact
completion card appears with zero scrolling required, clicked through to
the results page and confirmed the wide grid layout with the chart
correctly rendering computed (not fake) health-score data, and
confirmed Recent runs correctly appears below everything, de-emphasized.

---

# Live Scanner Docked Right + Active Testing's Own Full Page

Two more concrete layout requests: keep the live audit progress on the
right side while it's actually running, and give Active Testing the
same "dedicated full page with explanation" treatment the results page
just got.

## While an audit is running: form stays left, scanner docks right

The audit page switches to a two-column layout only while
`job.status === "running"` — the form (disabled) stays on the left, the
live radar-scanner panel appears docked on the right. Reverts to the
normal single-column layout once idle or complete.

**A real bug caught immediately by screenshotting this**: the five-step
flow row was designed for a wide main column and visibly overflowed in
the narrower ~350px side panel — labels cut off, steps crowding into
each other. Fixed with a dedicated compact mode (steps stack vertically,
detail text hidden) that only applies when the scanner is in this
docked-right context — confirmed clean with a second screenshot after
the fix.

## Active Testing: its own full page, not embedded inline

Previously Active Testing's entire interactive panel sat embedded in the
results page's main column. Now: the results page shows a clickable
link card ("Active Testing — actually calls your mutating endpoints...
→") plus a lightweight status summary in the side panel. Clicking
through opens a dedicated page with an explanation of what Active
Testing actually does, the real interactive panel (unchanged internals —
verification, the register-endpoint suggestion, rollback, diagnose, all
of it), and the same status summary now docked on the right.

**Avoided a real state-duplication risk while building this**: the
naive approach — rendering `ActiveTestPanel` twice (once compact in a
sidebar, once full on its own page) — would have created two independent
copies of its internal job state that could silently drift out of sync.
Instead, exactly one real instance exists (on the dedicated page), and
reports its status upward via a new `onStatusChange` callback to a
separate, purely presentational `ActiveTestStatusCard` used everywhere
else a summary is needed. Zero risk to the already-tested interactive
logic, since none of it was touched or duplicated.

## Validated end-to-end through a real browser, all three states

Ran a genuinely slow real audit (deliberately 2-3 second page loads) to
capture the actual running state mid-flight — confirmed the scanner
correctly docks right with the form still visible and disabled on the
left. Continued through to completion, confirmed the results page's new
link card and side-panel status summary, then clicked through to the
dedicated Active Testing page and confirmed the explanation, the real
trigger button, and the synced status card all render correctly.

---

# Wider Results Column + Engaging Wait + Shareable Active-Test Report

Three concrete requests, plus a serious latent bug caught and fixed
along the way.

## Widened the results side column

`results-grid` changed from a fixed `360px` side column to
`minmax(420px, 620px)` — real breathing room for the chart and status
card, confirmed via a full-page screenshot that the cramped scroll is
gone.

## A critical bug found and fixed: `rollbackPreview` was never declared

While reviewing this file, found that an earlier edit had accidentally
merged two lines together — `const [rollbackPreview, setRollbackPreview]
= useState(null);` had silently become part of a JavaScript **comment**,
meaning that state was never actually declared. This would have crashed
the moment anyone used the rollback-preview feature. Fixed, and — this
mattered — **specifically re-tested the rollback preview flow itself**
afterward (not just confirmed the build compiles) to prove it actually
works: correctly identified a self-canceling create+delete pair and
rendered "2 of 2 action(s) can be undone... nothing to undo" with zero
crash.

## Active Testing: an honestly-built engaging wait

A real elapsed-time counter and rotating flavor text ("Poking at your
POST endpoints…", "Recording every change for safe rollback…") replace
the old static "Testing endpoints…" line. Deliberately built without
fake progress bars or invented percentages — the backend genuinely
doesn't report granular per-step progress, so this stays honest: real
animation, real elapsed time, real (if generic) flavor text, not
simulated data pretending to be more informative than it is.

## Shareable Active-Test report, opens in a new tab via a real shortened link

- **`active_test_report.py`** — a new, full-width, dark-themed HTML
  report specifically for active-testing results: findings grouped by
  resource, plus an LLM-generated "Improvements" and "Scalability"
  section grounded only in what was actually found (same
  graceful-degradation pattern as `code_reviewer.py` when no API key is
  present).
- **`POST /api/active-test/report`** generates and saves this report,
  then calls the existing Go shortener service (`shortener/main.go`,
  built earlier in this project but never actually wired up until now)
  to get a real short link — falling back to the full URL if the
  shortener isn't running, rather than failing the whole request over a
  genuinely optional piece.

**A real integration bug caught before it shipped**: assumed the
shortener's endpoint was `/api/shorten` — it's actually just `/shorten`.
Found this by actually building the real Go binary and testing the real
HTTP call, not by reading the code and assuming.

**Validated with the full real chain, twice** — once via direct HTTP
calls (registered a real account, ran a real audit, generated a report,
got a real shortened URL, followed it, confirmed the real findings
rendered correctly), and again through an actual browser click,
confirming `window.open()` on the shortened link correctly opens a new
tab that follows the redirect to the real, freshly-generated report with
this exact test run's real `200` findings.

---

# Downloadable PDF Reports

Direct answer to "will it be a downloadable PDF" — it wasn't, now it is,
for both the main audit report and the active-testing report.

## Zero new dependencies

`pdf_export.py` uses Playwright's built-in print-to-PDF — and Playwright
+ Chromium is *already* a required install for `crawler.py`'s actual
crawling (already in `render.yaml`'s build command). This reuses the
exact same browser binary rather than adding a second HTML-to-PDF
library just for this one feature.

## A real risk caught and fixed before it mattered

Dark-themed reports specifically needed `print_background=True` and
`emulate_media("screen")` — browsers skip background colors on print by
default, which would have produced a PDF with light text on a plain
white background, unreadable. Didn't just trust this would work:
generated a real PDF, converted it back to an image with `pdftoppm`, and
visually confirmed the dark background was genuinely preserved before
moving on.

## Two endpoints, deliberately different auth requirements

- `GET /api/history/report/pdf` — the main audit report, **requires
  login** (same ownership check as everywhere else private data lives).
  Confirmed both sides directly: a logged-in owner gets a real `200`
  with valid PDF bytes, an anonymous request gets a real `401`.
- `GET /api/active-test/report/pdf` — the active-test report,
  **intentionally not behind login** — same reasoning as the HTML view
  and the shortened link it pairs with: this is meant to be shareable
  (a PR description, a Slack message) without requiring the recipient to
  have an account.

## Validated at every layer — magic bytes, visual render, and a real browser click

Tested via direct HTTP calls first (correct `Content-Type`,
`Content-Disposition: attachment`, valid `%PDF` header, reasonable file
size, both endpoints), then through an actual browser click on the new
"Download PDF" button — confirmed Playwright's own download detection
caught a real download event, with the correct suggested filename and
genuinely valid PDF content, not just that a link existed.

---

# In-Place Completion + Real New-Tab Flow + Full Report Access

Four concrete changes from real screenshots you shared, all validated
end-to-end through three genuinely separate browser tabs in one test —
not just in-app view switches dressed up to look like navigation.

## Compact scanner box — fits without page scroll

The scanner rings, step list, and terminal together exceeded typical
viewport height in the docked-right context. Compacted all three (ring
size, step card padding, gaps) and increased the terminal's max-height,
confirmed via a full screenshot that the whole box now fits with real
room to spare.

## Completion shown in the same box, no page navigation, no chart

Previously, finishing an audit meant clicking through to a separate
"results" page. Now the same right-side box that showed the scanner
transforms in place into the completion summary (health score, stats,
report links) plus a "Start Active Testing" prompt — deliberately no
chart here, matching what was asked.

## Active Testing genuinely opens in a new browser tab

This needed real URL-based routing, not just switching `view` state
within the same tab — a new tab is a fresh page load with zero React
state carried over. Added lightweight URL-param bootstrapping
(`?view=active-test&output_dir=...&url=...`) — `ActiveTestPanel` only
ever needed `url` and `outputDir` to function, so that's all a fresh tab
needs to reconstruct correctly. Confirmed with Playwright's actual
`expect_page()` new-tab detection, not just checking that a link exists.

**A real bug caught in the same pass**: the "Back" button on the
Active Testing page pointed to the `results` view, which wouldn't exist
with real data in a fresh new-tab context (only `output_dir` and `url`
are set, not the full audit summary `ResultCard` needs). Fixed to point
back to the always-safe `audit` view instead.

## "Full Report" prominently on the right, reusing tested infrastructure

Rather than building a second, parallel report system, the right-side
"Full Report" card on the Active Testing page calls the same
`generateActiveTestReport` flow already built and tested — disabled
until a real test has actually run, then opens the real
findings-by-resource + Improvements + Scalability report in yet another
new tab via the real shortener.

## Validated with three genuinely separate tabs in one continuous test

Main tab (audit) → new tab (Active Testing, confirmed via its own fresh
URL) → new tab (the final report, confirmed via the real shortened
link's redirect target) — each captured and visually confirmed correct,
not inferred from the code.

---

# One Continuous Report, Growing Through the Whole Flow

Direct response to: consent before creating test accounts, and a
properly-structured, expandable, ONE report that starts at code review
and grows through the deployed audit and active testing — not three
disconnected reports.

## Consent step before the deployed audit

Clicking "Yes, audit deployed site" now shows a clear disclosure about
optional test-account creation before proceeding, requiring explicit
"Understood, continue" — confirmed via screenshot.

## `unified_report.py` — one document, four expandable sections

Native HTML `<details>`/`<summary>` blocks (no JS framework needed,
works anywhere): Code Review, Deployed Audit, Active Testing,
Improvements & Scalability. Sections not yet run show a clear "not run"
or "pending" badge with an explanation, rather than being blank or
absent — so the shape of the eventual full report is visible from the
very first moment, right after code review.

**Validated by rendering and screenshotting all three progressive
states** before any frontend wiring existed: code-review-only, then
+deployed-audit, then +active-testing+suggestions — confirming the core
rendering logic was solid before building anything on top of it.

## Real backend API, tested directly before any UI touched it

`POST /api/report/create`, `POST /api/report/update`, `GET
/api/report/view` — tested with direct HTTP calls first: created a
report, viewed it, updated it, confirmed both pieces of data persisted
together in the same document.

## A real bug found and fixed by tracing the actual data, not guessing

Wired the frontend end-to-end and found section 4 stuck on "pending"
even after generating the full report. Didn't guess at the cause —
intercepted the actual network request to confirm the frontend was
sending the right data, then traced it into the backend and found the
real issue: `update_report()` was called with plain field names
(`active_test=`, `suggestions=`) at one call site while the function
expected `_json`-suffixed names to match the actual database columns
elsewhere. Fixed properly, not just patched: changed `update_report()`
itself to accept plain names everywhere and do the `_json` mapping
internally in exactly one place, so this class of bug can't recur at a
new call site later. Re-ran the full browser test afterward and
confirmed section 4 now genuinely populates with real suggestions data.

## Validated end-to-end, all four sections, real new-tab navigation

Full real browser test: code review → "View report" link appears
immediately → consent step → deployed audit (report grows, confirmed via
screenshot) → genuinely new browser tab for Active Testing (confirmed
`report_id` carries through the URL, confirmed via network interception
that the frontend sends it correctly) → Active Testing completes →
"View full report" generates suggestions → "View growing report" opens
the same document, now complete with all four sections holding real data
from that exact test run.

---

# Three Quick Corrections

## Simplified report link text

All three "View report" buttons/links (after code review, after the
deployed audit, on the Active Testing page) now just say "View report
→" — removed the "grows as you audit more" / "grows as you test more" /
"code review + audit + testing" explanatory suffixes.

## Removed the consent step

The disclosure about optional test-account creation, added in the
previous update, is gone — clicking "Yes, audit deployed site" now goes
directly to the deployed-audit form again, same as before that change.

## Fixed the report's width — same bug pattern as before, caught immediately

`unified_report.py`'s `<main>` had `max-width: 1100px` — the exact same
mistake as the `.wrap` class fixed earlier in this project. Removed it.
Also fixed `.mini-card-grid`, which had no `grid-template-columns` at
all (defaulting to a single column regardless of width) — now uses
`repeat(auto-fill, minmax(360px, 1fr))` so cards actually spread across
the newly-available width instead of stacking narrow in a now-wide page.

**Validated directly**: created a report via the API (bypassing the
LLM-dependent code review step entirely, so this could be tested without
a real API key) and measured the actual rendered `<main>` width in the
browser — confirmed `1920px` of a `1920px` viewport, not the old
`1100px` cap. Screenshotted it too: the two code-review findings now sit
side-by-side instead of stacked in a narrow column.

---

# Final Pass: One Report, Real Caching, Simplified Active Testing, New Landing Page

## Active Testing page simplified to single column

Removed the right-side column entirely (the duplicate status card and
the "Full Report" trigger that lived there) — everything now flows in
one column: explanation, the real interactive panel, then the report
actions below it. Less redundant, less visual clutter.

## The unified report now embeds the SAME rich content as the standalone one

This was the real fix behind "make the report as good as the code
review section" — the "Deployed Audit" section was a watered-down
3-stat summary of what `report_generator.py`'s standalone report already
showed in full detail. Refactored `report_generator.py` to separate its
actual content (health ring, stat chips, findings cards, dependency
graph) from its page shell, so the exact same rendering function now
gets called from two places: the standalone report, and — new —
directly inside the unified report's "Deployed Audit" section. One
source of truth for the content, not two reports slowly drifting apart.

**Validated by generating a real report through the full pipeline** —
confirmed the embedded section shows a genuinely-computed health score,
real stat chips, an actual finding (a concurrency degradation
legitimately detected during the test crawl), and the dependency graph
image, all correctly styled within the same page as the Code Review
section.

## Real caching for repo reviews — the memory/resource concern

Every code review previously re-cloned the target repo from scratch,
every single time, for every request — real, measured waste, worth
fixing before deploying somewhere resource-constrained. Added a cache
keyed by repo URL, checked via `git ls-remote` (a few KB over the wire,
no clone at all) against the actual latest commit SHA — if unchanged
within an hour, the cached result is returned directly, skipping both
the clone AND the LLM call entirely.

**Validated directly against the real `suzume` repo**: first call
cloned and ran the LLM (0.62s); second call for the same repo was
served entirely from cache (0.15s) — confirmed the LLM was called
exactly once across both requests, not twice.

## Landing page redesign

Bolder gradient headline, a badge pill, a stats strip (3 signals
combined / 0 setup for a demo / 100% rollback), and feature cards with
icon badges, hover lift, and a staggered entrance animation — aiming for
the same confident, generous-whitespace feel as Linear's or Google's
product pages rather than the previous plain, narrow, centered layout.

---

# Report Consolidation, UI Polish, Disk Cleanup

## Code Review: buttons first, content below

"Yes, audit deployed site" / "No, that's enough" / "View report" now
appear right under the header, before the good/bad findings — confirmed
via screenshot.

## Fixed the dull "Start Active Testing" button

Was using the same flat panel background as any other card. Now uses
the same warm orange energy as the rest of the active-testing UI (a
glow even at rest, not only on hover) — confirmed it no longer blends
into the background.

## One report, not two — the standalone active-test report is gone

This was the real structural fix. Previously `/api/active-test/report`
built and saved a completely separate `active_test_report.html` file,
shortened *that* link, and served *that* PDF — genuinely a second report
living alongside the unified one. Removed entirely:

- The endpoint now only generates suggestions and pushes them into the
  *existing* unified report — no file gets written for a separate report
  at all.
- Removed the now-dead `/api/active-test/report/view` and
  `/api/active-test/report/pdf` endpoints.
- Added `/api/report/share` and `/api/report/pdf` for the ONE report
  instead — sharing and downloading both now point at the exact same
  continuous document that started at code review.
- Moved the "Full Report" section out of its own separate side-card and
  directly into `ActiveTestPanel`'s own UI — Generate / View / Download
  PDF all live in one place now, not scattered across a redundant block.

**Validated with a real, complete run through the whole flow**: code
review → deployed audit → active testing → clicked "Generate report" —
confirmed the resulting tab's URL was genuinely `/api/report/view`, not
the old removed endpoint, and confirmed the final document held all
four sections' real data together: code review findings, the full rich
audit section (health ring, real concurrency finding, the actual
dependency graph with 44+ real endpoints), the active-test result, and
real improvement/scalability suggestions.

## Fixed: audit output directories were never cleaned up, ever

A genuine, previously-unfound issue — `runs/` (report.json, merged.json,
graph.png, report.html, one directory per audit) had zero cleanup
anywhere, growing forever. Added `cleanup_old_runs()`, run automatically
at the start of every pipeline execution: deletes run directories older
than `AUDITAGENT_RUN_RETENTION_DAYS` (default 14), while keeping the
lightweight database row so history and stats stay accurate. Validated
directly: created a real 25-day-old directory and a real 2-day-old one,
confirmed the cleanup deleted exactly the old one and left the recent
one untouched.

## Deployment reality check: Render's ephemeral filesystem

Cloned repos for code review were already being cleaned up correctly
(confirmed in the code, not just assumed). The bigger fact worth
knowing before deploying: Render's disk is ephemeral — wiped on every
redeploy, not just old files pruned by the new retention policy. Shared
report links and PDF downloads won't survive a redeploy unless file
storage moves to something like S3-compatible object storage, which
isn't built yet. Reasonable to accept as-is for now; worth revisiting if
long-lived shareable links become something people actually rely on.

---

# Active Testing Navigation, Landing Page Cohesion, Graph Enhancement

## Active Testing now navigates in-app, like History/Credentials

Was opening in a genuinely new browser tab (built deliberately a few
turns back) — now it's just `setView("active-test")`, same as clicking
History or Credentials in the top bar. Removed the now-unused
`buildActiveTestUrl` URL-construction function; kept the URL-param
bootstrap logic itself as a fallback for directly-shared links, since
that's harmless and doesn't hurt anything.

## Fixed the landing page's section-to-section color inconsistency

Root cause: the ambient background glow (the two radial gradients
behind the hero) were positioned as a percentage of the *whole
document's* height, not the viewport. On a long scrolling landing page
that meant the glow only ever appeared near the very top of the actual
HTML document — scroll past it and you hit a flat, glow-less background,
which is exactly the "colors don't match between sections" feeling.
Fixed with `background-attachment: fixed` so the glow stays anchored to
the viewport itself.

**Validated with a genuine scroll test**, not a full-page composite
screenshot (which wouldn't have caught this): captured the real
viewport at scroll position 0 and again after scrolling down ~900px —
confirmed the ambient glow is now consistently present in both, rather
than only visible at the very top.

## Graph visualization — actually enhanced this time

Three real, concrete additions, all validated on a genuine 29-node
graph with a realistic mix of pages/APIs/broken/slow nodes:

- **A legend** — the colors previously had zero explanation anywhere
  near the image itself.
- **Smart label truncation** — full raw URLs at every node made
  anything beyond a handful of nodes unreadable. Labels now strip the
  (redundant, identical-per-node) domain and cap length; on larger
  graphs, only the most-central 15 nodes get labeled at all, with a
  caption explicitly saying so, rather than cluttering everything.
- **A distinct highlight ring** around slow or broken nodes — visible
  independent of the base color, so problem areas are visible at a
  glance instead of only inferable by cross-referencing the legend.
