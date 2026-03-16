import { HorizonsWeeklyPulse } from "@/lib/types";

function formatWeekRange(start: string, end: string): string {
  const s = new Date(start + "T00:00:00");
  const e = new Date(end + "T00:00:00");
  const fmt = (d: Date) =>
    d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return `${fmt(s)}\u2013${fmt(e)}`;
}

const RATING_CONFIG: Record<string, { emoji: string; bg: string }> = {
  "thumbs-up": { emoji: "\uD83D\uDC4D", bg: "var(--emerald-bg)" },
  "meh": { emoji: "\uD83D\uDE10", bg: "var(--amber-bg)" },
  "thumbs-down": { emoji: "\uD83D\uDC4E", bg: "var(--rose-bg)" },
};

export function WeeklyPulse({ weeks }: { weeks: HorizonsWeeklyPulse[] }) {
  if (weeks.length === 0) {
    return (
      <p className="py-6 text-center text-sm text-ink-faint">
        Complete a weekly review to see your pulse here
      </p>
    );
  }

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 md:gap-4">
      {weeks.map((week) => {
        const config = RATING_CONFIG[week.week_rating] ?? RATING_CONFIG["meh"];
        const label = `Week of ${formatWeekRange(week.week_start, week.week_end)}: ${week.week_rating}${week.top_win ? `, top win: ${week.top_win}` : ""}`;

        return (
          <div
            key={week.week_start}
            className="min-h-[100px] rounded-xl bg-surface p-3 sm:p-4"
            aria-label={label}
          >
            <span className="font-mono text-[11px] uppercase tracking-wider text-ink-faint">
              {formatWeekRange(week.week_start, week.week_end)}
            </span>

            <div
              className="mt-2 flex h-8 w-8 items-center justify-center rounded-full text-base sm:h-10 sm:w-10 sm:text-lg"
              style={{ backgroundColor: config.bg }}
              aria-hidden="true"
            >
              {config.emoji}
            </div>

            {week.top_win ? (
              <p className="mt-2 line-clamp-2 text-sm text-ink-muted">
                {week.top_win}
              </p>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
