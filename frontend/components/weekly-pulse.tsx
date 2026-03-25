import Link from "next/link";

import { stripMarkdown } from "@/lib/format";
import { HorizonsWeeklyDocument, HorizonsWeeklyPulse } from "@/lib/types";

function formatWeekRange(start: string, end: string): string {
  const s = new Date(start + "T00:00:00");
  const e = new Date(end + "T00:00:00");
  const fmt = (d: Date) =>
    d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return `${fmt(s)}\u2013${fmt(e)}`;
}

function formatDate(dateStr: string): string {
  const d = new Date(dateStr);
  if (Number.isNaN(d.getTime())) return dateStr;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const RATING_CONFIG: Record<string, { emoji: string; bg: string }> = {
  "thumbs-up": { emoji: "\uD83D\uDC4D", bg: "var(--emerald-bg)" },
  "meh": { emoji: "\uD83D\uDE10", bg: "var(--amber-bg)" },
  "thumbs-down": { emoji: "\uD83D\uDC4E", bg: "var(--rose-bg)" },
};

export function WeeklyPulse({
  weeks,
  weeklyDocuments,
}: {
  weeks: HorizonsWeeklyPulse[];
  weeklyDocuments: HorizonsWeeklyDocument[];
}) {
  // If we have structured weekly reviews, show those
  if (weeks.length > 0) {
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

  // Fall back to weekly Documents
  if (weeklyDocuments.length > 0) {
    return (
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:gap-4">
        {weeklyDocuments.map((doc) => (
          <Link
            key={doc.id}
            href={`/journal/weekly/${doc.slug}`}
            className="group block rounded-xl bg-surface p-3 transition-colors hover:bg-surface-hover sm:p-4"
          >
            <span className="font-mono text-[11px] uppercase tracking-wider text-ink-faint">
              {formatDate(doc.updated_at)}
            </span>
            <p className="mt-1 font-display text-sm text-ink">
              {doc.title}
            </p>
            {doc.preview ? (
              <p className="mt-1 line-clamp-2 text-xs text-ink-muted">
                {stripMarkdown(doc.preview)}
              </p>
            ) : null}
          </Link>
        ))}
      </div>
    );
  }

  // Both empty — return null (parent hides the section)
  return null;
}
