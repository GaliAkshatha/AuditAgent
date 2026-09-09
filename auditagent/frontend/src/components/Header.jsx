const NODES = [
  { x: 20, y: 35, r: 4, delay: 0 },
  { x: 90, y: 15, r: 5, delay: 0.3 },
  { x: 160, y: 50, r: 4, delay: 0.6 },
  { x: 230, y: 20, r: 6, delay: 0.9 },
  { x: 300, y: 55, r: 4, delay: 1.2 },
  { x: 370, y: 25, r: 5, delay: 1.5 },
  { x: 440, y: 60, r: 4, delay: 1.8 },
  { x: 500, y: 30, r: 5, delay: 2.1 },
  { x: 560, y: 65, r: 4, delay: 0.4 },
  { x: 60, y: 70, r: 3, delay: 0.7 },
  { x: 200, y: 75, r: 3, delay: 1.0 },
  { x: 340, y: 78, r: 3, delay: 1.3 },
  { x: 470, y: 75, r: 3, delay: 1.6 },
];

const EDGES = [
  [0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6], [6, 7], [7, 8],
  [0, 9], [9, 2], [2, 10], [10, 4], [4, 11], [11, 6], [6, 12], [12, 8],
];

export default function Header({ totalRuns = 0, userEmail, onLogout, onOpenSettings }) {
  return (
    <header className="topbar">
      <div className="scan-sweep" />
      <svg className="graph-motif" viewBox="0 0 580 90" preserveAspectRatio="none" aria-hidden="true">
        {EDGES.map(([a, b], i) => (
          <line
            key={i}
            x1={NODES[a].x} y1={NODES[a].y}
            x2={NODES[b].x} y2={NODES[b].y}
            stroke="rgba(56,198,255,0.15)"
            strokeWidth="1"
          />
        ))}
        {NODES.map((n, i) => (
          <circle
            key={i}
            cx={n.x} cy={n.y} r={n.r}
            fill="var(--accent)"
            className="graph-node"
            style={{ animationDelay: `${n.delay}s` }}
          />
        ))}
      </svg>
      <div className="wrap topbar-inner">
        <div>
          <span className="wordmark">AuditAgent</span>
          <span className="tagline">Crawl it. Read the code. Find what breaks first.</span>
        </div>
        <div className="topbar-right">
          {totalRuns > 0 && (
            <div className="live-stat">
              <span className="live-stat-dot" />
              <span className="live-stat-value">{totalRuns}</span>
              <span className="live-stat-label">audits run</span>
            </div>
          )}
          {userEmail && (
            <div className="user-menu">
              <span className="user-email">{userEmail}</span>
              <button type="button" className="link-btn" onClick={onOpenSettings}>Credentials</button>
              <button type="button" className="link-btn logout-btn" onClick={onLogout}>Log out</button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
