/**
 * Open Sky charts: thin and quiet. Near-white strokes, accent only for the
 * latest point / goal met, a dashed hairline goal, no area fills, no glows.
 */

export interface SleepNight {
  label: string; // "M", "T", …
  hours: number | null; // null = no entry
  date: string;
}

export function SleepBars({ nights, goal = 7 }: { nights: SleepNight[]; goal?: number }) {
  const w = 320;
  const h = 96;
  const max = Math.max(goal + 1.5, ...nights.map((n) => n.hours ?? 0));
  const bw = 8;
  const gap = nights.length > 1 ? (w - bw * nights.length) / (nights.length - 1) : 0;
  const y = (v: number) => h - (v / max) * h;
  const described = nights
    .map((n) => `${n.date}: ${n.hours == null ? "no entry" : `${n.hours.toFixed(1)} hours`}`)
    .join("; ");
  return (
    <figure className="m-0 max-w-[440px]">
      <svg viewBox={`0 0 ${w} ${h + 22}`} className="w-full" role="img" aria-label={`Sleep, last ${nights.length} nights. ${described}`}>
        <line x1={0} x2={w} y1={y(goal)} y2={y(goal)} stroke="var(--os-hairline)" strokeDasharray="3 4" strokeWidth={1} />
        <text x={w} y={y(goal) - 5} textAnchor="end" fontSize={10} fill="var(--os-faint)">
          {goal}h goal
        </text>
        {nights.map((n, i) => {
          const x = i * (bw + gap);
          if (n.hours == null) {
            return <rect key={n.date} x={x} y={h - 2} width={bw} height={2} rx={1} fill="var(--os-hairline)" />;
          }
          const met = n.hours >= goal;
          return (
            <rect
              key={n.date}
              x={x}
              y={y(n.hours)}
              width={bw}
              height={h - y(n.hours)}
              rx={4}
              fill={met ? "var(--os-accent)" : "var(--os-ink)"}
              opacity={met ? 0.9 : 0.4}
            />
          );
        })}
        {nights.map((n, i) => (
          <text key={`l-${n.date}`} x={i * (bw + gap) + bw / 2} y={h + 16} textAnchor="middle" fontSize={11} fill="var(--os-faint)">
            {n.label}
          </text>
        ))}
      </svg>
    </figure>
  );
}

export interface WeightPoint {
  date: string;
  kg: number;
}

export function WeightLine({ points }: { points: WeightPoint[] }) {
  const w = 320;
  const h = 96;
  if (points.length === 0) return null;
  const sorted = [...points].sort((a, b) => a.date.localeCompare(b.date));
  const vals = sorted.map((p) => p.kg);
  const lo = Math.min(...vals) - 0.4;
  const hi = Math.max(...vals) + 0.4;
  const x = (i: number) => (sorted.length === 1 ? w / 2 : (i / (sorted.length - 1)) * (w - 8) + 4);
  const y = (v: number) => h - ((v - lo) / (hi - lo || 1)) * (h - 8) - 4;
  const d = sorted.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.kg).toFixed(1)}`).join(" ");
  const last = sorted[sorted.length - 1];
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      className="w-full max-w-[440px]"
      role="img"
      aria-label={`Weight over ${sorted.length} entries, from ${sorted[0].kg.toFixed(1)} to ${last.kg.toFixed(1)} kilograms`}
    >
      <path d={d} fill="none" stroke="var(--os-ink)" strokeOpacity={0.8} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={x(sorted.length - 1)} cy={y(last.kg)} r={3.5} fill="var(--os-accent)" />
    </svg>
  );
}
