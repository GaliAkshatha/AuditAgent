// "Scout" — the crawler-bot mascot. Expression state is driven entirely by
// real pipeline state (job status/step/findings), not a canned loop — the
// point is that it's reacting to something real, not just decorating.

const MOODS = {
  idle: { eyes: "blink", legs: false, speech: null },
  scanning: { eyes: "dart", legs: true, speech: "Scanning…" },
  thinking: { eyes: "focus", legs: false, speech: "Thinking…" },
  worried: { eyes: "wide", legs: false, speech: "Found something…" },
  happy: { eyes: "happy", legs: false, speech: "All clear!" },
  proud: { eyes: "happy", legs: false, speech: "Got it all." },
  error: { eyes: "flat", legs: false, speech: "Hit a snag." },
};

export default function Mascot({ mood = "idle", onClick }) {
  const cfg = MOODS[mood] || MOODS.idle;

  return (
    <button
      type="button"
      className={`mascot mood-${mood}`}
      onClick={onClick}
      aria-label="Chat with Scout about this audit"
    >
      {cfg.speech && <div className="mascot-speech">{cfg.speech}</div>}
      <span className="mascot-chat-hint">💬</span>
      <svg viewBox="0 0 100 100" className="mascot-svg">
        {/* antenna */}
        <line x1="50" y1="18" x2="50" y2="6" stroke="#04101E" strokeWidth="2.5" strokeLinecap="round" />
        <circle cx="50" cy="5" r="4" className="mascot-antenna-tip" />

        {/* body */}
        <ellipse cx="50" cy="52" rx="26" ry="24" fill="var(--accent)" stroke="#04101E" strokeWidth="2.5" />

        {/* legs */}
        <g className="mascot-legs" stroke="#04101E" strokeWidth="2.5" strokeLinecap="round" fill="none">
          <path d="M28 60 L14 52" />
          <path d="M28 68 L14 76" />
          <path d="M72 60 L86 52" />
          <path d="M72 68 L86 76" />
        </g>

        {/* eyes */}
        <g className="mascot-eyes">
          {cfg.eyes === "blink" && (
            <>
              <circle cx="40" cy="48" r="5" fill="white" className="eye" />
              <circle cx="40" cy="48" r="2.4" fill="#04101E" className="pupil" />
              <circle cx="60" cy="48" r="5" fill="white" className="eye" />
              <circle cx="60" cy="48" r="2.4" fill="#04101E" className="pupil" />
            </>
          )}
          {cfg.eyes === "dart" && (
            <>
              <circle cx="40" cy="48" r="5" fill="white" />
              <circle cx="40" cy="48" r="2.4" fill="#04101E" className="pupil-dart" />
              <circle cx="60" cy="48" r="5" fill="white" />
              <circle cx="60" cy="48" r="2.4" fill="#04101E" className="pupil-dart" />
            </>
          )}
          {cfg.eyes === "focus" && (
            <>
              <rect x="35" y="46" width="10" height="4" rx="2" fill="#04101E" />
              <rect x="55" y="46" width="10" height="4" rx="2" fill="#04101E" />
            </>
          )}
          {cfg.eyes === "wide" && (
            <>
              <circle cx="40" cy="48" r="6.5" fill="white" stroke="#04101E" strokeWidth="1.5" />
              <circle cx="40" cy="48" r="3" fill="#04101E" />
              <circle cx="60" cy="48" r="6.5" fill="white" stroke="#04101E" strokeWidth="1.5" />
              <circle cx="60" cy="48" r="3" fill="#04101E" />
            </>
          )}
          {cfg.eyes === "happy" && (
            <>
              <path d="M35 48 Q40 42 45 48" stroke="#04101E" strokeWidth="2.5" fill="none" strokeLinecap="round" />
              <path d="M55 48 Q60 42 65 48" stroke="#04101E" strokeWidth="2.5" fill="none" strokeLinecap="round" />
            </>
          )}
          {cfg.eyes === "flat" && (
            <>
              <line x1="35" y1="48" x2="45" y2="48" stroke="#04101E" strokeWidth="2.5" strokeLinecap="round" />
              <line x1="55" y1="48" x2="65" y2="48" stroke="#04101E" strokeWidth="2.5" strokeLinecap="round" />
            </>
          )}
        </g>

        {/* mouth */}
        {mood === "happy" || mood === "proud" ? (
          <path d="M42 62 Q50 68 58 62" stroke="#04101E" strokeWidth="2.5" fill="none" strokeLinecap="round" />
        ) : mood === "worried" || mood === "error" ? (
          <path d="M42 64 Q50 59 58 64" stroke="#04101E" strokeWidth="2.5" fill="none" strokeLinecap="round" />
        ) : (
          <line x1="44" y1="62" x2="56" y2="62" stroke="#04101E" strokeWidth="2.5" strokeLinecap="round" />
        )}
      </svg>
    </button>
  );
}
