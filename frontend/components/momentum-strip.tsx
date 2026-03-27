import { HorizonsMomentumDay } from "@/lib/types";

function formatCellDate(dateStr: string): string {
  const d = new Date(dateStr + "T00:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function MomentumStrip({
  days,
  streak,
}: {
  days: HorizonsMomentumDay[];
  streak: number;
}) {
  const hasActivity = days.some((d) => d.message_count > 0 || d.has_journal);

  // Mark which days are in the current streak (rightmost N consecutive active days)
  const streakDays = new Set<string>();
  for (let i = days.length - 1; i >= 0; i--) {
    const day = days[i];
    if (day.message_count > 0 || day.has_journal) {
      streakDays.add(day.date);
    } else {
      break;
    }
  }

  return (
    <div
      role="img"
      aria-label={`Activity over the last 30 days. Current streak: ${streak} day${streak === 1 ? "" : "s"}.`}
    >
      {/* Streak badge */}
      <div className="mb-4">
        {hasActivity ? (
          streak > 0 ? (
            <div className="inline-flex items-center gap-2 rounded-full border border-signal/20 bg-signal/10 px-4 py-2">
              <span className="text-signal text-sm" aria-hidden="true">{"\u26A1"}</span>
              <span className="text-sm font-bold tracking-tight text-signal">
                {streak} DAY STREAK
              </span>
            </div>
          ) : (
            <span className="text-sm text-ink-muted">No active streak</span>
          )
        ) : (
          <span className="text-sm text-ink-faint">
            Start a conversation to build momentum
          </span>
        )}
      </div>

      {/* Activity grid */}
      <div className="grid grid-cols-10 md:grid-cols-15 lg:grid-cols-30 gap-2">
        {days.map((day) => {
          const isActive = day.message_count > 0 || day.has_journal;
          const inStreak = streakDays.has(day.date);
          const label = `${formatCellDate(day.date)}: ${day.message_count} message${day.message_count === 1 ? "" : "s"}${day.has_journal ? ", journal entry" : ""}${inStreak ? " (in streak)" : ""}`;

          return (
            <div
              key={day.date}
              title={label}
              aria-label={label}
              className={`aspect-square rounded-sm transition-opacity duration-150 ${
                inStreak
                  ? "bg-signal glow-signal"
                  : isActive
                    ? "bg-signal opacity-45"
                    : "bg-surface-elevated opacity-40"
              }`}
            >
              {day.has_journal && isActive ? (
                <span
                  className="flex h-full w-full items-center justify-center"
                  aria-hidden="true"
                >
                  <span
                    className="block h-[4px] w-[4px] rounded-full"
                    style={{ backgroundColor: "var(--accent)", opacity: 0.8 }}
                  />
                </span>
              ) : null}
            </div>
          );
        })}
      </div>

      {/* Legend */}
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-ink-faint">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded-sm bg-signal glow-signal" />
          In streak
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded-sm bg-signal opacity-45" />
          Active
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded-sm bg-surface-elevated opacity-40" />
          No activity
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-1 w-1 rounded-full" style={{ backgroundColor: "var(--accent)" }} />
          Journal entry
        </span>
      </div>
    </div>
  );
}
