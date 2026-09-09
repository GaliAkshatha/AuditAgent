import { useMemo, useState } from "react";

function formatDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export default function HistoryPanel({ rows, mostTested, onToggleMostTested, onOpenReport, toggleLabel, hideToggle }) {
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    if (!query.trim()) return rows;
    const q = query.toLowerCase();
    return rows.filter((r) => r.url.toLowerCase().includes(q));
  }, [rows, query]);

  return (
    <section className="panel">
      <div className="section-head">
        <h2>{mostTested ? "Most-tested URLs" : "Recent runs"}</h2>
        {!hideToggle && (
          <button type="button" className="link-btn" onClick={onToggleMostTested}>
            {toggleLabel || (mostTested ? "Show recent runs" : "Show most-tested")}
          </button>
        )}
      </div>

      {rows.length > 3 && (
        <input
          type="search"
          className="history-search"
          placeholder="Filter by URL…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      )}

      {filtered.length === 0 ? (
        <p className="empty">
          {rows.length === 0 ? "No runs yet — run your first audit above." : "No runs match that filter."}
        </p>
      ) : mostTested ? (
        <table className="history-table">
          <thead>
            <tr><th>URL</th><th>Runs</th><th>Last tested</th></tr>
          </thead>
          <tbody>
            {filtered.map((r, i) => (
              <tr key={i}>
                <td className="url-cell">{r.url}</td>
                <td>{r.run_count}×</td>
                <td>{formatDate(r.last_run)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <table className="history-table">
          <thead>
            <tr><th>URL</th><th>When</th><th></th></tr>
          </thead>
          <tbody>
            {filtered.map((r, i) => (
              <tr
                key={i}
                className={r.output_dir ? "clickable" : ""}
                onClick={() => r.output_dir && onOpenReport(r)}
              >
                <td className="url-cell">{r.url}</td>
                <td>{formatDate(r.started_at)}</td>
                <td>
                  {r.concurrency_concerning && <span className="badge warn">concurrency</span>}
                  {!!r.never_observed_count && <span className="badge warn">{r.never_observed_count} unreached</span>}
                  {!r.concurrency_concerning && !r.never_observed_count && r.success ? (
                    <span className="badge ok">clean</span>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
