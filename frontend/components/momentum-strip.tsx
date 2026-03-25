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
      {/* Streak hero */}
      <div className="mb-3 flex items-baseline gap-2">
        {hasActivity ? (
          streak > 0 ? (
            <>
              <span className="text-2xl font-bold" style={{ color: "var(--signal-text)" }}>
                {streak}
              </span>
              <span className="text-sm text-ink-muted">day streak</span>
            </>
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
      <div className="flex flex-wrap items-center gap-[3px] sm:gap-1">
        {days.map((day) => {
          const isActive = day.message_count > 0 || day.has_journal;
          const inStreak = streakDays.has(day.date);
          const label = `${formatCellDate(day.date)}: ${day.message_count} message${day.message_count === 1 ? "" : "s"}${day.has_journal ? ", journal entry" : ""}${inStreak ? " (in streak)" : ""}`;

          // Three tiers: streak (solid), active-outside-streak (medium), inactive (faint)
          let opacity: number;
          if (inStreak) {
            opacity = 0.95;
          } else if (isActive) {
            opacity = 0.45;
          } else {
            opacity = 0.08;
          }

          return (
            <div
              key={day.date}
              title={label}
              aria-label={label}
              className="relative rounded transition-opacity duration-150"
              style={{
                backgroundColor: "var(--signal)",
                opacity,
                width: isActive ? 16 : 12,
                height: isActive ? 16 : 12,
              }}
            >
              {day.has_journal && isActive ? (
                <span
                  className="absolute left-1/2 top-1/2 block h-[4px] w-[4px] -translate-x-1/2 -translate-y-1/2 rounded-full"
                  style={{ backgroundColor: "var(--accent)", opacity: 0.8 }}
                  aria-hidden="true"
                />
              ) : null}
            </div>
          );
        })}
      </div>

      {/* Legend */}
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-ink-faint">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: "var(--signal)", opacity: 0.95 }} />
          In streak
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: "var(--signal)", opacity: 0.45 }} />
          Active
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: "var(--signal)", opacity: 0.08 }} />
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
