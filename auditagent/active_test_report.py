"""
AuditAgent — Active Test Report
A self-contained HTML report for a completed Active Testing run —
findings, and an LLM-generated improvements/scalability section based on
what was actually found. Mirrors report_generator.py's structure and
dark "tech game" visual language, but focused on active-testing results
specifically rather than the passive crawl.
"""

import json

from llm_provider import chat_completion

CLASS_PRIORITY = ["crash", "error", "rejected", "auth", "skipped", "unknown", "ok"]
CLASS_LABELS = {
    "ok": ("OK", "ok"), "auth": ("Auth required", "warn"), "crash": ("Server error", "danger"),
    "rejected": ("Rejected", "warn"), "skipped": ("Skipped", "skip"),
    "error": ("Network error", "warn"), "unknown": ("Unknown", "skip"),
}


def classify_step(step):
    if step.get("skipped_reason"):
        return "skipped"
    if step.get("error"):
        return "error"
    status = step.get("status")
    if status is None:
        return "unknown"
    if status < 300:
        return "ok"
    if status in (401, 403):
        return "auth"
    if status >= 500:
        return "crash"
    return "rejected"


def classify_group(entry):
    classes = {classify_step(s) for s in entry.get("steps", [])}
    for p in CLASS_PRIORITY:
        if p in classes:
            return p
    return "ok"


SUGGESTIONS_PROMPT = """You are a backend engineer reviewing active-testing results for a developer audience. Be extremely concise, short bullet points, 5-15 words each, not paragraphs.

Return ONLY valid JSON, no markdown fences, no preamble, in exactly this shape:
{{
  "improvements": [{{"title": "short label", "detail": "one short line", "severity": "high|medium|low"}}],
  "scalability": [{{"title": "short label", "detail": "one short line"}}]
}}

3-6 items per list. Base this ONLY on the concrete findings below, do not invent generic advice not grounded in what's actually shown. Focus on real server errors, auth patterns, and rejected requests.

FINDINGS:
{findings}
"""


def _findings_summary(report):
    lines = []
    for entry in report:
        cls = classify_group(entry)
        for step in entry.get("steps", []):
            status = step.get("status") or step.get("error") or str(step.get("skipped_reason", ""))[:60]
            lines.append(f"{step.get('method')} {entry.get('resource')} -> {status} ({cls})")
    return "\n".join(lines[:60])


def generate_suggestions(report, provider="gemini"):
    try:
        prompt = SUGGESTIONS_PROMPT.format(findings=_findings_summary(report))
        response = chat_completion(messages=[{"role": "user", "content": prompt}], provider=provider)
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        return json.loads(cleaned.strip())
    except Exception as e:
        return {"improvements": [], "scalability": [], "error": str(e)}


def build_active_test_report_html(url, report, suggestions):
    counts = {}
    for entry in report:
        cls = classify_group(entry)
        counts[cls] = counts.get(cls, 0) + 1

    summary_chips = "".join(
        f'<div class="stat"><div class="value">{counts[k]}</div><div class="label">{CLASS_LABELS[k][0]}</div></div>'
        for k in CLASS_PRIORITY if counts.get(k)
    )

    group_cards = []
    for entry in report:
        cls = classify_group(entry)
        label, tone = CLASS_LABELS[cls]
        rows = "".join(
            f"<tr><td>{s.get('method')}</td><td>{s.get('url')}</td>"
            f"<td>{s.get('status') or s.get('error') or str(s.get('skipped_reason', ''))[:80] or '-'}</td></tr>"
            for s in entry.get("steps", [])
        )
        group_cards.append(
            f'<div class="card {tone}"><h3>{entry.get("resource")} '
            f'<span class="badge {tone}">{label}</span></h3>'
            f'<table><tr><th>Method</th><th>URL</th><th>Result</th></tr>{rows}</table></div>'
        )

    def bullet_list(items, key_severity=False):
        if not items:
            return '<p class="muted">None found.</p>'
        out = []
        for item in items:
            sev = ""
            if key_severity and item.get("severity"):
                sev = f' <span class="badge {item.get("severity","low")}">{item.get("severity","")}</span>'
            out.append(f'<div class="suggestion-item"><strong>{item["title"]}</strong>{sev}<p>{item["detail"]}</p></div>')
        return "".join(out)

    improvements_html = bullet_list(suggestions.get("improvements", []), key_severity=True)
    scalability_html = bullet_list(suggestions.get("scalability", []))

    css = """
  :root {
    --bg: #060B18; --panel: #0D1526; --panel-2: #111C33;
    --border: rgba(76,195,255,0.14); --border-strong: rgba(76,195,255,0.3);
    --ink: #E7EEFC; --muted: #6E85AA;
    --accent: #38C6FF; --accent-2: #7B7CFF; --accent-glow: rgba(56,198,255,0.55);
    --danger: #FF5C6C; --warn: #FFB454; --ok: #3DDC97;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg);
    background-image:
      radial-gradient(circle at 15% 10%, rgba(56,198,255,0.07), transparent 40%),
      radial-gradient(circle at 85% 20%, rgba(123,124,255,0.07), transparent 40%),
      linear-gradient(rgba(255,255,255,0.02) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,0.02) 1px, transparent 1px);
    background-size: auto, auto, 42px 42px, 42px 42px;
    color: var(--ink); margin: 0; padding: 0;
  }
  header {
    background: linear-gradient(180deg, #0A1226, #060B18);
    border-bottom: 1px solid var(--border); padding: 40px 5%; position: relative; overflow: hidden;
  }
  header::after {
    content: ""; position: absolute; top: 0; left: -30%; width: 30%; height: 100%;
    background: linear-gradient(90deg, transparent, rgba(56,198,255,0.06), transparent);
    animation: header-scan 5s linear infinite;
  }
  @keyframes header-scan { from { left: -30%; } to { left: 130%; } }
  header h1 {
    margin: 0 0 8px 0; font-size: 26px; font-weight: 700; position: relative;
    background: linear-gradient(90deg, #EAF6FF, var(--accent));
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  header .url { font-size: 14px; color: var(--muted); font-family: ui-monospace, "SF Mono", Consolas, monospace; position: relative; }
  .stats { display: flex; gap: 12px; margin-top: 20px; flex-wrap: wrap; position: relative; }
  .stat { background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 10px; padding: 10px 16px; }
  .stat .value { font-size: 20px; font-weight: 700; color: var(--accent); }
  .stat .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 2px; }
  main { max-width: 100%; padding: 32px 5%; display: grid; grid-template-columns: 2fr 1fr; gap: 28px; }
  section { margin-bottom: 32px; }
  h2 { font-size: 18px; font-weight: 700; border-bottom: 1px solid var(--border); padding-bottom: 10px; }
  .cards { display: grid; gap: 14px; }
  .card {
    background: linear-gradient(180deg, var(--panel-2), var(--panel));
    border: 1px solid var(--border); border-left: 3px solid var(--muted);
    border-radius: 8px; padding: 14px 18px;
  }
  .card.danger { border-left-color: var(--danger); }
  .card.warn { border-left-color: var(--warn); }
  .card.ok { border-left-color: var(--ok); }
  .card.skip { border-left-color: var(--muted); }
  .card h3 { margin-top: 0; font-size: 14px; display: flex; align-items: center; gap: 10px; }
  .card table { width: 100%; border-collapse: collapse; font-size: 12.5px; margin-top: 8px; }
  .card th { text-align: left; color: var(--muted); font-weight: 600; padding: 4px 8px 4px 0; border-bottom: 1px solid var(--border); }
  .card td { padding: 6px 8px 6px 0; border-bottom: 1px solid rgba(255,255,255,0.04); word-break: break-all; }
  .badge { font-size: 10px; padding: 2px 8px; border-radius: 100px; font-weight: 700; }
  .badge.ok { background: rgba(61,220,151,0.15); color: var(--ok); }
  .badge.warn { background: rgba(255,180,84,0.15); color: var(--warn); }
  .badge.danger { background: rgba(255,92,108,0.15); color: var(--danger); }
  .badge.skip { background: rgba(110,133,170,0.15); color: var(--muted); }
  .badge.high { background: rgba(255,92,108,0.15); color: var(--danger); }
  .badge.medium { background: rgba(255,180,84,0.15); color: var(--warn); }
  .badge.low { background: rgba(110,133,170,0.15); color: var(--muted); }
  .suggestions-panel {
    background: linear-gradient(180deg, var(--panel-2), var(--panel));
    border: 1px solid var(--border); border-radius: 8px; padding: 18px 20px;
  }
  .suggestion-item { margin-bottom: 14px; padding-bottom: 14px; border-bottom: 1px solid var(--border); }
  .suggestion-item:last-child { border-bottom: none; margin-bottom: 0; padding-bottom: 0; }
  .suggestion-item p { margin: 4px 0 0 0; font-size: 12.5px; color: var(--muted); }
  .muted { color: var(--muted); font-size: 13px; }
  footer { text-align: center; color: var(--muted); font-size: 12px; padding: 28px; border-top: 1px solid var(--border); }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
"""

    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
        f"<title>Active Testing Report - {url}</title><style>{css}</style></head><body>"
        f'<header><h1>Active Testing Report</h1><div class="url">{url}</div>'
        f'<div class="stats">{summary_chips}</div></header>'
        f'<main><section><h2>Findings by resource</h2><div class="cards">{"".join(group_cards)}</div></section>'
        f'<div><section><h2>Improvements</h2><div class="suggestions-panel">{improvements_html}</div></section>'
        f'<section><h2>Scalability</h2><div class="suggestions-panel">{scalability_html}</div></section></div>'
        "</main><footer>Generated by AuditAgent</footer></body></html>"
    )
