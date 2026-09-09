import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell } from "recharts";
import { computeHealthScore } from "./HealthScore";

function barColor(score) {
  if (score >= 65) return "#3DDC97";
  if (score >= 40) return "#FFB454";
  return "#FF5C6C";
}

export default function ResultsCharts({ summary, historyRows }) {
  const currentScore = computeHealthScore(summary);

  const recent = historyRows.slice(0, 7).reverse().map((r, i) => ({
    name: `#${historyRows.length - i}`,
    score: computeHealthScore(r),
  }));
  const chartData = [...recent, { name: "Now", score: currentScore, isCurrent: true }];

  return (
    <section className="panel results-chart-panel">
      <h3>Health score trend</h3>
      <p className="muted small">This run vs. your last {recent.length} audits of any site.</p>
      <ResponsiveContainer width="100%" height={180}>
        <BarChart data={chartData} margin={{ top: 8, right: 8, left: -20, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" vertical={false} />
          <XAxis dataKey="name" tick={{ fill: "#6E85AA", fontSize: 11 }} axisLine={{ stroke: "rgba(255,255,255,0.1)" }} tickLine={false} />
          <YAxis domain={[0, 100]} tick={{ fill: "#6E85AA", fontSize: 11 }} axisLine={false} tickLine={false} />
          <Tooltip
            contentStyle={{ background: "#0D1526", border: "1px solid rgba(76,195,255,0.3)", borderRadius: 8, fontSize: 12 }}
            labelStyle={{ color: "#E7EEFC" }}
            cursor={{ fill: "rgba(56,198,255,0.06)" }}
          />
          <Bar dataKey="score" radius={[6, 6, 0, 0]}>
            {chartData.map((d, i) => (
              <Cell key={i} fill={barColor(d.score)} fillOpacity={d.isCurrent ? 1 : 0.55} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </section>
  );
}
