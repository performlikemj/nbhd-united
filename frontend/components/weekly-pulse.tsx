import { HorizonsWeeklyDocument, HorizonsWeeklyPulse } from "@/lib/types";

function formatWeekRange(start: string, end: string): string {
  const s = new Date(start + "T00:00:00");
  const e = new Date(end + "T00:00:00");
  const fmt = (d: Date) =>
    d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return `${fmt(s)}\u2013${fmt(e)}`;
}

function deriveWeekRangeFromSlug(slug: string, updatedAt: string): string {
  const parsed = /^\d{4}-\d{2}-\d{2}$/.test(slug) ? new Date(slug + "T00:00:00") : null;
  if (parsed && !Number.isNaN(parsed.getTime())) {
    const end = new Date(parsed);
    end.setDate(end.getDate() + 6);
    const iso = (d: Date) => d.toISOString().slice(0, 10);
    return formatWeekRange(iso(parsed), iso(end));
  }
  return new Date(updatedAt).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

const RATING_EMOJI: Record<string, string> = {
  "thumbs-up": "\uD83D\uDE80",
  "meh": "\uD83E\uDDD8",
  "thumbs-down": "\uD83D\uDD25",
};

export function WeeklyPulse({
  weeks,
  documents = [],
}: {
  weeks: HorizonsWeeklyPulse[];
  documents?: HorizonsWeeklyDocument[];
}) {
  if (weeks.length === 0 && documents.length === 0) {
    return (
      <p className="py-6 text-center text-sm text-ink-muted">
        Your weekly reflections will appear here. The first one arrives this Monday.
      </p>
    );
  }

  if (weeks.length === 0) {
    return (
      <div className="space-y-6">
        {documents.map((doc, index) => {
          const header = deriveWeekRangeFromSlug(doc.slug, doc.updated_at);
          const opacity = Math.max(0.4, 1 - index * 0.2);
          return (
            <div
              key={doc.id}
              className="group cursor-default transition-opacity hover:opacity-100"
              style={{ opacity }}
              aria-label={`Week of ${header}${doc.preview ? `: ${doc.preview}` : ""}`}
            >
              <div className="mb-2 flex items-center justify-between">
                <span className="font-mono text-xs uppercase tracking-widest text-ink-faint">
                  {header}
                </span>
              </div>
              {doc.preview ? (
                <p className="text-sm leading-relaxed text-ink-muted group-hover:text-ink transition-colors line-clamp-3">
                  {doc.preview}
                </p>
              ) : null}
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {weeks.map((week, index) => {
        const emoji = RATING_EMOJI[week.week_rating] ?? "\uD83E\uDDD8";
        const label = `Week of ${formatWeekRange(week.week_start, week.week_end)}${week.top_win ? `: ${week.top_win}` : ""}`;
        const opacity = Math.max(0.4, 1 - index * 0.2);

        return (
          <div
            key={week.week_start}
            className="group cursor-default transition-opacity hover:opacity-100"
            style={{ opacity }}
            aria-label={label}
          >
            <div className="mb-2 flex items-center justify-between">
              <span className="font-mono text-xs uppercase tracking-widest text-ink-faint">
                {formatWeekRange(week.week_start, week.week_end)}
              </span>
              <span className="text-xl" aria-hidden="true">{emoji}</span>
            </div>
            {week.top_win ? (
              <p className="text-sm leading-relaxed text-ink-muted group-hover:text-ink transition-colors">
                {week.top_win}
              </p>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
