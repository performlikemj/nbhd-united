"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { MarkdownRenderer } from "@/components/markdown-renderer";
import { useHorizonsQuery } from "@/lib/queries";
import { HorizonsGoal } from "@/lib/types";

function formatDate(dateStr: string): string {
  const d = new Date(dateStr);
  if (Number.isNaN(d.getTime())) return dateStr;
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
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

function GoalShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="h-full overflow-y-auto px-4 py-6 sm:px-6 sm:py-8">
      <div className="mx-auto w-full max-w-3xl">{children}</div>
    </div>
  );
}

export default function GoalDetailClient() {
  const pathname = usePathname();
  // usePathname is the reliable source under the static-export fallback;
  // useParams is unpopulated when index.html is served for an un-prerendered
  // slug. Decode because slugs (legacy Document slugs) may be URL-encoded.
  const rawSlug = pathname?.split("/journal/goal/")[1]?.split("/")[0] ?? "";
  const slug = (() => {
    try {
      return decodeURIComponent(rawSlug);
    } catch {
      return rawSlug;
    }
  })();

  const { data, isLoading, error } = useHorizonsQuery();

  const backLink = (
    <Link
      href="/horizons"
      className="inline-flex items-center gap-1.5 text-sm text-accent transition-colors hover:text-accent-hover focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-2"
    >
      &larr; Back to Horizons
    </Link>
  );

  if (isLoading) {
    return (
      <GoalShell>
        {backLink}
        <div className="mt-6 animate-pulse space-y-4">
          <div className="h-8 w-2/3 rounded bg-surface-elevated" />
          <div className="h-4 w-full rounded bg-surface-elevated" />
          <div className="h-4 w-5/6 rounded bg-surface-elevated" />
          <div className="h-4 w-4/6 rounded bg-surface-elevated" />
        </div>
      </GoalShell>
    );
  }

  if (error) {
    return (
      <GoalShell>
        {backLink}
        <div className="glass-card-horizons mt-6 p-5 text-sm text-rose-text">
          Failed to load this goal.{" "}
          {error instanceof Error ? error.message : "Please try again."}
        </div>
      </GoalShell>
    );
  }

  const goal = data?.goals.find((g) => g.slug === slug);

  if (!goal) {
    return (
      <GoalShell>
        {backLink}
        <div className="glass-card-horizons mt-6 p-5 text-center text-sm text-ink-muted">
          We couldn&apos;t find this goal. It may have been completed or
          removed.{" "}
          <Link href="/horizons" className="text-accent underline">
            View your active goals
          </Link>
          .
        </div>
      </GoalShell>
    );
  }

  return (
    <GoalShell>
      {backLink}
      <article className="mt-6 glass-card-horizons border-l-2 border-l-accent p-5 sm:p-8">
        <header>
          <span className="font-mono text-[10px] uppercase tracking-wider text-ink-faint">
            Updated {formatDate(goal.updated_at)}
          </span>
          <h1 className="mt-1 font-headline text-3xl font-bold leading-tight text-ink sm:text-4xl">
            {extractDisplayTitle(goal)}
          </h1>
        </header>
        <div className="mt-6">
          <MarkdownRenderer content={goal.preview} />
        </div>
      </article>
    </GoalShell>
  );
}
