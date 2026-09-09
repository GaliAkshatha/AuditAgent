"""
AuditAgent v4 — Audit Context & Tools
Rebuilds the actual dependency graph from a crawl report (and optionally a
static-analysis endpoint list), then exposes a small set of tools the
chatbot can call: impact analysis, bottleneck lookup, endpoint search, and
pattern retrieval. This is what makes the chatbot answer from the real
graph on demand ("what breaks if X fails?") instead of just reciting a
static report — the whole point of putting a chat layer on top of
infrastructure that already exists (graph + RAG + report data).
"""

import json

from graph_engine import build_graph, compute_metrics, impact_analysis as graph_impact_analysis
from merge import merge_repo_endpoints
from retrieval import TfidfIndex
from corpus import all_patterns


class AuditContext:
    def __init__(self, report_path: str, endpoints_path: str | None = None, api_base_url: str | None = None):
        with open(report_path) as f:
            self.crawl_report = json.load(f)

        self.graph = build_graph(self.crawl_report)

        self.merge_result = None
        if endpoints_path:
            with open(endpoints_path) as f:
                endpoints_report = json.load(f)
            self.merge_result = merge_repo_endpoints(
                self.graph, self.crawl_report["start_url"], endpoints_report["endpoints"], api_base_url=api_base_url
            )

        self.metrics = compute_metrics(self.graph)
        self.concurrency_probe = self.crawl_report.get("concurrency_probe")
        self.index = TfidfIndex(all_patterns())

    # ---- Tools available to the chatbot -----------------------------------

    def tool_impact_analysis(self, node: str) -> dict:
        """What breaks if `node` fails? Fuzzy-matches on substring if the
        exact node ID isn't provided (the model won't usually know exact
        internal node-ID formatting)."""
        resolved = self._resolve_node(node)
        if resolved is None:
            return {"error": f"No node found matching '{node}'. Try search_endpoints first to find the exact name."}
        result = graph_impact_analysis(self.graph, resolved)
        result["resolved_node"] = resolved
        return result

    def tool_get_bottlenecks(self) -> list[dict]:
        """Current bottlenecks: high-centrality AND slow nodes, ranked."""
        return self.metrics.bottlenecks

    def tool_get_top_central_nodes(self) -> list[dict]:
        """Highest-betweenness-centrality nodes — single-point-of-failure candidates."""
        return self.metrics.top_central_nodes

    def tool_search_endpoints(self, fragment: str) -> list[dict]:
        """Find graph nodes (pages or API endpoints) whose URL/ID contains
        this substring, along with their source (crawl_observed /
        repo_defined / both) and known attributes."""
        fragment = fragment.lower()
        matches = []
        for node, data in self.graph.nodes(data=True):
            if fragment in node.lower():
                matches.append({"node": node, **{k: v for k, v in data.items() if v is not None}})
        return matches[:15]

    def tool_get_concurrency_probe(self) -> dict:
        """Result of the sequential-vs-concurrent load probe, if one was run."""
        return self.concurrency_probe or {"note": "No concurrency probe data available for this run."}

    def tool_get_never_observed_endpoints(self) -> dict:
        """Endpoints defined in the repo's source but never reached by the live crawl."""
        if not self.merge_result:
            return {"note": "No repo/static-analysis data was merged into this graph."}
        return {
            "count": len(self.merge_result["never_observed"]),
            "endpoints": self.merge_result["never_observed"][:20],
        }

    def tool_search_patterns(self, query: str) -> list[dict]:
        """Retrieve relevant system-design patterns from the reference corpus."""
        results = self.index.query(query, top_k=3)
        return [{"title": r["title"], "problem": r["problem"], "solution": r["solution"]} for r in results]

    # ---- internal ------------------------------------------------------------

    def _resolve_node(self, node: str) -> str | None:
        if node in self.graph:
            return node
        node_lower = node.lower()
        for n in self.graph.nodes:
            if node_lower in n.lower():
                return n
        return None

    def summary(self) -> dict:
        """A short overview handed to the chatbot as system context at the
        start of the conversation — enough to orient it, not the full graph."""
        return {
            "start_url": self.crawl_report.get("start_url"),
            "pages_visited": self.crawl_report.get("pages_visited"),
            "num_graph_nodes": self.metrics.num_nodes,
            "num_graph_edges": self.metrics.num_edges,
            "top_bottleneck": self.metrics.bottlenecks[0] if self.metrics.bottlenecks else None,
            "top_central_node": self.metrics.top_central_nodes[0] if self.metrics.top_central_nodes else None,
            "never_observed_endpoint_count": len(self.merge_result["never_observed"]) if self.merge_result else 0,
            "concurrency_concerning": bool(self.concurrency_probe and self.concurrency_probe.get("concerning")),
        }


TOOL_DESCRIPTIONS = """Available tools:
- impact_analysis(node: str) - what breaks if this page/endpoint fails (finds nodes that depend on it)
- get_bottlenecks() - current bottlenecks (high-centrality AND slow nodes), ranked
- get_top_central_nodes() - highest-centrality nodes (single-point-of-failure candidates)
- search_endpoints(fragment: str) - find graph nodes whose URL contains this substring
- get_concurrency_probe() - sequential-vs-concurrent load test result, if available
- get_never_observed_endpoints() - endpoints defined in the repo but never reached live
- search_patterns(query: str) - retrieve relevant system-design patterns from the reference corpus"""


def call_tool(context: AuditContext, tool_name: str, args: dict) -> object:
    tool_map = {
        "impact_analysis": lambda: context.tool_impact_analysis(args.get("node", "")),
        "get_bottlenecks": lambda: context.tool_get_bottlenecks(),
        "get_top_central_nodes": lambda: context.tool_get_top_central_nodes(),
        "search_endpoints": lambda: context.tool_search_endpoints(args.get("fragment", "")),
        "get_concurrency_probe": lambda: context.tool_get_concurrency_probe(),
        "get_never_observed_endpoints": lambda: context.tool_get_never_observed_endpoints(),
        "search_patterns": lambda: context.tool_search_patterns(args.get("query", "")),
    }
    fn = tool_map.get(tool_name)
    if fn is None:
        return {"error": f"Unknown tool '{tool_name}'"}
    try:
        return fn()
    except Exception as e:
        return {"error": f"Tool execution failed: {e}"}
