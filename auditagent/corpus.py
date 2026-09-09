"""
AuditAgent v2 — System Design Pattern Corpus
A small, hand-curated set of system-design patterns for the Architect
agent to retrieve from. Per the project plan: start with plain retrieval
over ~20 hand-picked patterns before considering GraphRAG — the corpus is
small enough that concept relationships don't yet matter for retrieval
quality.

Each entry has:
    id       - short identifier
    title    - human-readable name
    tags     - keywords used for retrieval matching
    problem  - what symptom/finding this pattern addresses
    solution - the actual recommendation text
"""

PATTERNS = [
    {
        "id": "caching-hot-endpoint",
        "title": "Cache high-traffic read endpoints",
        "tags": ["slow", "bottleneck", "high traffic", "centrality", "read", "database", "latency"],
        "problem": "A single endpoint or page has high centrality (many things depend on it) and high response time — every request recomputes the same or similar data.",
        "solution": "Add a caching layer (Redis/Memcached) in front of the endpoint, keyed by request parameters. For fully static or slow-changing data, cache at the CDN/edge level instead of hitting the origin at all. Set a TTL matched to how often the underlying data actually changes; even a 30-60s TTL removes most of the load on a hot path.",
    },
    {
        "id": "cache-invalidation",
        "title": "Cache invalidation strategy",
        "tags": ["caching", "cache", "stale data", "consistency"],
        "problem": "Adding a cache without a clear invalidation plan leads to stale data being served after writes.",
        "solution": "Prefer explicit invalidation on write (delete/update the cache key when the underlying record changes) over relying purely on TTL expiry for anything user-facing. For less critical data, a short TTL alone is acceptable. Avoid cache stampede on invalidation by using a lock or serving stale-while-revalidate.",
    },
    {
        "id": "single-point-of-failure",
        "title": "Reduce single points of failure",
        "tags": ["centrality", "single point of failure", "critical", "high blast radius", "impact"],
        "problem": "A node with high betweenness centrality means a large share of the app's traffic/navigation paths pass through it — if it fails, most of the app becomes unreachable.",
        "solution": "Run multiple instances behind a load balancer instead of a single instance. For a critical shared service (e.g. an auth/session service), add a health check and automatic failover. Consider whether the centrality is structurally necessary (e.g. auth genuinely must gate everything) vs. accidental coupling that could be decoupled.",
    },
    {
        "id": "horizontal-scaling",
        "title": "Horizontal scaling for stateless services",
        "tags": ["scale", "traffic", "load", "throughput", "10x", "capacity"],
        "problem": "A service will need to handle significantly more concurrent load than it currently does.",
        "solution": "Ensure the service is stateless (no in-memory session/data that only one instance has) so it can run multiple replicas behind a load balancer, and add autoscaling rules based on CPU/request-rate. Move any session state into a shared store (Redis) so any replica can serve any request.",
    },
    {
        "id": "database-indexing",
        "title": "Database indexing for slow queries",
        "tags": ["slow", "database", "query", "latency", "db"],
        "problem": "A page or endpoint is slow and the underlying operation involves a database lookup, filter, or sort.",
        "solution": "Add an index on the column(s) used in WHERE/ORDER BY/JOIN clauses for the slow query. Verify with EXPLAIN/query plan that the index is actually used. For composite queries, a composite index in the right column order often beats separate single-column indexes.",
    },
    {
        "id": "n-plus-one",
        "title": "N+1 query problem",
        "tags": ["slow", "database", "orm", "loop", "many requests"],
        "problem": "An endpoint's response time scales with the number of related records it returns — one query per item in a list instead of one query total.",
        "solution": "Use eager loading / joins to fetch related data in a single query instead of one query per row (most ORMs support this — e.g. `.include()`, `.select_related()`, `.joinedload()`). This is one of the most common and highest-impact scalability fixes in CRUD-heavy apps.",
    },
    {
        "id": "connection-pooling",
        "title": "Database connection pooling",
        "tags": ["database", "connections", "scale", "concurrent", "exhausted"],
        "problem": "Under concurrent load, a service opens a new database connection per request, which doesn't scale and can exhaust the database's connection limit.",
        "solution": "Use a connection pool (e.g. pgbouncer for Postgres, or the pooling built into most ORMs/drivers) so connections are reused across requests instead of opened and closed each time. Size the pool based on expected concurrency, not guesswork.",
    },
    {
        "id": "rate-limiting",
        "title": "Rate limiting",
        "tags": ["auth", "login", "abuse", "brute force", "public endpoint", "security"],
        "problem": "A public-facing endpoint (especially auth: login, register, password reset) has no limit on request frequency, making it vulnerable to abuse and a potential resource-exhaustion vector at scale.",
        "solution": "Add rate limiting per IP and/or per account on sensitive endpoints (login, register, password reset, any expensive computation endpoint). A sliding-window limiter in Redis is a common, simple implementation. Return 429 with a Retry-After header.",
    },
    {
        "id": "circuit-breaker",
        "title": "Circuit breaker for downstream dependencies",
        "tags": ["cycle", "cycles", "retry", "cascading failure", "downstream", "third-party"],
        "problem": "A cycle exists in API calls, or a service calls a downstream dependency that could become slow/unavailable, risking cascading failures or retry storms.",
        "solution": "Wrap calls to unreliable downstream dependencies in a circuit breaker (e.g. fail fast after N consecutive failures, then periodically retry). This prevents one slow/failing dependency from exhausting threads/connections across the whole system. Combine with exponential backoff on retries, never fixed-interval retry loops.",
    },
    {
        "id": "async-processing",
        "title": "Move slow work to a background queue",
        "tags": ["slow", "long-running", "email", "processing", "upload", "extraction"],
        "problem": "An endpoint does slow synchronous work (sending email, processing a file, calling an external API) inside the request-response cycle, making the user wait and tying up a server thread/connection.",
        "solution": "Move the slow work to a background job queue (e.g. BullMQ, Celery, SQS + workers) and return an immediate response (202 Accepted or a job ID the client can poll). This is especially relevant for endpoints like parsing/extraction, file uploads, or sending notifications.",
    },
    {
        "id": "cdn-static-assets",
        "title": "CDN for static assets",
        "tags": ["slow", "homepage", "load time", "assets", "images", "frontend"],
        "problem": "A page is slow to load, particularly if serving images, JS/CSS bundles, or other static assets directly from the origin server.",
        "solution": "Serve static assets through a CDN (Cloudflare, CloudFront, etc.) instead of the origin server. This reduces origin load and improves latency for geographically distant users. Combine with proper cache-control headers.",
    },
    {
        "id": "pagination",
        "title": "Pagination for list endpoints",
        "tags": ["list", "collection", "large response", "slow", "growing data"],
        "problem": "An endpoint returns an entire collection (e.g. all companies, all applications) with no limit — response size and time will grow unbounded as data grows.",
        "solution": "Add cursor-based or offset-based pagination with a sane default and max page size. Cursor-based pagination scales better than offset-based for large, frequently-changing datasets.",
    },
    {
        "id": "read-replicas",
        "title": "Database read replicas",
        "tags": ["database", "scale", "read-heavy", "reporting", "analytics"],
        "problem": "The database is under heavy read load (e.g. from analytics/dashboard endpoints) competing with write traffic from the main application.",
        "solution": "Add one or more read replicas and route read-heavy, less time-sensitive queries (dashboards, reports, analytics) to them, keeping the primary database free for writes and latency-sensitive reads.",
    },
    {
        "id": "idempotency",
        "title": "Idempotency keys for mutating endpoints",
        "tags": ["retry", "duplicate", "post", "payment", "double-submit"],
        "problem": "A POST/PATCH/DELETE endpoint could be called twice (client retry, network blip, double-click) and cause a duplicate side effect (duplicate record, double charge).",
        "solution": "Accept an idempotency key from the client and store which keys have already been processed, returning the cached result for a repeat request instead of re-executing the action. Critical for any payment or account-creation endpoint.",
    },
    {
        "id": "dead-endpoint-cleanup",
        "title": "Investigate endpoints defined but never observed live",
        "tags": ["repo_defined", "never observed", "dead code", "unused", "undocumented"],
        "problem": "An endpoint exists in the codebase but was never reached during a live crawl of the app's UI.",
        "solution": "This is either (a) dead code that should be removed to reduce attack surface and maintenance burden, (b) an internal/admin route intentionally not linked from the public UI — verify it has proper auth, or (c) an undocumented/untested route that a QA process is missing entirely. Any of these three is worth a deliberate decision, not silence.",
    },
    {
        "id": "monitoring-observability",
        "title": "Add monitoring before scaling blind",
        "tags": ["unknown", "visibility", "monitoring", "metrics", "logging"],
        "problem": "There's no visibility into real production traffic patterns, error rates, or latency distributions — scaling decisions would be guesses.",
        "solution": "Add basic request/error/latency metrics (e.g. via Prometheus + Grafana, or a hosted APM) before making scaling investments. You want to know your actual p95/p99 latency and real traffic distribution across endpoints, not just what a single audit run observed.",
    },
    {
        "id": "session-storage",
        "title": "Externalize session storage",
        "tags": ["auth", "session", "login", "stateless", "scale"],
        "problem": "User sessions are stored in server memory, which prevents horizontal scaling (a user's session only exists on the instance that created it) and loses sessions on restart/deploy.",
        "solution": "Store sessions in a shared store (Redis, or a signed stateless JWT if appropriate for the use case) so any server instance can validate any user's session. This is a prerequisite for horizontal scaling any authenticated service.",
    },
    {
        "id": "graceful-degradation",
        "title": "Graceful degradation for non-critical features",
        "tags": ["dependency", "third-party", "external service", "availability"],
        "problem": "A page's core functionality is coupled to a non-critical third-party call (e.g. analytics, recommendations) — if that call is slow or fails, the whole page fails or hangs.",
        "solution": "Make non-critical calls non-blocking (fire-and-forget, or wrapped in a timeout with a fallback) so a slow/failed dependency degrades that one feature, not the whole page. Reserve synchronous, blocking calls for genuinely critical-path dependencies only.",
    },
    {
        "id": "database-sharding",
        "title": "Database sharding (advanced, later-stage)",
        "tags": ["massive scale", "sharding", "partition", "very large"],
        "problem": "A single database instance is the bottleneck even after indexing, read replicas, and caching — write throughput itself is the limit.",
        "solution": "Consider sharding the database by a logical key (e.g. tenant/user ID) once simpler options (indexing, caching, read replicas) are exhausted. This is a significant architectural change with real operational cost — treat it as a later-stage option, not a first response to a single slow endpoint.",
    },
    {
        "id": "unresolved-mount-prefix",
        "title": "Router organization / explicit mount prefixes",
        "tags": ["duplicate", "collision", "unresolved", "routing", "organization"],
        "problem": "Multiple routers in the codebase mount at ambiguous or colliding paths, making it hard to statically determine the real routing structure (and, per this audit, causing two modules to appear to collide on the same bare path).",
        "solution": "Mount every router with an explicit, unique prefix directly in the main app file (avoid routing through aggregator files that obscure the actual prefix) — this is a maintainability fix as much as a scalability one, since ambiguous routing makes the whole system harder to reason about at scale.",
    },
]


def all_patterns() -> list[dict]:
    return PATTERNS
