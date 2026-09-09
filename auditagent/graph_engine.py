"""
AuditAgent v1 — Dependency Graph Engine
Takes the JSON report produced by crawler.py and builds an actual dependency
graph of the app: pages and API endpoints as nodes, navigation/calls as edges.
Then computes the graph-theory metrics that power everything downstream:
centrality (single-point-of-failure detection), bottlenecks, cycles, and
impact/blast-radius analysis.

This stays pure code, no LLM — per the project plan, graph construction and
metric computation are deterministic. The Architect agent (next milestone)
only ever receives the *already-computed* results below as structured input.

Usage:
    python graph_engine.py report.json
    python graph_engine.py report.json --output graph_report.json
    python graph_engine.py report.json --visualize graph.png
    python graph_engine.py report.json --impact "https://example.com/"
"""

import argparse
import json
from dataclasses import dataclass, field, asdict

import networkx as nx


SLOW_THRESHOLD_MS = 1500  # same threshold crawler.py uses, kept in sync


@dataclass
class GraphMetrics:
    num_nodes: int = 0
    num_edges: int = 0
    top_central_nodes: list[dict] = field(default_factory=list)
    bottlenecks: list[dict] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)
    navigation_cycle_count: int = 0
    broken_link_nodes: list[str] = field(default_factory=list)
    orphan_api_calls: list[str] = field(default_factory=list)


def api_node_id(method: str, url: str) -> str:
    """API endpoints get their own node namespace (method + url) so
    GET /api/users and DELETE /api/users are correctly treated as different
    nodes — very different blast radius if one of them goes down."""
    return f"[API] {method} {url}"


def build_graph(report: dict) -> nx.DiGraph:
    """Build a directed graph from a crawler.py JSON report.

    Nodes: pages (type='page'), API endpoints (type='api'), and any link
    that was discovered but never crawled — external links, or same-domain
    links beyond max_pages/max_depth (type='uncrawled').

    Edges: 'links_to' (page -> page/uncrawled, from <a href>) and
    'calls' (page -> api, from observed XHR/fetch requests).
    """
    G = nx.DiGraph()

    # First pass: add every crawled page as a proper node with its data.
    for page in report["pages"]:
        G.add_node(
            page["url"],
            type="page",
            status_code=page.get("status_code"),
            response_time_ms=page.get("response_time_ms"),
            error=page.get("error"),
            title=page.get("title"),
            console_error_count=len(page.get("console_errors", [])),
        )

    # Second pass: edges for links_to and calls, adding stub nodes for
    # anything referenced but not itself crawled (external sites, or pages
    # beyond max_pages/max_depth).
    for page in report["pages"]:
        src = page["url"]

        for link in page.get("links_found", []):
            if link not in G:
                G.add_node(link, type="uncrawled")
            G.add_edge(src, link, type="links_to")

        for call in page.get("api_calls", []):
            node_id = api_node_id(call["method"], call["url"])
            if node_id not in G:
                G.add_node(
                    node_id,
                    type="api",
                    method=call["method"],
                    url=call["url"],
                    status_code=call.get("status_code"),
                )
            G.add_edge(src, node_id, type="calls")

    # Broken links become nodes too (or get flagged if already present) —
    # this is what lets "impact analysis" answer "what breaks if this dies."
    for bl in report.get("broken_links", []):
        if bl["url"] in G:
            G.nodes[bl["url"]]["broken"] = True
            G.nodes[bl["url"]]["status_code"] = bl.get("status_code")
        else:
            G.add_node(bl["url"], type="broken_link", broken=True, status_code=bl.get("status_code"))
        if bl["found_on"] in G:
            G.add_edge(bl["found_on"], bl["url"], type="links_to")

    return G


def compute_metrics(G: nx.DiGraph) -> GraphMetrics:
    metrics = GraphMetrics(num_nodes=G.number_of_nodes(), num_edges=G.number_of_edges())

    if G.number_of_nodes() == 0:
        return metrics

    betweenness = nx.betweenness_centrality(G)

    # Top central nodes = your single-point-of-failure candidates. High
    # betweenness means a lot of the app's traversal paths pass through it.
    ranked = sorted(betweenness.items(), key=lambda kv: -kv[1])
    metrics.top_central_nodes = [
        {"node": node, "betweenness": round(score, 4), "type": G.nodes[node].get("type")}
        for node, score in ranked[:10] if score > 0
    ]

    # Bottlenecks = high centrality AND slow. This is the ranked priority
    # list the Architect agent will act on first.
    bottlenecks = []
    for node, data in G.nodes(data=True):
        rt = data.get("response_time_ms")
        bc = betweenness.get(node, 0)
        if rt and rt > SLOW_THRESHOLD_MS:
            bottlenecks.append({
                "node": node,
                "response_time_ms": rt,
                "betweenness": round(bc, 4),
                "type": data.get("type"),
            })
    bottlenecks.sort(key=lambda x: (-x["betweenness"], -x["response_time_ms"]))
    metrics.bottlenecks = bottlenecks

    # Cycles — separated by edge type, because they mean very different
    # things. A links_to cycle (login <-> home <-> register) is completely
    # normal site navigation, not a bug. A calls cycle (API A calls API B
    # calls API A) is a real bug smell and a retry-storm risk at scale.
    # Only the latter gets surfaced as a finding; the former is just a count.
    calls_subgraph = nx.DiGraph(
        (u, v) for u, v, d in G.edges(data=True) if d.get("type") == "calls"
    )
    try:
        metrics.cycles = [cycle for cycle in nx.simple_cycles(calls_subgraph) if len(cycle) > 1]
    except Exception:
        metrics.cycles = []

    nav_cycle_count = 0
    try:
        nav_subgraph = nx.DiGraph(
            (u, v) for u, v, d in G.edges(data=True) if d.get("type") == "links_to"
        )
        nav_cycle_count = sum(1 for c in nx.simple_cycles(nav_subgraph) if len(c) > 1)
    except Exception:
        pass
    metrics.navigation_cycle_count = nav_cycle_count

    # Broken link nodes, surfaced directly from the graph (not just the
    # crawler's flat list) so impact_analysis can be run on any of them.
    metrics.broken_link_nodes = [n for n, d in G.nodes(data=True) if d.get("broken")]

    # API endpoints with no incoming edge from any crawled page — these were
    # observed once but we lost track of which page triggered them (can
    # happen with async calls firing after navigation). Worth flagging as a
    # graph-quality note, not a bug in the app itself.
    metrics.orphan_api_calls = [
        n for n, d in G.nodes(data=True) if d.get("type") == "api" and G.in_degree(n) == 0
    ]

    return metrics


def impact_analysis(G: nx.DiGraph, node: str) -> dict:
    """Blast-radius analysis: if `node` goes down, what else in the app
    depends on it (directly or transitively)? Answers "what breaks if X
    fails" — the exact question the future chatbot layer needs to answer."""
    if node not in G:
        return {"node": node, "found": False, "affected": []}

    # Nodes that depend ON `node` are the ones with a path TO it — i.e.
    # ancestors in the directed graph (pages that call/link to this node).
    affected = sorted(nx.ancestors(G, node))
    return {
        "node": node,
        "found": True,
        "affected_count": len(affected),
        "affected": affected,
    }


def print_summary(metrics: GraphMetrics, G: nx.DiGraph) -> None:
    print(f"\n{'='*60}")
    print("AuditAgent — Dependency Graph Report")
    print(f"{'='*60}")
    print(f"Nodes: {metrics.num_nodes}  |  Edges: {metrics.num_edges}\n")

    if metrics.top_central_nodes:
        print("🎯 Highest-centrality nodes (single-point-of-failure candidates):")
        for item in metrics.top_central_nodes[:5]:
            print(f"  - [{item['type']}] {item['node']}  (betweenness: {item['betweenness']})")
        print()

    if metrics.bottlenecks:
        print(f"🐢 Bottlenecks — high-centrality AND slow ({len(metrics.bottlenecks)}):")
        for b in metrics.bottlenecks[:5]:
            print(f"  - [{b['type']}] {b['node']}  →  {b['response_time_ms']}ms  (betweenness: {b['betweenness']})")
        print()

    if metrics.cycles:
        print(f"🔄 API call cycles detected ({len(metrics.cycles)}) — real bug smell / retry-storm risk:")
        for cycle in metrics.cycles[:5]:
            print(f"  - {' → '.join(cycle)} → (back to start)")
        print()
    elif metrics.navigation_cycle_count > 0:
        print(f"🧭 {metrics.navigation_cycle_count} navigation cycle(s) found (login ↔ home, etc.) — normal, no action needed.\n")

    if metrics.broken_link_nodes:
        print(f"🔗 Broken nodes in graph ({len(metrics.broken_link_nodes)}):")
        for n in metrics.broken_link_nodes[:5]:
            print(f"  - {n}")
        print()

    if metrics.orphan_api_calls:
        print(f"❓ API calls with no traced source page ({len(metrics.orphan_api_calls)}):")
        for n in metrics.orphan_api_calls[:5]:
            print(f"  - {n}")
        print()

    if not any([metrics.top_central_nodes, metrics.bottlenecks, metrics.cycles, metrics.broken_link_nodes]):
        print("✅ Graph built, no major structural issues surfaced in this pass.\n")

    if metrics.orphan_api_calls == [] and metrics.num_nodes == 0:
        print("⚠ Graph is empty — check that the report file has crawl data.\n")


def _short_label(url: str, domain: str) -> str:
    """Full URLs as labels were unreadable clutter on anything but a
    tiny graph — strips the (redundant, same-for-every-node) domain and
    keeps just the path, truncated if still long."""
    label = url.replace(f"https://{domain}", "").replace(f"http://{domain}", "") or "/"
    if len(label) > 28:
        label = label[:26] + "…"
    return label


def visualize(G: nx.DiGraph, output_path: str) -> None:
    """Save a quick visual of the graph — color-coded by node type, sized by
    betweenness centrality, so bottleneck nodes visually stand out.

    Dark background to match the report/frontend's actual theme — this is
    primarily consumed embedded in report_generator.py's HTML output now,
    and a plain white matplotlib default looked jarring against a dark
    page (caught by actually rendering a report and looking at it).

    Enhanced with a legend (colors previously had no explanation anywhere
    near the image itself), smart label truncation (full URLs at every
    node made anything beyond a handful of nodes unreadable — labels are
    now domain-stripped and length-capped, and only drawn for the most
    central nodes on larger graphs rather than everything at once), and
    a distinct highlight ring around slow or broken nodes so problem
    areas are visible at a glance, not just inferable from node color."""
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from urllib.parse import urlparse

    betweenness = nx.betweenness_centrality(G) if G.number_of_nodes() > 0 else {}

    color_map = {
        "page": "#38C6FF",
        "api": "#3DDC97",
        "uncrawled": "#4A5C80",
        "broken_link": "#FF5C6C",
    }
    type_labels = {"page": "Page", "api": "API endpoint", "uncrawled": "Not crawled", "broken_link": "Broken link"}
    node_colors = [color_map.get(G.nodes[n].get("type"), "#4A5C80") for n in G.nodes]
    node_sizes = [300 + betweenness.get(n, 0) * 5000 for n in G.nodes]

    # A node is "flagged" if it's slow or broken — drawn with a distinct
    # ring so problem nodes are visible at a glance, not just inferable
    # from color matching a legend.
    flagged = [
        n for n in G.nodes
        if G.nodes[n].get("broken")
        or (G.nodes[n].get("response_time_ms") or 0) > SLOW_THRESHOLD_MS
    ]

    bg_color = "#0D1526"
    fig = plt.figure(figsize=(14, 10), facecolor=bg_color)
    ax = plt.gca()
    ax.set_facecolor(bg_color)
    pos = nx.spring_layout(G, seed=42, k=0.6, iterations=100)

    nx.draw_networkx_edges(G, pos, alpha=0.35, arrows=True, arrowsize=10, edge_color="#38C6FF")
    nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=node_sizes, alpha=0.95,
                            edgecolors="#060B18", linewidths=1.2)
    if flagged:
        flagged_sizes = [300 + betweenness.get(n, 0) * 5000 + 120 for n in flagged]
        nx.draw_networkx_nodes(G, pos, nodelist=flagged, node_color="none",
                                node_size=flagged_sizes, edgecolors="#FF5C6C", linewidths=2.2)

    # Labels only on the most central nodes once the graph gets large —
    # labeling every single node with its full path was unreadable
    # clutter well before 20+ nodes; a handful of the most important
    # nodes (or everything, for a genuinely small graph) stays legible.
    domain = urlparse(next(iter(G.nodes), "")).netloc
    label_nodes = list(G.nodes) if G.number_of_nodes() <= 15 else sorted(
        G.nodes, key=lambda n: betweenness.get(n, 0), reverse=True
    )[:15]
    labels = {n: _short_label(n, domain) for n in label_nodes}
    nx.draw_networkx_labels(G, pos, labels=labels, font_size=7, font_color="#E7EEFC")

    legend_handles = [mpatches.Patch(color=c, label=type_labels[t]) for t, c in color_map.items()]
    legend_handles.append(mpatches.Patch(facecolor="none", edgecolor="#FF5C6C", linewidth=2, label="Slow or broken"))
    legend = ax.legend(
        handles=legend_handles, loc="upper left", fontsize=8, framealpha=0.9,
        facecolor="#111C33", edgecolor="#38C6FF", labelcolor="#E7EEFC",
    )

    plt.title("AuditAgent — Application Dependency Graph", color="#E7EEFC", fontsize=13)
    if G.number_of_nodes() > 15:
        plt.figtext(0.5, 0.01, f"Showing labels for the {len(label_nodes)} most-connected of {G.number_of_nodes()} nodes",
                    color="#6E85AA", fontsize=8, ha="center")
    plt.axis("off")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, facecolor=bg_color)
    plt.close()
    print(f"Graph visualization saved to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="AuditAgent v1 — build and analyze the dependency graph")
    parser.add_argument("report", help="Path to the JSON report produced by crawler.py")
    parser.add_argument("--output", type=str, default=None, help="Write full graph metrics as JSON to this file")
    parser.add_argument("--visualize", type=str, default=None, help="Save a PNG visualization to this path")
    parser.add_argument("--impact", type=str, default=None, help="Run impact/blast-radius analysis on this node URL")
    args = parser.parse_args()

    with open(args.report) as f:
        report = json.load(f)

    G = build_graph(report)
    metrics = compute_metrics(G)
    print_summary(metrics, G)

    if args.impact:
        result = impact_analysis(G, args.impact)
        print(f"\n💥 Impact analysis for {args.impact}:")
        if not result["found"]:
            print("  Node not found in graph.")
        else:
            print(f"  {result['affected_count']} node(s) would be affected if this fails:")
            for n in result["affected"]:
                print(f"    - {n}")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(asdict(metrics), f, indent=2, default=str)
        print(f"\nFull graph metrics written to {args.output}")

    if args.visualize:
        visualize(G, args.visualize)


if __name__ == "__main__":
    main()
