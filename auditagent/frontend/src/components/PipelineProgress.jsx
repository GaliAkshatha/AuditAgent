import { useEffect, useState } from "react";

const STEPS = [
  { key: "crawling", label: "Crawling", detail: "Live app", icon: "🕸️" },
  { key: "analyzing_repo", label: "Analyzing", detail: "Repo source", icon: "🔍" },
  { key: "building_graph", label: "Graphing", detail: "Dependencies", icon: "🕹️" },
  { key: "generating_recommendations", label: "Reasoning", detail: "Recommendations", icon: "🧠" },
  { key: "generating_report", label: "Reporting", detail: "Final output", icon: "📋" },
];

const FLAVOR_TEXT = {
  crawling: ["Following every link it can find…", "Poking at pages like a dungeon map…", "Watching for slow doors and dead ends…"],
  analyzing_repo: ["Reading through your source code…", "Hunting for every defined route…", "Cross-referencing with the live site…"],
  building_graph: ["Connecting the dots…", "Wiring nodes together…", "Charting the dependency graph…"],
  generating_recommendations: ["Consulting the AI oracle…", "Weighing the evidence…", "Drafting the verdict…"],
  generating_report: ["Polishing the final report…", "Assembling everything into one view…", "Almost there…"],
};

function stepState(stepKey, currentStep, status) {
  const order = STEPS.map((s) => s.key);
  const currentIndex = order.indexOf(currentStep);
  const thisIndex = order.indexOf(stepKey);
  if (currentStep === "complete" || thisIndex < currentIndex) return "done";
  if (thisIndex === currentIndex) return status === "failed" ? "failed" : "active";
  return "pending";
}

function useFlavorText(currentStep, status) {
  const [text, setText] = useState("");
  useEffect(() => {
    if (status !== "running") return;
    const options = FLAVOR_TEXT[currentStep] || [];
    if (!options.length) return;
    let i = 0;
    setText(options[0]);
    const timer = setInterval(() => {
      i = (i + 1) % options.length;
      setText(options[i]);
    }, 2200);
    return () => clearInterval(timer);
  }, [currentStep, status]);
  return text;
}

export default function PipelineProgress({ url, currentStep, status, logLines }) {
  const flavorText = useFlavorText(currentStep, status);
  const activeStep = STEPS.find((s) => s.key === currentStep);

  return (
    <section className="panel progress-panel">
      <div className="progress-head">
        <span className="live-dot" data-status={status} />
        <h2 className="mono">{url}</h2>
      </div>

      {status === "running" && (
        <div className="scanner-stage">
          <div className="scanner-rings">
            <span className="scanner-ring r1" />
            <span className="scanner-ring r2" />
            <span className="scanner-ring r3" />
            <span className="scanner-core">{activeStep?.icon || "⚙️"}</span>
          </div>
          <p className="scanner-flavor">{flavorText}</p>
        </div>
      )}

      <div className="flow">
        {STEPS.map((s, i) => {
          const state = stepState(s.key, currentStep, status);
          return (
            <div className="flow-item" key={s.key}>
              <div className={`flow-card ${state}`}>
                {state === "done" && <span className="flow-check">✓</span>}
                {state === "active" && <span className="flow-pulse" />}
                <span className="flow-label">{s.label}</span>
                <span className="flow-detail">{s.detail}</span>
              </div>
              {i < STEPS.length - 1 && <div className={`flow-connector ${state === "done" ? "lit" : ""}`} />}
            </div>
          );
        })}
      </div>

      <div className="terminal">
        <div className="terminal-body">
          {logLines.map((line, i) => (
            <div key={i} className="terminal-line">
              <span className="terminal-prompt">$</span> {line}
            </div>
          ))}
          {status === "running" && <span className="terminal-cursor" />}
        </div>
      </div>
    </section>
  );
}

export { STEPS };
