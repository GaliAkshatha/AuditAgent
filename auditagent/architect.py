"""
AuditAgent v2 — Architect Agent
Takes the merged graph output (merge.py) and turns the structural findings
(bottlenecks, centrality, cycles, repo-defined-but-never-observed endpoints)
into concrete, prioritized scalability recommendations — grounded in the
retrieved pattern corpus (corpus.py + retrieval.py), not generic advice.

This is the one component in the pipeline that actually calls an LLM.
Every prior stage (crawler, graph engine, static analysis, merge) is
deliberately pure code — this agent only receives already-computed,
structured findings, never raw crawl/repo data. That's what keeps token
usage low: a handful of API calls per run, not one per page/endpoint.

Requires an Anthropic API key. Set it via:
    export ANTHROPIC_API_KEY=sk-ant-...     (macOS/Linux)
    $env:ANTHROPIC_API_KEY="sk-ant-..."     (Windows PowerShell)

Usage:
    python architect.py merged_report.json --output recommendations.md
"""

import argparse
import json
import os
import sys
import time

from corpus import all_patterns
from retrieval import TfidfIndex

try:
    from dotenv import load_dotenv
    load_dotenv()  # silently loads .env if present; no-op if the file doesn't exist
except ImportError:
    pass  # python-dotenv is optional — falls back to whatever's already in the shell env


def build_findings(merged: dict) -> list[dict]:
    """Turn the merged graph's metrics into a list of individual findings,
    each with a short query string used to retrieve relevant patterns.
    This is the structured hand-off point — the LLM never sees raw graph
    data, only these pre-digested findings."""
    metrics = merged.get("metrics", {})
    merge_summary = merged.get("merge_summary", {})
    findings = []

    for b in metrics.get("bottlenecks", [])[:5]:
        findings.append({
            "type": "bottleneck",
            "summary": f"{b['type']} node '{b['node']}' is slow ({b['response_time_ms']}ms) "
                       f"and has betweenness centrality {b['betweenness']} — a lot of the app's "
                       f"traversal paths pass through it.",
            "query": f"high centrality bottleneck slow response time {b['node']}",
            "data": b,
        })

    for c in metrics.get("top_central_nodes", [])[:3]:
        # Skip ones already covered as a bottleneck to avoid duplicate findings.
        if any(f["data"].get("node") == c["node"] for f in findings if f["type"] == "bottleneck"):
            continue
        findings.append({
            "type": "high_centrality",
            "summary": f"{c['type']} node '{c['node']}' has the highest betweenness centrality "
                       f"({c['betweenness']}) in the app — single point of failure candidate, even "
                       f"though it isn't currently slow.",
            "query": f"single point of failure high centrality {c['node']}",
            "data": c,
        })

    for cycle in metrics.get("cycles", [])[:3]:
        findings.append({
            "type": "api_cycle",
            "summary": f"Circular API calls detected: {' -> '.join(cycle)} -> (back to start).",
            "query": "cycle detected in API calls retry cascading failure",
            "data": {"cycle": cycle},
        })

    never_observed = merge_summary.get("never_observed", [])
    if never_observed:
        sample = [n["url"] for n in never_observed[:5]]
        findings.append({
            "type": "dead_or_untested_endpoints",
            "summary": f"{len(never_observed)} endpoint(s) are defined in the repo's source code but "
                       f"were never reached by the live crawl — e.g. {', '.join(sample)}"
                       f"{'...' if len(never_observed) > 5 else ''}.",
            "query": "endpoint defined in code never observed live dead code auth",
            "data": {"count": len(never_observed), "sample": sample},
        })

    probe = merged.get("concurrency_probe")
    if probe and probe.get("concerning"):
        findings.append({
            "type": "concurrency_degradation",
            "summary": f"The app's backend gets SLOWER under concurrent load, not just linearly slower: "
                       f"{probe['url']} averaged {probe['sequential_avg_ms']}ms when requests were sent "
                       f"one at a time, but {probe['concurrent_avg_ms']}ms when {probe['sample_size']} "
                       f"requests were sent concurrently — a {probe['degradation_ratio']}x slowdown. "
                       f"This was measured directly with a controlled probe (same page, sequential vs. "
                       f"concurrent), not inferred.",
            "query": "database connection pooling concurrent requests exhausted contention scale",
            "data": probe,
        })

    return findings


def enrich_with_patterns(findings: list[dict], index: TfidfIndex, top_k: int = 2) -> list[dict]:
    for f in findings:
        results = index.query(f["query"], top_k=top_k)
        f["retrieved_patterns"] = [
            {"title": r["title"], "problem": r["problem"], "solution": r["solution"]}
            for r in results
        ]
    return findings


def build_prompt(findings: list[dict], app_url: str) -> str:
    """Constructs the single prompt sent to the Architect model. Findings
    and their retrieved patterns are the only large input — no raw HTML,
    no full graph dump. This is deliberately compact for cost."""
    lines = [
        f"You are a system design reviewer analyzing a web application at {app_url}.",
        "Below are structural findings from an automated audit (a dependency graph built from "
        "crawling the live app and statically parsing its backend routes), each paired with "
        "relevant system-design patterns retrieved from a reference corpus.",
        "",
        "For each finding, write a specific, actionable recommendation. Reference the app's "
        "actual data (URLs, response times, endpoint counts) — do not write generic advice. "
        "Rank your recommendations by priority (highest impact / highest risk first). "
        "End with a short overall summary of the app's biggest scalability risk.",
        "",
        "=== FINDINGS ===",
    ]
    for i, f in enumerate(findings, 1):
        lines.append(f"\n--- Finding {i} ({f['type']}) ---")
        lines.append(f["summary"])
        if f["retrieved_patterns"]:
            lines.append("Relevant patterns:")
            for p in f["retrieved_patterns"]:
                lines.append(f"  - {p['title']}: {p['solution']}")
    return "\n".join(lines)


def call_architect_model(prompt: str, provider: str = "anthropic", model: str | None = None) -> str:
    if provider == "gemini":
        return _call_gemini(prompt, model or "gemini-2.5-flash")
    return _call_anthropic(prompt, model or "claude-sonnet-4-6")


def _call_anthropic(prompt: str, model: str) -> str:
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "The 'anthropic' package isn't installed. Run: pip install anthropic"
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable not set. Get a key from "
            "console.anthropic.com and set it before running this script."
        )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if hasattr(block, "text"))


def _call_gemini(prompt: str, model: str, max_retries: int = 3) -> str:
    try:
        from google import genai
        from google.genai import errors as genai_errors
    except ImportError:
        raise RuntimeError(
            "The 'google-genai' package isn't installed. Run: pip install google-genai"
        )

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) environment variable not set. Get a free "
            "key from Google AI Studio (aistudio.google.com/apikey) and set it before "
            "running this script."
        )

    client = genai.Client(api_key=api_key)

    # The free tier's models (especially Flash) periodically return 503
    # UNAVAILABLE under high demand — a temporary server-side overload, not
    # a problem with the request. Retry with backoff before giving up,
    # rather than crashing the whole run on a transient blip.
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(model=model, contents=prompt)
            return response.text
        except genai_errors.ServerError as e:
            last_error = e
            if attempt < max_retries:
                wait = 2 ** attempt  # 2s, 4s, 8s
                print(f"  Gemini is temporarily overloaded (attempt {attempt}/{max_retries}), "
                      f"retrying in {wait}s...")
                time.sleep(wait)
        except genai_errors.ClientError as e:
            # Not retryable — bad request, auth issue, quota exhausted, etc.
            raise RuntimeError(f"Gemini API error (not retryable): {e}")

    raise RuntimeError(
        f"Gemini's API stayed unavailable after {max_retries} attempts ({last_error}). "
        "This is Google's free-tier model being overloaded, not a bug here — try again "
        "in a few minutes, or use --provider anthropic if you have that key set up."
    )


def main():
    parser = argparse.ArgumentParser(description="AuditAgent v2 — Architect agent: turn graph findings into scalability recommendations")
    parser.add_argument("merged_report", help="Path to merge.py's JSON output")
    parser.add_argument("--app-url", type=str, default=None, help="App URL for context in the prompt (defaults to inferring from data if possible)")
    parser.add_argument("--output", type=str, default="recommendations.md", help="Write the recommendations to this markdown file")
    parser.add_argument("--provider", type=str, default="gemini", choices=["anthropic", "gemini"],
                         help="Which model provider to use (default: gemini — free tier, resets daily via "
                              "Google AI Studio). Pass 'anthropic' for Claude if you have that key set up.")
    parser.add_argument("--model", type=str, default=None,
                         help="Model name override. Defaults: claude-sonnet-4-6 (anthropic) / gemini-2.5-flash (gemini)")
    parser.add_argument("--dry-run", action="store_true", help="Build the prompt and print it without calling the API (useful for reviewing token usage / prompt content first)")
    args = parser.parse_args()

    with open(args.merged_report) as f:
        merged = json.load(f)

    findings = build_findings(merged)
    if not findings:
        print("No findings extracted from the merged report — nothing for the Architect agent to reason about.")
        sys.exit(0)

    index = TfidfIndex(all_patterns())
    findings = enrich_with_patterns(findings, index)

    app_url = args.app_url or merged.get("merge_summary", {}).get("api_base_url_used", "the application")
    prompt = build_prompt(findings, app_url)

    print(f"Built {len(findings)} finding(s), retrieved patterns for each.\n")

    if args.dry_run:
        print("=== PROMPT (dry run, not sent) ===\n")
        print(prompt)
        print(f"\n(approx {len(prompt.split())} words / {len(prompt)} chars)")
        return

    print("Calling Architect model...")
    try:
        result = call_architect_model(prompt, provider=args.provider, model=args.model)
    except RuntimeError as e:
        print(f"\n⚠ {e}")
        print("\nRun with --dry-run to see the prompt that would have been sent, "
              "or set up your API key and try again.")
        sys.exit(1)

    with open(args.output, "w") as f:
        f.write(f"# Scalability Recommendations\n\n{result}\n")

    print(f"\nRecommendations written to {args.output}")
    print(f"\n{'='*60}\n{result}\n{'='*60}")


if __name__ == "__main__":
    main()
