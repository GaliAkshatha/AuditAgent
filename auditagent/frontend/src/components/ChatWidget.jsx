import { useEffect, useRef, useState } from "react";
import { sendChatMessage } from "../api";

export default function ChatWidget({ open, onClose, outputDir, provider }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const bodyRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [messages, loading]);

  useEffect(() => {
    if (open && inputRef.current) inputRef.current.focus();
  }, [open]);

  async function handleSend(e) {
    e.preventDefault();
    const question = input.trim();
    if (!question || loading || !outputDir) return;

    setMessages((m) => [...m, { role: "user", content: question }]);
    setInput("");
    setLoading(true);
    try {
      const answer = await sendChatMessage(outputDir, question, provider);
      setMessages((m) => [...m, { role: "assistant", content: answer }]);
    } catch (err) {
      setMessages((m) => [...m, { role: "assistant", content: err.message, error: true }]);
    } finally {
      setLoading(false);
    }
  }

  if (!open) return null;

  return (
    <div className="chat-widget">
      <div className="chat-widget-head">
        <span className="chat-widget-title">
          <span className="chat-live-dot" /> Ask Scout
        </span>
        <button type="button" className="chat-close" onClick={onClose} aria-label="Close chat">✕</button>
      </div>

      <div className="chat-body" ref={bodyRef}>
        {!outputDir ? (
          <p className="chat-empty">Run an audit first — then I can answer real questions about it, like "what breaks if login fails?"</p>
        ) : messages.length === 0 ? (
          <p className="chat-empty">Ask me about this audit — bottlenecks, impact analysis, unreached endpoints, concurrency…</p>
        ) : (
          messages.map((m, i) => (
            <div key={i} className={`chat-msg ${m.role} ${m.error ? "error" : ""}`}>
              {m.content}
            </div>
          ))
        )}
        {loading && (
          <div className="chat-msg assistant loading">
            <span className="chat-dot" /><span className="chat-dot" /><span className="chat-dot" />
          </div>
        )}
      </div>

      <form className="chat-input-row" onSubmit={handleSend}>
        <input
          ref={inputRef}
          type="text"
          placeholder={outputDir ? "Ask a question…" : "Run an audit first…"}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={!outputDir || loading}
        />
        <button type="submit" disabled={!outputDir || loading || !input.trim()}>
          →
        </button>
      </form>
    </div>
  );
}
