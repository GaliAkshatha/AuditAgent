"""
AuditAgent v1.5 — Merge
Combines the Tester Agent's crawl report (crawler.py) and the Static
Analysis Agent's endpoint list (static_analysis.py) into a single
dependency graph, tagging every node by where it came from:

    source: crawl_observed  — reachable and actually seen live
    source: repo_defined    — exists in code, not observed live
    source: both            — exists in code AND was observed live

The interesting finding is almost always the repo_defined-only set: those
are endpoints your app actually exposes that the crawler never reached —
because they're behind auth, only triggered by a form submission, or
genuinely dead code. This is the "every endpoint" payoff the whole
two-source architecture was built for.

Usage:
    python merge.py report.json endpoints.json
    python merge.py report.json endpoints.json --api-base-url https://suzume.akshathag.in/api
    python merge.py report.json endpoints.json --output merged_graph.json --visualize merged.png
"""

import argparse
import json
from dataclasses import asdict

import networkx as nx

from graph_engine import build_graph, api_node_id, compute_metrics, GraphMetrics, visualize


def merge_repo_endpoints(
    G: nx.DiGraph,
    crawl_start_url: str,
    repo_endpoints: list[dict],
    api_base_url: str | None = None,
) -> dict:
    """Adds repo-defined endpoints into the graph, tagging every node's
    `source`. Returns a summary dict of what was matched vs. net-new.

    IMPORTANT ASSUMPTION: without an explicit --api-base-url, this guesses
    the API lives at the same domain as the crawled site. That's often
    wrong for real apps (API on a subdomain, different port, or a
    monorepo's frontend/backend split, as with the modules/preparation vs.
    modules/auth structure seen here) — pass --api-base-url explicitly for
    an accurate cross-reference. Without it, treat the "never observed"
    list as a hypothesis to verify, not a confirmed fact.
    """
    base = (api_base_url or crawl_start_url).rstrip("/")

    # Tag every existing node as crawl_observed first (this is everything
    # the Tester Agent actually built the graph from). Snapshot this set
    # BEFORE adding any repo endpoints — otherwise two repo endpoints that
    # collide with each other (e.g. two routers with unresolved mount
    # prefixes both defaulting to bare "/") get miscounted as a live match
    # against each other, not against anything the crawler actually saw.
    for _, data in G.nodes(data=True):
        data.setdefault("source", "crawl_observed")
    crawl_observed_ids = set(G.nodes)

    matched, added, skipped_duplicate = 0, 0, 0
    never_observed = []
    seen_repo_node_ids = set()

    for ep in repo_endpoints:
        method = ep["method"]
        path = ep["path"]
        method_label = "ANY" if method == "ANY" else method

        full_url = base + ("" if path.startswith("/") else "/") + path
        node_id = api_node_id(method_label, full_url)

        if node_id in seen_repo_node_ids:
            # Duplicate within the repo scan itself (e.g. an unresolved
            # mount prefix causing two different modules to collide on the
            # same bare path) — not a finding, just skip it.
            skipped_duplicate += 1
            continue
        seen_repo_node_ids.add(node_id)

        if node_id in crawl_observed_ids:
            G.nodes[node_id]["source"] = "both"
            matched += 1
        else:
            G.add_node(
                node_id,
                type="api",
                method=method_label,
                url=full_url,
                source="repo_defined",
                repo_file=ep.get("file"),
                repo_line=ep.get("line"),
                framework=ep.get("framework"),
            )
            added += 1
            never_observed.append({"node": node_id, "method": method_label, "url": full_url})

    return {
        "matched_both": matched,
        "repo_only_added": added,
        "skipped_duplicate": skipped_duplicate,
        "never_observed": never_observed,
        "api_base_url_used": base,
        "assumption_flagged": api_base_url is None,
    }


def print_merge_summary(merge_result: dict, metrics: GraphMetrics) -> None:
    print(f"\n{'='*60}")
    print("AuditAgent — Merged Graph (crawl + repo)")
    print(f"{'='*60}")
    print(f"Nodes: {metrics.num_nodes}  |  Edges: {metrics.num_edges}")
    print(f"API base URL used for cross-reference: {merge_result['api_base_url_used']}")
    if merge_result["assumption_flagged"]:
        print("  ⚠ This was GUESSED (same domain as the crawled site) — pass --api-base-url")
        print("    for an accurate result if your API lives elsewhere.")
    print()

    print(f"✅ Endpoints confirmed both in code AND observed live: {merge_result['matched_both']}")
    print(f"❓ Endpoints defined in code but NEVER observed live: {merge_result['repo_only_added']}")
    if merge_result["skipped_duplicate"]:
        print(f"⚠ {merge_result['skipped_duplicate']} duplicate endpoint(s) skipped — likely routers with an")
        print("  unresolved mount prefix colliding on the same bare path (check static_analysis.py output).")
    print()

    if merge_result["never_observed"]:
        print("These exist in your source code but the crawl never reached them")
        print("(likely behind auth, only triggered by form submission, or dead code):")
        for item in merge_result["never_observed"][:20]:
            print(f"  - {item['method']:<6} {item['url']}")
        if len(merge_result["never_observed"]) > 20:
            print(f"  ... and {len(merge_result['never_observed']) - 20} more")
        print()


def main():
    parser = argparse.ArgumentParser(description="AuditAgent v1.5 — merge crawl data and repo endpoints into one graph")
    parser.add_argument("report", help="Path to crawler.py's JSON report")
    parser.add_argument("endpoints", help="Path to static_analysis.py's JSON report")
    parser.add_argument("--api-base-url", type=str, default=None,
                         help="Base URL where the API actually lives, if different from the crawled site "
                              "(e.g. https://api.example.com or https://example.com/api). "
                              "Without this, the tool guesses the same domain as the crawl, which is "
                              "often wrong for split frontend/backend deployments.")
    parser.add_argument("--output", type=str, default=None, help="Write full merged graph metrics as JSON")
    parser.add_argument("--visualize", type=str, default=None, help="Save a PNG of the merged graph")
    args = parser.parse_args()

    with open(args.report) as f:
        crawl_report = json.load(f)
    with open(args.endpoints) as f:
        static_report = json.load(f)

    G = build_graph(crawl_report)
    merge_result = merge_repo_endpoints(
        G, crawl_report["start_url"], static_report["endpoints"], api_base_url=args.api_base_url
    )
    metrics = compute_metrics(G)
    print_merge_summary(merge_result, metrics)

    if args.output:
        out = {
            "metrics": asdict(metrics),
            "merge_summary": merge_result,
            "concurrency_probe": crawl_report.get("concurrency_probe"),
        }
        with open(args.output, "w") as f:
            json.dump(out, f, indent=2, default=str)
        print(f"Full merged report written to {args.output}")

    if args.visualize:
        visualize(G, args.visualize)


if __name__ == "__main__":
    main()
