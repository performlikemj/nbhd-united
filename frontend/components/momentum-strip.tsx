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
      <div className="flex flex-wrap gap-[2px] sm:flex-nowrap sm:gap-1 md:gap-[3px]">
        {days.map((day) => {
          const intensity = Math.min(
            1,
            (day.message_count + (day.has_journal ? 2 : 0)) / 8,
          );
          const opacity = intensity > 0 ? 0.15 + intensity * 0.85 : 0.08;
          const label = `${formatCellDate(day.date)}: ${day.message_count} message${day.message_count === 1 ? "" : "s"}${day.has_journal ? ", journal entry" : ""}`;

          return (
            <div
              key={day.date}
              title={label}
              aria-label={label}
              className="relative h-3 w-3 rounded-[3px] transition-opacity duration-150 sm:h-[14px] sm:w-[14px] md:h-4 md:w-4"
              style={{
                backgroundColor: "var(--signal)",
                opacity,
              }}
            >
              {day.has_journal && intensity > 0 ? (
                <span
                  className="absolute left-1/2 top-1/2 block h-[3px] w-[3px] -translate-x-1/2 -translate-y-1/2 rounded-full"
                  style={{ backgroundColor: "var(--accent)", opacity: 0.6 }}
                  aria-hidden="true"
                />
              ) : null}
            </div>
          );
        })}
      </div>

      <p className="mt-2 font-mono text-xs text-ink-muted">
        {hasActivity ? (
          streak > 0 ? `${streak}-day streak` : "No active streak"
        ) : (
          <span className="text-ink-faint">
            Start a conversation to build momentum
          </span>
        )}
      </p>
    </div>
  );
}
