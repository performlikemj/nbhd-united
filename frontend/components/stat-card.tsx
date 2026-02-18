export function StatCard({
  label,
  value,
  hint,
  tone = "accent",
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "accent" | "signal";
}) {
  const toneClass = tone === "accent" ? "border-accent/30" : "border-signal/35";

  return (
    <article className={`rounded-panel border ${toneClass} bg-surface-elevated p-4`}>
      <p className="font-mono text-xs uppercase tracking-[0.15em] text-ink-muted">{label}</p>
      <p className="mt-3 text-2xl font-semibold text-ink">{value}</p>
      {hint ? <p className="mt-2 text-sm text-ink-muted">{hint}</p> : null}
    </article>
  );
}
