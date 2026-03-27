import Link from "next/link";

import { stripMarkdown } from "@/lib/format";
import { HorizonsGoal } from "@/lib/types";

function formatDate(dateStr: string): string {
  const d = new Date(dateStr);
  if (Number.isNaN(d.getTime())) return dateStr;
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: new Date().getFullYear() === d.getFullYear() ? undefined : "numeric",
  });
}

function extractDisplayTitle(goal: HorizonsGoal): string {
  if (goal.title && goal.title.toLowerCase() !== "goals") {
    return goal.title;
  }
  // Look for the first ### heading in the preview
  const match = goal.preview.match(/###\s+(.+)/);
  if (match) {
    return match[1].replace(/\*\*/g, "").trim();
  }
  return goal.title || "Untitled Goal";
}

function extractGoalPreview(preview: string): string {
  // Remove document-level headings (# Goals, ## Active, ## Completed)
  let text = preview
    .replace(/^#\s+Goals?\s*$/gm, "")
    .replace(/^##\s+(Active|Completed)\s*$/gm, "")
    .trim();

  // Try to extract just the first goal section (up to next ### or end)
  const goalMatch = text.match(/###\s+.+?\n([\s\S]*?)(?=###\s|$)/);
  if (goalMatch) {
    text = goalMatch[1];
  }

  // Remove metadata lines (Added:, Status:, Target:, Why:)
  text = text
    .replace(/^-\s*(Added|Status|Target|Why):.*$/gm, "")
    .trim();

  return stripMarkdown(text);
}

export function GoalCard({ goal }: { goal: HorizonsGoal }) {
  const displayTitle = extractDisplayTitle(goal);
  const cleanPreview = extractGoalPreview(goal.preview);

  return (
    <Link
      href={`/journal/goal/${goal.slug}`}
      className="group block glass-card-horizons border-l-2 border-l-accent p-5 transition-all hover:border-l-accent-hover focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-2 md:p-6"
    >
      <article>
        <h3 className="font-headline font-semibold text-lg leading-tight text-ink">
          {displayTitle}
        </h3>

        {cleanPreview ? (
          <p className="mt-2 line-clamp-3 text-xs text-ink-muted leading-relaxed">
            {cleanPreview}
          </p>
        ) : null}

        <div className="mt-4 flex items-center justify-between">
          <span className="font-mono text-[10px] uppercase tracking-wider text-ink-faint">
            {formatDate(goal.created_at)}
          </span>
          <span className="text-xs text-accent opacity-100 sm:opacity-0 sm:transition-opacity sm:group-hover:opacity-100 sm:group-focus-visible:opacity-100">
            View in Journal &rarr;
          </span>
        </div>
      </article>
    </Link>
  );
}
