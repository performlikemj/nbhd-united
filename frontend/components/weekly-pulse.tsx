import { HorizonsWeeklyPulse } from "@/lib/types";

function formatWeekRange(start: string, end: string): string {
  const s = new Date(start + "T00:00:00");
  const e = new Date(end + "T00:00:00");
  const fmt = (d: Date) =>
    d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return `${fmt(s)}\u2013${fmt(e)}`;
}

const RATING_CONFIG: Record<string, { emoji: string; label: string; bg: string; text: string }> = {
  "thumbs-up": { emoji: "\uD83D\uDC4D", label: "Good week", bg: "var(--emerald-bg)", text: "var(--emerald-text)" },
  "meh": { emoji: "\uD83D\uDE10", label: "Mixed week", bg: "var(--amber-bg)", text: "var(--amber-text)" },
  "thumbs-down": { emoji: "\uD83D\uDC4E", label: "Tough week", bg: "var(--rose-bg)", text: "var(--rose-text)" },
};

export function WeeklyPulse({ weeks }: { weeks: HorizonsWeeklyPulse[] }) {
  // No structured reviews yet — show encouraging empty state
  if (weeks.length === 0) {
    return (
      <p className="py-6 text-center text-sm text-ink-muted">
        Your weekly reflections will appear here. The first one arrives this Monday.
      </p>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:gap-4">
      {weeks.map((week) => {
        const config = RATING_CONFIG[week.week_rating] ?? RATING_CONFIG["meh"];
        const label = `Week of ${formatWeekRange(week.week_start, week.week_end)}: ${config.label}${week.top_win ? `, top win: ${week.top_win}` : ""}`;

        return (
          <div
            key={week.week_start}
            className="rounded-panel border border-border p-4"
            style={{ backgroundColor: config.bg }}
            aria-label={label}
          >
            <div className="flex items-center gap-2">
              <span className="text-lg" aria-hidden="true">{config.emoji}</span>
              <span className="text-sm font-semibold" style={{ color: config.text }}>
                {config.label}
              </span>
            </div>

            <p className="mt-1 font-mono text-[11px] uppercase tracking-wider text-ink-faint">
              {formatWeekRange(week.week_start, week.week_end)}
            </p>

            {week.top_win ? (
              <p className="mt-2 text-sm leading-relaxed text-ink">
                {week.top_win}
              </p>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
