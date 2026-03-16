import { HorizonsPendingExtraction } from "@/lib/types";

function formatDate(dateStr: string): string {
  const d = new Date(dateStr + "T00:00:00");
  if (Number.isNaN(d.getTime())) return dateStr;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function PendingGoal({
  extraction,
}: {
  extraction: HorizonsPendingExtraction;
}) {
  return (
    <article className="rounded-panel border border-dashed border-border bg-surface/60 p-4 md:p-5">
      <p className="mb-2 text-xs italic text-ink-faint">
        Your assistant noticed&hellip;
      </p>

      <p className="text-sm leading-relaxed text-ink">{extraction.text}</p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span
          className={`rounded-full px-2 py-0.5 font-mono text-[11px] ${
            extraction.kind === "goal"
              ? "bg-sky-50 text-sky-800 dark:bg-sky-900/20 dark:text-sky-300"
              : "bg-slate-100 text-slate-600 dark:bg-slate-800/30 dark:text-slate-400"
          }`}
        >
          {extraction.kind}
        </span>

        <span className="text-xs text-ink-faint">
          {extraction.confidence} confidence
        </span>

        {extraction.source_date ? (
          <span className="font-mono text-xs text-ink-faint">
            from {formatDate(extraction.source_date)}
          </span>
        ) : null}
      </div>

      <p className="mt-3 text-xs text-ink-faint">
        Review in your chat to approve
      </p>
    </article>
  );
}
