/**
 * Custom recharts tooltip styled to match the design system.
 */
export function ChartTooltip({
  active,
  payload,
  label,
  formatter,
}: {
  active?: boolean;
  payload?: { name: string; value: number; color: string }[];
  label?: string;
  formatter?: (value: number) => string;
}) {
  if (!active || !payload?.length) return null;

  const fmt = formatter ?? ((v: number) => `$${v.toLocaleString()}`);

  return (
    <div className="rounded-panel border border-border bg-surface p-3 shadow-panel text-sm">
      {label ? (
        <p className="mb-1.5 font-mono text-xs text-ink-faint">{label}</p>
      ) : null}
      {payload.map((entry) => (
        <div key={entry.name} className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2 rounded-full"
            style={{ backgroundColor: entry.color }}
            aria-hidden="true"
          />
          <span className="text-ink-muted">{entry.name}:</span>
          <span className="font-mono font-medium text-ink">
            {fmt(entry.value)}
          </span>
        </div>
      ))}
    </div>
  );
}
