import { HorizonsMomentumDay } from "@/lib/types";

export function MomentumStrip({
  days,
  streak,
}: {
  days?: HorizonsMomentumDay[] | null;
  streak?: number | null;
}) {
  const momentumDays = days ?? [];
  const activeDays = momentumDays.filter(
    (day) => day.message_count > 0 || day.has_journal,
  ).length;
  const hasStreak = streak !== null && streak !== undefined;
  const hasActiveDays = momentumDays.length > 0;

  return (
    <div className="flex flex-col gap-3.5">
      {hasStreak || hasActiveDays ? (
        <div className="flex items-center">
          {hasStreak ? (
            <div className="min-w-0 flex-1 text-center">
              <div className="flex items-baseline justify-center gap-1.5 text-signal">
                <svg
                  aria-hidden="true"
                  className="h-3 w-3 shrink-0"
                  viewBox="0 0 24 24"
                  fill="currentColor"
                >
                  <path d="M13.5 2.2c.4 3.1-.7 4.7-2.1 6.3-1.2 1.4-2.4 2.8-2.1 5 .6-1.2 1.5-2.1 2.7-2.8-.2 2.1.8 3.1 2 4.2 1 1 1.8 2.1 1.7 3.8 1.7-1.2 2.8-3.2 2.8-5.5 0-4.3-2.4-8.1-5-11ZM9 7.5c-2.2 1.9-3.5 4.5-3.5 7.3A6.5 6.5 0 0 0 12 21.3c.8 0 1.6-.1 2.3-.4-2.8-1.1-6.7-3.5-5.3-13.4Z" />
                </svg>
                <p className="font-headline text-[30px] font-bold tabular-nums leading-none">
                  {streak}
                </p>
              </div>
              <p className="mt-1 text-xs text-ink-faint">day streak</p>
            </div>
          ) : null}

          {hasStreak && hasActiveDays ? (
            <span
              aria-hidden="true"
              className="h-[34px] w-px shrink-0 bg-border"
            />
          ) : null}

          {hasActiveDays ? (
            <div className="min-w-0 flex-1 text-center">
              <div className="flex items-baseline justify-center gap-1.5 text-accent-hi">
                <svg
                  aria-hidden="true"
                  className="h-3 w-3 shrink-0"
                  viewBox="0 0 24 24"
                  fill="currentColor"
                >
                  <path d="M4 20V10h4v10H4Zm6 0V4h4v16h-4Zm6 0v-7h4v7h-4Z" />
                </svg>
                <p className="font-headline text-[30px] font-bold tabular-nums leading-none">
                  {activeDays}
                </p>
              </div>
              <p className="mt-1 text-xs text-ink-faint">
                active days · 30
              </p>
            </div>
          ) : null}
        </div>
      ) : null}

      <p className="sr-only">
        Active {activeDays} of the last 30 days, current streak {streak ?? 0} days
      </p>
      {momentumDays.length > 0 ? (
        <div
          aria-hidden="true"
          className="flex items-center justify-center gap-[3px]"
        >
          {momentumDays.map((day) => {
            const isActive = day.message_count > 0 || day.has_journal;

            return (
              <span
                key={day.date}
                className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                  isActive ? "bg-accent-hi" : "bg-border"
                }`}
              />
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
