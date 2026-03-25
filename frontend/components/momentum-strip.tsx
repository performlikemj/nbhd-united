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
              <span className="text-sm text-ink-muted">
                day streak
              </span>
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

      {/* Activity grid — binary: active (solid) vs inactive (faint) */}
      <div className="flex flex-wrap gap-[3px] sm:flex-nowrap sm:gap-1">
        {days.map((day) => {
          const isActive = day.message_count > 0 || day.has_journal;
          const intensity = Math.min(1, (day.message_count + (day.has_journal ? 2 : 0)) / 8);
          const label = `${formatCellDate(day.date)}: ${day.message_count} message${day.message_count === 1 ? "" : "s"}${day.has_journal ? ", journal entry" : ""}`;

          return (
            <div
              key={day.date}
              title={label}
              aria-label={label}
              className="relative rounded transition-all duration-150"
              style={{
                backgroundColor: "var(--signal)",
                opacity: isActive ? 0.3 + intensity * 0.7 : 0.08,
                width: isActive ? (intensity > 0.5 ? 18 : 16) : 14,
                height: isActive ? (intensity > 0.5 ? 18 : 16) : 14,
                alignSelf: "center",
              }}
            >
              {day.has_journal && isActive ? (
                <span
                  className="absolute left-1/2 top-1/2 block h-[4px] w-[4px] -translate-x-1/2 -translate-y-1/2 rounded-full"
                  style={{ backgroundColor: "var(--accent)", opacity: 0.7 }}
                  aria-hidden="true"
                />
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
