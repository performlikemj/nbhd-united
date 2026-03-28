export function StatCard({
  label,
  value,
  hint,
  tone = "accent",
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "accent" | "signal" | "error";
}) {
  const borderColor = tone === "error" ? "border-rose-text/30" : tone === "signal" ? "border-signal/30" : "border-accent/30";
  const glowClass = tone === "error" ? "hover:shadow-[0_0_20px_rgba(253,164,175,0.1)]" : tone === "signal" ? "hover:shadow-[0_0_20px_rgba(78,205,196,0.1)]" : "hover:shadow-[0_0_20px_rgba(124,107,240,0.1)]";

  return (
    <article className={`glass-card rounded-xl border-t-2 ${borderColor} p-5 transition-transform duration-300 hover:scale-[1.02] ${glowClass}`}>
      <p className="text-xs font-medium uppercase tracking-[0.12em] text-ink-faint">{label}</p>
      <p className="mt-2 font-headline text-2xl font-bold text-ink">{value}</p>
      {hint ? <p className="mt-2 text-xs text-ink-muted">{hint}</p> : null}
    </article>
  );
}
