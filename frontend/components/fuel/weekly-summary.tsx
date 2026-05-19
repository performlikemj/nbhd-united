"use client";

import { SkelBar } from "@/components/ui/skeleton";
import { useWeeklyVolumeQuery } from "@/lib/queries";
import { CATEGORIES } from "./category-meta";
import type { WorkoutCategory } from "@/lib/types";

export function WeeklySummary() {
  const { data, isPending } = useWeeklyVolumeQuery();

  if (isPending) {
    return (
      <div
        className="rounded-panel border border-border bg-surface-elevated p-4 sm:p-5 mb-6"
        role="status"
        aria-busy="true"
        aria-label="Loading weekly summary"
      >
        <div className="flex items-center justify-between mb-3">
          <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">THIS WEEK</div>
          <SkelBar className="h-3 w-32" />
        </div>
        <div className="flex flex-wrap gap-3">
          <SkelBar className="h-4 w-20" />
          <SkelBar className="h-4 w-16" />
          <SkelBar className="h-4 w-24" />
        </div>
      </div>
    );
  }
  if (!data) return null;
  if (data.totals.sessions === 0) return null;

  return (
    <div className="rounded-panel border border-border bg-surface-elevated p-4 sm:p-5 mb-6">
      <div className="flex items-center justify-between mb-3">
        <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">THIS WEEK</div>
        <div className="text-xs text-ink-faint font-mono">
          {data.totals.sessions} sessions &middot; {data.totals.minutes} min
        </div>
      </div>
      <div className="flex flex-wrap gap-3">
        {data.by_category.map((c) => {
          const meta = CATEGORIES[c.category as WorkoutCategory];
          if (!meta) return null;
          return (
            <div key={c.category} className="flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-full shrink-0" style={{ background: meta.accent }} />
              <span className="text-xs text-ink-muted">{meta.label}</span>
              <span className="text-xs font-mono text-ink-faint">{c.count}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
