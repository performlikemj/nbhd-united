import type { HorizonsData } from "@/lib/types";

type MoodTrendEntry = HorizonsData["mood_trend"][number];

const ENERGY_LEVEL: Record<string, number> = {
  low: 1,
  medium: 2,
  high: 3,
  "1": 1,
  "2": 2,
  "3": 3,
};

export function MoodTrendSparkline({ entries }: { entries: MoodTrendEntry[] }) {
  const levels = entries
    .map((entry) => ENERGY_LEVEL[String(entry.energy).toLowerCase()])
    .filter((level): level is number => level !== undefined);

  if (levels.length === 0) return null;

  const width = 112;
  const height = 32;
  const padding = 3;
  const xStep = levels.length > 1 ? (width - padding * 2) / (levels.length - 1) : 0;
  const points = levels.map((level, index) => ({
    x: levels.length > 1 ? padding + index * xStep : width / 2,
    y: height - padding - ((level - 1) / 2) * (height - padding * 2),
  }));
  const path = points
    .map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`)
    .join(" ");
  const latestLevel = levels.at(-1) ?? 1;

  return (
    <div className="flex shrink-0 items-center gap-2" title={`Latest energy: ${latestLevel} of 3`}>
      <span className="font-mono text-[10px] uppercase tracking-wider text-ink-faint">
        Energy
      </span>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="h-8 w-28 text-signal"
        role="img"
        aria-label={`Energy trend across ${levels.length} journal ${levels.length === 1 ? "entry" : "entries"}; latest ${latestLevel} of 3`}
      >
        <path
          d={path}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
          opacity="0.8"
        />
        {points.map((point, index) => (
          <circle
            key={`${point.x}:${point.y}:${index}`}
            cx={point.x}
            cy={point.y}
            r={index === points.length - 1 ? 2.5 : 1.5}
            fill="currentColor"
            opacity={index === points.length - 1 ? 1 : 0.55}
          />
        ))}
      </svg>
    </div>
  );
}
