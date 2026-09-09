const ICONS = {
  audit: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2v4M12 18v4M4.9 4.9l2.8 2.8M16.3 16.3l2.8 2.8M2 12h4M18 12h4M4.9 19.1l2.8-2.8M16.3 7.7l2.8-2.8" />
    </svg>
  ),
  history: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3.5 2" strokeLinecap="round" />
    </svg>
  ),
  credentials: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <circle cx="8" cy="8" r="4" />
      <path d="M11 11l9 9M17 15l3-3M14 18l2-2" strokeLinecap="round" />
    </svg>
  ),
  logout: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M9 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h4M16 17l5-5-5-5M21 12H9" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
};

const NAV_ITEMS = [
  { id: "audit", label: "New Audit", icon: "audit" },
  { id: "history", label: "History", icon: "history" },
  { id: "credentials", label: "Credentials", icon: "credentials" },
];

export default function TopBar({ view, onNavigate, userEmail, onLogout, totalRuns }) {
  return (
    <header className="topbar game-topbar">
      <div className="scan-sweep" />
      <div className="wrap topbar-inner">
        <button type="button" className="topbar-brand" onClick={() => onNavigate("audit")}>
          <span className="sidebar-logo-dot" />
          <span className="sidebar-wordmark">AuditAgent</span>
        </button>

        <nav className="topbar-nav">
          {NAV_ITEMS.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`topbar-nav-item ${view === item.id ? "active" : ""}`}
              onClick={() => onNavigate(item.id)}
            >
              <span className="sidebar-nav-icon">{ICONS[item.icon]}</span>
              {item.label}
            </button>
          ))}
        </nav>

        <div className="topbar-right">
          {totalRuns > 0 && (
            <div className="live-stat game-xp-badge">
              <span className="live-stat-dot" />
              <span className="live-stat-value">{totalRuns}</span>
              <span className="live-stat-label">XP</span>
            </div>
          )}
          <div className="sidebar-avatar topbar-avatar">{userEmail?.[0]?.toUpperCase() || "?"}</div>
          <button type="button" className="sidebar-logout" onClick={onLogout} title="Log out">
            {ICONS.logout}
          </button>
        </div>
      </div>
    </header>
  );
}
