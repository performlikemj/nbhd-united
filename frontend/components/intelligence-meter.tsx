/**
 * Power-bar indicator for AI model capability.
 * Renders a segmented 10-bar meter with color coding.
 */

type LevelSpec = number | [number, number];

function resolve(spec: LevelSpec): { fill: number; display: string } {
  if (Array.isArray(spec)) {
    const lo = Math.max(1, Math.min(10, spec[0]));
    const hi = Math.max(1, Math.min(10, spec[1]));
    return { fill: hi, display: `${lo}-${hi}/10` };
  }
  const v = Math.max(1, Math.min(10, spec));
  return { fill: v, display: `${v}/10` };
}

function barColor(fill: number): string {
  if (fill >= 9) return "bg-[var(--status-emerald-text)]";
  if (fill >= 7) return "bg-[var(--status-sky-text)]";
  if (fill >= 5) return "bg-[var(--status-amber-text)]";
  return "bg-[var(--status-rose-text)]";
}

type IntelligenceMeterProps = {
  level: LevelSpec;
  label?: string;
  compact?: boolean;
};

export function IntelligenceMeter({ level, label, compact = false }: IntelligenceMeterProps) {
  const { fill, display } = resolve(level);
  const color = barColor(fill);

  if (compact) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-ink-muted" title={`Intelligence: ${display}`}>
        <span className="flex gap-px">
          {Array.from({ length: 10 }, (_, i) => (
            <span
              key={i}
              className={`inline-block h-2.5 w-1 rounded-sm ${i < fill ? color : "bg-border"}`}
            />
          ))}
        </span>
        <span className="tabular-nums">{display}</span>
      </span>
    );
  }

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">
          {label ?? "Intelligence"}
        </span>
        <span className="text-xs tabular-nums text-ink-muted">{display}</span>
      </div>
      <div className="flex gap-0.5">
        {Array.from({ length: 10 }, (_, i) => (
          <div
            key={i}
            className={`h-1.5 flex-1 rounded-sm ${i < fill ? color : "bg-border"}`}
          />
        ))}
      </div>
    </div>
  );
}

/** Tier-to-level mapping for the models we actually offer. */
export const TIER_INTELLIGENCE: Record<string, LevelSpec> = {
  starter: 6,
  premium: [8, 9],
  byok: 7,
};
