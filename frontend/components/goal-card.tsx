import Link from "next/link";

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

function stripMarkdown(text: string): string {
  return text
    .replace(/^#{1,6}\s+/gm, "")        // headings
    .replace(/\*\*(.+?)\*\*/g, "$1")     // bold
    .replace(/\*(.+?)\*/g, "$1")         // italic
    .replace(/_(.+?)_/g, "$1")           // italic alt
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1") // links
    .replace(/^[-*]\s+/gm, "")           // list items
    .replace(/^>\s+/gm, "")             // blockquotes
    .replace(/`([^`]+)`/g, "$1")         // inline code
    .replace(/\n{2,}/g, " ")             // collapse blank lines
    .replace(/\n/g, " ")                 // remaining newlines
    .trim();
}

function extractDisplayTitle(goal: HorizonsGoal): string {
  if (goal.title && goal.title.toLowerCase() !== "goals") {
    return goal.title;
  }
  const match = goal.preview.match(/###\s+(.+)/);
  if (match) {
    return match[1].replace(/\*\*/g, "").trim();
  }
  return goal.title || "Untitled Goal";
}

export function GoalCard({ goal }: { goal: HorizonsGoal }) {
  const displayTitle = extractDisplayTitle(goal);
  const cleanPreview = stripMarkdown(goal.preview);

  return (
    <Link
      href={`/journal/goal/${goal.slug}`}
      className="group block rounded-panel border border-border bg-card/95 p-4 transition-colors hover:border-border-strong hover:bg-surface-hover focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-2 md:p-5"
    >
      <article>
        <h3 className="font-display text-lg text-ink md:text-xl">
          {displayTitle}
        </h3>

        {cleanPreview ? (
          <p className="mt-2 line-clamp-3 text-sm text-ink-muted">
            {cleanPreview}
          </p>
        ) : null}

        <div className="mt-3 flex items-center justify-between">
          <span className="font-mono text-xs text-ink-faint">
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
