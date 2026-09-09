"""
AuditAgent — Unified Report
One continuous report that starts existing right after code review and
grows as the person progresses through the flow (deployed audit, then
active testing) — rather than three disconnected reports for three
separate stages. Stored as raw JSON pieces per stage and rendered fresh
on every view, so "the same report" always reflects whatever's actually
been run so far.
"""

import json
import secrets
from datetime import datetime, timezone

import db

CLASS_LABELS = {
    "ok": ("OK", "ok"), "auth": ("Auth required", "warn"), "crash": ("Server error", "danger"),
    "rejected": ("Rejected", "warn"), "skipped": ("Skipped", "skip"),
    "error": ("Network error", "warn"), "unknown": ("Unknown", "skip"),
}
CLASS_PRIORITY = ["crash", "error", "rejected", "auth", "skipped", "unknown", "ok"]


def init_unified_reports_table():
    conn = db.get_connection()
    db.execute(conn, """
        CREATE TABLE IF NOT EXISTS unified_reports (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            repo TEXT,
            url TEXT,
            code_review_json TEXT,
            audit_summary_json TEXT,
            active_test_json TEXT,
            suggestions_json TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def create_report(user_id, repo, code_review_result):
    conn = init_unified_reports_table()
    report_id = secrets.token_urlsafe(8)
    db.execute(
        conn,
        "INSERT INTO unified_reports (id, user_id, repo, code_review_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (report_id, user_id, repo, json.dumps(code_review_result), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return report_id


JSON_COLUMNS = {"code_review", "audit_summary", "active_test", "suggestions"}


def update_report(report_id, **fields):
    """Callers pass plain field names (url, audit_summary, active_test,
    suggestions) — this maps them to the actual _json-suffixed DB columns
    internally, in exactly one place, rather than requiring every call
    site to remember that suffix convention itself. A mismatch here
    previously caused a real bug: a call site used the plain names
    directly against the DB, silently failing to update the right
    column."""
    conn = init_unified_reports_table()
    for key, value in fields.items():
        column = f"{key}_json" if key in JSON_COLUMNS else key
        if key in JSON_COLUMNS and not isinstance(value, str):
            value = json.dumps(value)
        db.execute(conn, f"UPDATE unified_reports SET {column} = ? WHERE id = ?", (value, report_id))
    conn.commit()
    conn.close()


def get_report_row(report_id):
    conn = init_unified_reports_table()
    row = db.execute(
        conn,
        "SELECT user_id, repo, url, code_review_json, audit_summary_json, active_test_json, suggestions_json "
        "FROM unified_reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "user_id": row[0], "repo": row[1], "url": row[2],
        "code_review": json.loads(row[3]) if row[3] else None,
        "audit_summary": json.loads(row[4]) if row[4] else None,
        "active_test": json.loads(row[5]) if row[5] else None,
        "suggestions": json.loads(row[6]) if row[6] else None,
    }


def _classify_step(step):
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


def _classify_group(entry):
    classes = {_classify_step(s) for s in entry.get("steps", [])}
    for p in CLASS_PRIORITY:
        if p in classes:
            return p
    return "ok"


def _section(title, badge, open_by_default, body_html):
    open_attr = "open" if open_by_default else ""
    return (
        f'<details class="report-section" {open_attr}>'
        f'<summary><span>{title}</span>{badge}</summary>'
        f'<div class="report-section-body">{body_html}</div></details>'
    )


def _code_review_section(cr):
    if not cr:
        return _section(
            "1. Code Review", '<span class="badge skip">not run</span>', False,
            '<p class="muted">Skipped for this audit.</p>',
        )
    good = cr.get("good", [])
    bad = cr.get("bad", [])
    badge = f'<span class="badge ok">{len(good)} good</span> <span class="badge warn">{len(bad)} flagged</span>'
    cards = "".join(
        f'<div class="mini-card ok"><strong>Good: {g["title"]}</strong><p>{g["detail"]}</p></div>' for g in good
    ) + "".join(
        f'<div class="mini-card {b.get("severity","medium")}"><strong>Flag: {b["title"]}</strong>'
        f'<span class="badge {b.get("severity","medium")}">{b.get("severity","")}</span>'
        f'<p>{b["detail"]}</p></div>' for b in bad
    )
    files = cr.get("files_reviewed")
    sub = f'<p class="muted small">{files} files scanned.</p>' if files else ""
    return _section("1. Code Review", badge, True, f'{sub}<div class="mini-card-grid">{cards}</div>')


def _audit_section(audit_data):
    if not audit_data:
        return _section(
            "2. Deployed Audit", '<span class="badge skip">not run</span>', False,
            '<p class="muted">Not audited yet — code review only so far.</p>',
        )
    badge = f'<span class="badge {audit_data.get("badge_tone", "ok")}">{audit_data.get("badge_label", "")}</span>'
    return _section("2. Deployed Audit", badge, True, audit_data.get("content_html", ""))


def _active_test_section(active_test):
    if not active_test:
        return _section(
            "3. Active Testing", '<span class="badge skip">not run</span>', False,
            '<p class="muted">Not run yet — this actually calls your mutating endpoints to verify they behave correctly under real requests.</p>',
        )
    report = active_test.get("report", [])
    counts = {}
    for entry in report:
        cls = _classify_group(entry)
        counts[cls] = counts.get(cls, 0) + 1
    badge = " ".join(
        f'<span class="badge {CLASS_LABELS[k][1]}">{counts[k]} {CLASS_LABELS[k][0]}</span>'
        for k in CLASS_PRIORITY if counts.get(k)
    )
    cards = ""
    for entry in report:
        cls = _classify_group(entry)
        label, tone = CLASS_LABELS[cls]
        rows = "".join(
            f"<tr><td>{s.get('method')}</td><td>{s.get('url')}</td>"
            f"<td>{s.get('status') or s.get('error') or str(s.get('skipped_reason',''))[:80] or '-'}</td></tr>"
            for s in entry.get("steps", [])
        )
        cards += (
            f'<div class="mini-card {tone}"><strong>{entry.get("resource")}</strong> '
            f'<span class="badge {tone}">{label}</span>'
            f'<table><tr><th>Method</th><th>URL</th><th>Result</th></tr>{rows}</table></div>'
        )
    return _section("3. Active Testing", badge, True, f'<div class="mini-card-grid">{cards}</div>')


def _suggestions_section(suggestions):
    if not suggestions:
        return _section(
            "4. Improvements & Scalability", '<span class="badge skip">pending</span>', False,
            '<p class="muted">Available once Active Testing completes.</p>',
        )

    def bullets(items, key_severity=False):
        if not items:
            return '<p class="muted">None found.</p>'
        out = []
        for item in items:
            sev = ""
            if key_severity and item.get("severity"):
                sev = f' <span class="badge {item.get("severity","low")}">{item.get("severity","")}</span>'
            out.append(f'<div class="suggestion-item"><strong>{item["title"]}</strong>{sev}<p>{item["detail"]}</p></div>')
        return "".join(out)

    body = (
        "<h4>What to improve</h4>"
        + bullets(suggestions.get("improvements", []), key_severity=True)
        + "<h4>Scalability</h4>"
        + bullets(suggestions.get("scalability", []))
    )
    return _section("4. Improvements & Scalability", '<span class="badge ok">ready</span>', True, body)


REPORT_CSS = """
  :root {
    --bg: #060B18; --panel: #0D1526; --panel-2: #111C33;
    --border: rgba(76,195,255,0.14); --ink: #E7EEFC; --muted: #6E85AA;
    --accent: #38C6FF; --danger: #FF5C6C; --warn: #FFB454; --ok: #3DDC97;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg); color: var(--ink); margin: 0; padding: 0;
    background-image: linear-gradient(rgba(255,255,255,0.02) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,0.02) 1px, transparent 1px);
    background-size: 42px 42px;
  }
  header { background: linear-gradient(180deg, #0A1226, #060B18); border-bottom: 1px solid var(--border); padding: 36px 6%; }
  header h1 {
    margin: 0 0 6px 0; font-size: 24px;
    background: linear-gradient(90deg, #EAF6FF, var(--accent));
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  header .sub { color: var(--muted); font-family: ui-monospace, monospace; font-size: 13px; }
  main { max-width: 100%; margin: 0 auto; padding: 32px 4% 60px; }
  details.report-section {
    background: linear-gradient(180deg, var(--panel-2), var(--panel));
    border: 1px solid var(--border); border-radius: 10px; margin-bottom: 16px; overflow: hidden;
  }
  details.report-section summary {
    cursor: pointer; padding: 16px 20px; font-size: 15px; font-weight: 700;
    display: flex; align-items: center; justify-content: space-between; gap: 10px; list-style: none;
  }
  details.report-section summary::-webkit-details-marker { display: none; }
  details.report-section summary::after { content: "+"; margin-left: auto; color: var(--muted); font-size: 18px; }
  details.report-section[open] summary::after { content: "\\2212"; }
  .report-section-body { padding: 0 20px 20px 20px; }
  .mini-card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 10px; }
  .mini-card {
    background: rgba(255,255,255,0.02); border: 1px solid var(--border); border-left: 3px solid var(--muted);
    border-radius: 8px; padding: 12px 16px; font-size: 13px;
  }
  .mini-card.ok { border-left-color: var(--ok); }
  .mini-card.warn, .mini-card.medium { border-left-color: var(--warn); }
  .mini-card.danger, .mini-card.high { border-left-color: var(--danger); }
  .mini-card p { margin: 4px 0 0 0; color: var(--muted); }
  .mini-card table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }
  .mini-card th { text-align: left; color: var(--muted); padding: 3px 6px 3px 0; }
  .mini-card td { padding: 4px 6px 4px 0; border-top: 1px solid rgba(255,255,255,0.04); word-break: break-all; }
  .stat-row { display: flex; gap: 12px; margin-bottom: 10px; }
  .stat { background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 8px; padding: 8px 14px; }
  .stat .value { font-size: 18px; font-weight: 700; color: var(--accent); }
  .stat .label { font-size: 10px; color: var(--muted); text-transform: uppercase; }
  .badge { font-size: 10px; padding: 2px 8px; border-radius: 100px; font-weight: 700; }
  .badge.ok { background: rgba(61,220,151,0.15); color: var(--ok); }
  .badge.warn, .badge.medium { background: rgba(255,180,84,0.15); color: var(--warn); }
  .badge.danger, .badge.high { background: rgba(255,92,108,0.15); color: var(--danger); }
  .badge.skip, .badge.low { background: rgba(110,133,170,0.15); color: var(--muted); }
  h4 { font-size: 13px; margin: 16px 0 8px 0; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .suggestion-item { margin-bottom: 12px; padding-bottom: 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  .suggestion-item p { margin: 4px 0 0 0; color: var(--muted); }
  .muted { color: var(--muted); font-size: 13px; }
  .muted.small { font-size: 12px; }
  footer { text-align: center; color: var(--muted); font-size: 12px; padding: 24px; }

  /* ---- Embedded report_generator.py content (the rich "Deployed Audit"
     fragment) — these classes are used by report_generator.build_report_content(),
     written here without the "header"-scoping that version uses, since
     the embedding context here is a <details> block, not a <header>. ---- */
  .report-audit-fragment .health-badge {
    display: flex; align-items: center; gap: 16px; margin-bottom: 16px;
    padding: 14px 20px; border-radius: 12px; width: fit-content; position: relative;
    background: rgba(255,255,255,0.03); border: 1px solid var(--border-strong);
  }
  .report-audit-fragment .health-badge.ok { box-shadow: 0 0 26px -10px rgba(61,220,151,0.4); }
  .report-audit-fragment .health-badge.warn { box-shadow: 0 0 26px -10px rgba(255,180,84,0.4); }
  .report-audit-fragment .health-badge.danger { box-shadow: 0 0 26px -10px rgba(255,92,108,0.45); }
  .report-audit-fragment .health-badge-ring { position: relative; width: 56px; height: 56px; flex-shrink: 0; }
  .report-audit-fragment .health-ring-svg { width: 56px; height: 56px; transform: rotate(-90deg); }
  .report-audit-fragment .health-ring-track { fill: none; stroke: rgba(255,255,255,0.08); stroke-width: 5; }
  .report-audit-fragment .health-ring-fill {
    fill: none; stroke-width: 5; stroke-linecap: round;
    stroke-dasharray: 176; transition: stroke-dashoffset 0.6s ease;
  }
  .report-audit-fragment .health-badge.ok .health-ring-fill { stroke: var(--ok); }
  .report-audit-fragment .health-badge.warn .health-ring-fill { stroke: var(--warn); }
  .report-audit-fragment .health-badge.danger .health-ring-fill { stroke: var(--danger); }
  .report-audit-fragment .health-badge-score {
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    font-size: 15px; font-weight: 700; color: var(--ink);
  }
  .report-audit-fragment .health-badge-title { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
  .report-audit-fragment .health-badge-verdict { font-size: 15px; font-weight: 700; margin-top: 2px; }
  .report-audit-fragment .health-badge.ok .health-badge-verdict { color: var(--ok); }
  .report-audit-fragment .health-badge.warn .health-badge-verdict { color: var(--warn); }
  .report-audit-fragment .health-badge.danger .health-badge-verdict { color: var(--danger); }

  .report-audit-fragment .stats { display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
  .report-audit-fragment .stat { background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 8px; padding: 8px 14px; }
  .report-audit-fragment .stat .value { font-size: 18px; font-weight: 700; color: var(--accent); }
  .report-audit-fragment .stat .label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .report-audit-fragment .cards { display: grid; gap: 12px; margin: 0; }
  .report-audit-fragment .card {
    background: rgba(255,255,255,0.02); border: 1px solid var(--border); border-left: 3px solid var(--muted);
    border-radius: 8px; padding: 14px 18px;
  }
  .report-audit-fragment .card.danger { border-left-color: var(--danger); }
  .report-audit-fragment .card.warn { border-left-color: var(--warn); }
  .report-audit-fragment .card.ok { border-left-color: var(--ok); }
  .report-audit-fragment .card h3 { margin-top: 0; font-size: 14px; color: var(--ink); }
  .report-audit-fragment .card table { width: 100%; border-collapse: collapse; font-size: 12.5px; margin-top: 8px; }
  .report-audit-fragment .card th { text-align: left; color: var(--muted); font-weight: 600; padding: 4px 8px 4px 0; border-bottom: 1px solid var(--border); }
  .report-audit-fragment .card td { padding: 6px 8px 6px 0; border-bottom: 1px solid rgba(255,255,255,0.04); word-break: break-all; }
  .report-audit-fragment .card a { color: var(--accent); }
  .report-audit-fragment .graph-img { max-width: 100%; border: 1px solid var(--border); border-radius: 8px; margin-top: 12px; }
  .report-audit-fragment section { margin: 20px 0 0 0; }
  .report-audit-fragment h2 { font-size: 14px; margin: 0 0 10px 0; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
"""


def build_unified_report_html(row):
    sections = (
        _code_review_section(row["code_review"])
        + _audit_section(row["audit_summary"])
        + _active_test_section(row["active_test"])
        + _suggestions_section(row["suggestions"])
    )
    title = row.get("url") or row.get("repo") or "AuditAgent Report"

    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
        f"<title>AuditAgent Report - {title}</title><style>{REPORT_CSS}</style></head><body>"
        f'<header><h1>AuditAgent Report</h1><div class="sub">{title}</div></header>'
        f"<main>{sections}</main>"
        "<footer>Generated by AuditAgent — this report updates as you progress through the audit.</footer>"
        "</body></html>"
    )
