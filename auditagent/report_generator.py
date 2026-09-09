"""
AuditAgent — Report Generator
Ties everything the pipeline produces (crawl data, graph visualization,
findings, Architect recommendations) into ONE self-contained HTML file —
the actual "deliverable" this whole project was pitched around back at the
start: "a shareable, shortened report link you can drop in a Slack channel
or PR." Until now, that link had nothing real to point at — this closes
that gap.

Self-contained on purpose: the graph PNG is base64-embedded directly into
the HTML, so the single output file works standalone — no broken image
links if you move it, email it, or feed its path straight into the
shortener.

Usage:
    python report_generator.py report.json --output report.html
    python report_generator.py report.json --merged merged.json --graph graph.png --recommendations recommendations.md --output report.html
"""

import argparse
import base64
import json
import os
from datetime import datetime, timezone

try:
    import markdown as md_lib
except ImportError:
    md_lib = None


def load_json(path: str | None) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    with open(path) as f:
        return json.load(f)


def embed_image_base64(path: str | None) -> str | None:
    if not path or not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    ext = os.path.splitext(path)[1].lstrip(".") or "png"
    return f"data:image/{ext};base64,{data}"


def render_markdown(path: str | None) -> str:
    if not path or not os.path.isfile(path):
        return ""
    with open(path) as f:
        text = f.read()

    # architect.py writes its output as "# Scalability Recommendations\n\n<body>",
    # but this report already gives that section its own <h2> header — strip a
    # leading H1 so the title doesn't render twice (caught by actually screenshotting
    # a generated report and looking at it, not just checking the HTML didn't error).
    lines = text.lstrip().split("\n", 1)
    if lines and lines[0].strip().startswith("# "):
        text = lines[1] if len(lines) > 1 else ""

    if md_lib:
        return md_lib.markdown(text, extensions=["extra"])
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<pre>{escaped}</pre>"


def fmt(value, suffix: str = "") -> str:
    return f"{value}{suffix}" if value is not None else "—"


def compute_health_score(concurrency_concerning: bool, top_bottleneck_ms, never_observed_count: int) -> int:
    """Mirrors frontend/src/components/HealthScore.jsx's computeHealthScore()
    exactly — same inputs, same penalties, same bands — so the report you
    share never disagrees with what the dashboard showed for the same run.
    Heuristic and intentionally simple/transparent, not a scientific metric."""
    score = 100
    if concurrency_concerning:
        score -= 20
    if top_bottleneck_ms:
        overage = max(0, top_bottleneck_ms - 1500)
        score -= min(30, round(overage / 300))
    if never_observed_count:
        score -= min(25, never_observed_count)
    return max(5, min(100, score))


def health_band(score: int) -> tuple[str, str]:
    if score >= 85:
        return "Excellent", "ok"
    if score >= 65:
        return "Solid", "ok"
    if score >= 40:
        return "Needs work", "warn"
    return "At risk", "danger"


def build_report_content(
    crawl_report: dict,
    merged: dict,
    graph_image_data_uri: str | None,
    recommendations_html: str,
) -> str:
    """The actual report content — health badge, stats, findings cards,
    graph, recommendations — separated from the full-page HTML shell so
    it can be embedded directly inside another page (unified_report.py's
    "Deployed Audit" section) instead of only ever existing as a
    standalone document. This is what makes the combined report show the
    SAME rich detail as this standalone one, not a watered-down summary
    of it — one source of truth for the content, two possible contexts
    to render it in."""
    start_url = crawl_report.get("start_url", "Unknown app")
    pages_visited = crawl_report.get("pages_visited", "—")
    crawl_duration = crawl_report.get("crawl_duration_s", "—")
    slow_pages = crawl_report.get("slow_pages", [])
    broken_links = crawl_report.get("broken_links", [])
    probe = crawl_report.get("concurrency_probe")

    metrics = merged.get("metrics", {})
    merge_summary = merged.get("merge_summary", {})
    bottlenecks = metrics.get("bottlenecks", [])
    top_central = metrics.get("top_central_nodes", [])
    never_observed = merge_summary.get("never_observed", [])
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Same score, same bands as the live dashboard for this exact run —
    # see compute_health_score()'s docstring for why that consistency matters.
    concurrency_concerning = bool(probe and probe.get("concerning"))
    top_bottleneck_ms = bottlenecks[0]["response_time_ms"] if bottlenecks else None
    health_score = compute_health_score(concurrency_concerning, top_bottleneck_ms, len(never_observed))
    health_label, health_cls = health_band(health_score)

    # ---- Finding cards --------------------------------------------------

    cards = []

    if slow_pages:
        rows = "".join(
            f"<tr><td>{p['url']}</td><td>{fmt(p['response_time_ms'], 'ms')}</td></tr>"
            for p in slow_pages
        )
        cards.append(f"""
        <div class="card warn">
          <h3>🐢 Slow pages ({len(slow_pages)})</h3>
          <table><tr><th>URL</th><th>Response time</th></tr>{rows}</table>
        </div>""")

    if probe and probe.get("concerning"):
        cards.append(f"""
        <div class="card danger">
          <h3>⚠️ Concurrency degradation detected</h3>
          <p><strong>{probe['url']}</strong> got <strong>{probe['degradation_ratio']}x slower</strong>
          under concurrent load — {fmt(probe['sequential_avg_ms'],'ms')} sequential vs.
          {fmt(probe['concurrent_avg_ms'],'ms')} concurrent (n={probe.get('sample_size','?')}).</p>
          <p class="muted">Common causes: missing DB connection pooling, serverless cold-start
          contention, or per-IP connection throttling.</p>
        </div>""")
    elif probe:
        cards.append(f"""
        <div class="card ok">
          <h3>✅ Concurrency check passed</h3>
          <p>{probe['url']} handled concurrent load fine
          ({fmt(probe['sequential_avg_ms'],'ms')} sequential vs. {fmt(probe['concurrent_avg_ms'],'ms')} concurrent).</p>
        </div>""")

    if bottlenecks:
        rows = "".join(
            f"<tr><td>{b['node']}</td><td>{fmt(b['response_time_ms'],'ms')}</td><td>{b['betweenness']}</td></tr>"
            for b in bottlenecks
        )
        cards.append(f"""
        <div class="card warn">
          <h3>🎯 Bottlenecks (high centrality + slow)</h3>
          <table><tr><th>Node</th><th>Response time</th><th>Betweenness</th></tr>{rows}</table>
        </div>""")

    if top_central:
        rows = "".join(f"<tr><td>{c['node']}</td><td>{c['betweenness']}</td></tr>" for c in top_central[:5])
        cards.append(f"""
        <div class="card">
          <h3>🔗 Highest-centrality nodes</h3>
          <table><tr><th>Node</th><th>Betweenness</th></tr>{rows}</table>
        </div>""")

    if never_observed:
        sample = never_observed[:10]
        rows = "".join(f"<tr><td>{n['method']}</td><td>{n['url']}</td></tr>" for n in sample)
        more = f"<p class='muted'>...and {len(never_observed)-10} more</p>" if len(never_observed) > 10 else ""
        cards.append(f"""
        <div class="card warn">
          <h3>❓ Endpoints defined in code, never observed live ({len(never_observed)})</h3>
          <table><tr><th>Method</th><th>URL</th></tr>{rows}</table>{more}
        </div>""")

    if broken_links:
        rows = "".join(
            f"<tr><td>{b['url']}</td><td>{b.get('status_code','?')}</td><td>{b.get('found_on','')}</td></tr>"
            for b in broken_links
        )
        cards.append(f"""
        <div class="card danger">
          <h3>🔗 Broken links ({len(broken_links)})</h3>
          <table><tr><th>URL</th><th>Status</th><th>Found on</th></tr>{rows}</table>
        </div>""")

    if not cards:
        cards.append('<div class="card ok"><h3>✅ No issues surfaced in this pass</h3></div>')

    graph_section = ""
    if graph_image_data_uri:
        graph_section = f"""
        <section>
          <h2>Dependency Graph</h2>
          <img src="{graph_image_data_uri}" alt="Dependency graph" class="graph-img">
        </section>"""

    return f"""<div class="report-audit-fragment">
<div class="health-badge {health_cls}">
  <div class="health-badge-ring">
    <svg viewBox="0 0 64 64" class="health-ring-svg">
      <circle cx="32" cy="32" r="28" class="health-ring-track" />
      <circle cx="32" cy="32" r="28" class="health-ring-fill" style="stroke-dashoffset: {176 - round(176 * health_score / 100)}" />
    </svg>
    <span class="health-badge-score">{health_score}</span>
  </div>
  <div>
    <div class="health-badge-title">Site Health</div>
    <div class="health-badge-verdict">{health_label}</div>
  </div>
</div>

<div class="stats">
  <div class="stat"><div class="value">{fmt(pages_visited)}</div><div class="label">Pages crawled</div></div>
  <div class="stat"><div class="value">{fmt(crawl_duration,'s')}</div><div class="label">Crawl duration</div></div>
  <div class="stat"><div class="value">{fmt(metrics.get('num_nodes'))}</div><div class="label">Graph nodes</div></div>
  <div class="stat"><div class="value">{len(never_observed)}</div><div class="label">Unreached endpoints</div></div>
</div>

<div class="cards">{"".join(cards)}</div>
{graph_section}
</div>"""


def build_html(
    crawl_report: dict,
    merged: dict,
    graph_image_data_uri: str | None,
    recommendations_html: str,
) -> str:
    """The full standalone page — page shell + CSS around
    build_report_content()'s fragment, plus the Recommendations section
    (kept here only, not in the embedded fragment, since the unified
    report has its own separate Improvements & Scalability section and
    showing LLM suggestions twice would be redundant)."""
    start_url = crawl_report.get("start_url", "Unknown app")
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    content = build_report_content(crawl_report, merged, graph_image_data_uri, recommendations_html)
    recommendations_section = ""
    if recommendations_html:
        recommendations_section = f'<section><h2>Scalability Recommendations</h2><div class="recommendations">{recommendations_html}</div></section>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>AuditAgent Report — {start_url}</title>
<style>
  :root {{
    --bg: #060B18; --panel: #0D1526; --panel-2: #111C33;
    --border: rgba(76,195,255,0.14); --border-strong: rgba(76,195,255,0.3);
    --ink: #E7EEFC; --muted: #6E85AA;
    --accent: #38C6FF; --accent-2: #7B7CFF; --accent-glow: rgba(56,198,255,0.55);
    --danger: #FF5C6C; --warn: #FFB454; --ok: #3DDC97;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg);
    background-image:
      radial-gradient(circle at 15% 10%, rgba(56,198,255,0.07), transparent 40%),
      radial-gradient(circle at 85% 20%, rgba(123,124,255,0.07), transparent 40%),
      linear-gradient(rgba(255,255,255,0.02) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,0.02) 1px, transparent 1px);
    background-size: auto, auto, 42px 42px, 42px 42px;
    color: var(--ink); margin: 0; padding: 0;
  }}
  header {{
    background: linear-gradient(180deg, #0A1226, #060B18);
    border-bottom: 1px solid var(--border);
    color: white; padding: 40px 32px; position: relative; overflow: hidden;
  }}
  header::after {{
    content: ""; position: absolute; top: 0; left: -30%; width: 30%; height: 100%;
    background: linear-gradient(90deg, transparent, rgba(56,198,255,0.06), transparent);
    animation: header-scan 5s linear infinite;
  }}
  @keyframes header-scan {{ from {{ left: -30%; }} to {{ left: 130%; }} }}
  header h1 {{
    margin: 0 0 8px 0; font-size: 28px; font-weight: 700; position: relative;
    background: linear-gradient(90deg, #EAF6FF, var(--accent));
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }}
  header .url {{ font-size: 15px; color: var(--muted); font-family: ui-monospace, "SF Mono", Consolas, monospace; position: relative; }}
  main {{ max-width: 100%; margin: 0 auto; padding: 32px 4%; }}
  section {{ margin-bottom: 40px; }}
  h2 {{ font-size: 19px; font-weight: 700; border-bottom: 1px solid var(--border); padding-bottom: 10px; }}

  .health-badge {{
    display: flex; align-items: center; gap: 16px; margin: 22px 32px 0 32px;
    padding: 14px 20px; border-radius: 12px; width: fit-content; position: relative;
    background: rgba(255,255,255,0.03); border: 1px solid var(--border-strong);
  }}
  .health-badge.ok {{ box-shadow: 0 0 26px -10px rgba(61,220,151,0.4); }}
  .health-badge.warn {{ box-shadow: 0 0 26px -10px rgba(255,180,84,0.4); }}
  .health-badge.danger {{ box-shadow: 0 0 26px -10px rgba(255,92,108,0.45); }}
  .health-badge-ring {{ position: relative; width: 64px; height: 64px; flex-shrink: 0; }}
  .health-ring-svg {{ width: 64px; height: 64px; transform: rotate(-90deg); }}
  .health-ring-track {{ fill: none; stroke: rgba(255,255,255,0.08); stroke-width: 5; }}
  .health-ring-fill {{
    fill: none; stroke-width: 5; stroke-linecap: round;
    stroke-dasharray: 176; transition: stroke-dashoffset 0.6s ease;
  }}
  .health-badge.ok .health-ring-fill {{ stroke: var(--ok); }}
  .health-badge.warn .health-ring-fill {{ stroke: var(--warn); }}
  .health-badge.danger .health-ring-fill {{ stroke: var(--danger); }}
  .health-badge-score {{
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    font-size: 17px; font-weight: 700; color: var(--ink);
  }}
  .health-badge-title {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }}
  .health-badge-verdict {{ font-size: 17px; font-weight: 700; margin-top: 2px; }}
  .health-badge.ok .health-badge-verdict {{ color: var(--ok); }}
  .health-badge.warn .health-badge-verdict {{ color: var(--warn); }}
  .health-badge.danger .health-badge-verdict {{ color: var(--danger); }}

  .stats {{ display: flex; gap: 12px; margin: 22px 32px 0 32px; flex-wrap: wrap; position: relative; }}
  .stat {{ background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 10px; padding: 10px 16px; }}
  .stat .value {{ font-size: 22px; font-weight: 700; color: var(--accent); }}
  .stat .label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 2px; }}
  .cards {{ display: grid; gap: 16px; margin: 32px; }}
  .card {{
    background: linear-gradient(180deg, var(--panel-2), var(--panel));
    border: 1px solid var(--border); border-left: 3px solid var(--muted);
    border-radius: 8px; padding: 16px 20px;
    box-shadow: 0 10px 24px -16px rgba(0,0,0,0.5);
    animation: card-rise 0.5s ease both;
  }}
  @keyframes card-rise {{
    from {{ opacity: 0; transform: translateY(10px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}
  .cards .card:nth-child(1) {{ animation-delay: 0.05s; }}
  .cards .card:nth-child(2) {{ animation-delay: 0.1s; }}
  .cards .card:nth-child(3) {{ animation-delay: 0.15s; }}
  .cards .card:nth-child(4) {{ animation-delay: 0.2s; }}
  .cards .card:nth-child(5) {{ animation-delay: 0.25s; }}
  .card.danger {{ border-left-color: var(--danger); box-shadow: 0 0 20px -8px rgba(255,92,108,0.25); }}
  .card.warn {{ border-left-color: var(--warn); box-shadow: 0 0 20px -8px rgba(255,180,84,0.2); }}
  .card.ok {{ border-left-color: var(--ok); box-shadow: 0 0 20px -8px rgba(61,220,151,0.2); }}
  .card h3 {{ margin-top: 0; font-size: 15px; color: var(--ink); }}
  .card table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }}
  .card th {{ text-align: left; color: var(--muted); font-weight: 600; padding: 4px 8px 4px 0; border-bottom: 1px solid var(--border); }}
  .card td {{ padding: 6px 8px 6px 0; border-bottom: 1px solid rgba(255,255,255,0.04); word-break: break-all; color: var(--ink); }}
  .card a {{ color: var(--accent); }}
  .muted {{ color: var(--muted); font-size: 13px; }}
  .graph-img {{ max-width: 100%; border: 1px solid var(--border); border-radius: 8px; }}
  .recommendations {{
    background: linear-gradient(180deg, var(--panel-2), var(--panel));
    border: 1px solid var(--border); border-radius: 8px; padding: 24px 28px;
  }}
  .recommendations h3 {{ margin-top: 24px; color: var(--accent); }}
  .recommendations h3:first-child {{ margin-top: 0; }}
  .recommendations strong {{ color: var(--ink); }}
  .recommendations code {{ background: rgba(255,255,255,0.06); padding: 1px 5px; border-radius: 4px; font-size: 0.92em; }}
  footer {{ text-align: center; color: var(--muted); font-size: 12px; padding: 28px; border-top: 1px solid var(--border); }}
</style>
</head>
<body>
<header>
  <h1>AuditAgent Report</h1>
  <div class="url">{start_url}</div>
</header>
<main>
  {content}
  {recommendations_section}
</main>
<footer>Generated by AuditAgent · {generated_at}</footer>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="AuditAgent — generate a single shareable HTML report")
    parser.add_argument("report", help="Path to crawler.py's JSON report")
    parser.add_argument("--merged", type=str, default=None, help="Path to merge.py's JSON output (optional — adds graph/endpoint findings)")
    parser.add_argument("--graph", type=str, default=None, help="Path to a graph PNG from graph_engine.py/merge.py --visualize (optional — embeds it)")
    parser.add_argument("--recommendations", type=str, default=None, help="Path to architect.py's markdown output (optional — includes it)")
    parser.add_argument("--output", type=str, default="report.html", help="Output HTML file path (default: report.html)")
    args = parser.parse_args()

    crawl_report = load_json(args.report)
    merged = load_json(args.merged)
    graph_image = embed_image_base64(args.graph)
    recommendations_html = render_markdown(args.recommendations)

    html = build_html(crawl_report, merged, graph_image, recommendations_html)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(args.output) / 1024
    print(f"Report written to {args.output} ({size_kb:.1f} KB)")
    if not args.merged:
        print("  (no --merged provided — graph/endpoint findings omitted)")
    if not args.graph:
        print("  (no --graph provided — dependency graph image omitted)")
    if not args.recommendations:
        print("  (no --recommendations provided — Architect recommendations omitted)")


if __name__ == "__main__":
    main()
