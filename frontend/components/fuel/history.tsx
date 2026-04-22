"use client";

import { useMemo, useState } from "react";

import { useWorkoutsQuery } from "@/lib/queries";
import type { FuelWorkout, WorkoutCategory } from "@/lib/types";
import { CATEGORIES, CATEGORY_IDS } from "./category-meta";

interface HistoryProps {
  onOpenWorkout: (id: string) => void;
}

export function History({ onOpenWorkout }: HistoryProps) {
  const [filter, setFilter] = useState<"all" | WorkoutCategory>("all");
  const { data: workouts, isLoading } = useWorkoutsQuery({ status: "done", limit: 200 });

  const done = useMemo(() => workouts || [], [workouts]);

  const counts = useMemo(() => {
    const c: Record<string, number> = { total: done.length };
    done.forEach((w) => { c[w.category] = (c[w.category] || 0) + 1; });
    return c;
  }, [done]);

  const filtered = filter === "all" ? done : done.filter((w) => w.category === filter);

  if (isLoading) {
    return <div className="text-sm text-ink-faint">Loading workouts...</div>;
  }

  return (
    <div className="space-y-4">
      {/* Filter chips — horizontal scroll on mobile */}
      <div className="overflow-x-auto -mx-4 px-4 sm:mx-0 sm:px-0">
        <div className="flex items-center gap-1.5 min-w-max sm:min-w-0 sm:flex-wrap pb-1 sm:pb-0">
          <button
            onClick={() => setFilter("all")}
            className={`rounded-full min-h-[44px] px-3 py-2 text-[11px] font-bold uppercase tracking-wider transition border whitespace-nowrap ${
              filter === "all" ? "bg-ink text-surface border-ink" : "border-border text-ink-muted hover:text-ink"
            }`}
          >
            ALL <span className="font-mono opacity-60 ml-1">{counts.total}</span>
          </button>
          {CATEGORY_IDS.map((c) => {
            const n = counts[c] || 0;
            if (n === 0) return null;
            const on = filter === c;
            return (
              <button
                key={c}
                onClick={() => setFilter(c)}
                className={`rounded-full min-h-[44px] px-3 py-2 text-[11px] font-bold uppercase tracking-wider transition border flex items-center gap-1.5 whitespace-nowrap ${
                  on ? "text-ink" : "text-ink-muted"
                }`}
                style={on ? { background: `color-mix(in srgb, ${CATEGORIES[c].accent} 20%, transparent)`, borderColor: CATEGORIES[c].accent } : { borderColor: "var(--color-border)" }}
              >
                <span className="h-1.5 w-1.5 rounded-full" style={{ background: CATEGORIES[c].accent }} />
                {CATEGORIES[c].label}
                <span className="font-mono opacity-60">{n}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* List */}
      <div className="space-y-2">
        {filtered.map((w) => (
          <WorkoutRow key={w.id} w={w} onClick={() => onOpenWorkout(w.id)} />
        ))}
        {filtered.length === 0 && (
          <div className="rounded-panel border border-border p-4 sm:p-8 text-center text-sm text-ink-faint">
            No workouts logged yet.
          </div>
        )}
      </div>
    </div>
  );
}

function summaryChips(w: FuelWorkout): string[] {
  const d = w.detail_json || {};
  if (w.category === "strength") {
    const exercises = (d.exercises as { sets: unknown[] }[]) || [];
    const sets = exercises.reduce((a, e) => a + (e.sets?.length || 0), 0);
    return [`${exercises.length} exercises`, `${sets} sets`];
  }
  if (w.category === "cardio") {
    return [
      d.distance_km && `${d.distance_km} km`,
      d.pace && `${d.pace}/km`,
      d.avg_hr && `${d.avg_hr} bpm`,
    ].filter(Boolean) as string[];
  }
  if (w.category === "hiit") {
    return [`${d.rounds || "?"} rounds`, d.peak_hr && `peak ${d.peak_hr}`].filter(Boolean) as string[];
  }
  if (w.category === "calisthenics") {
    return [`${((d.skills as unknown[]) || []).length} skills`];
  }
  return [];
}

function WorkoutRow({ w, onClick }: { w: FuelWorkout; onClick: () => void }) {
  const meta = CATEGORIES[w.category as WorkoutCategory];
  return (
    <button
      onClick={onClick}
      className="w-full rounded-panel border border-border bg-surface-elevated hover:border-border-strong hover:bg-surface-hover transition px-3 sm:px-4 py-3 text-left flex items-center gap-2.5 sm:gap-3 min-h-[44px]"
    >
      <span
        className="shrink-0 h-8 w-8 rounded-lg flex items-center justify-center text-xs font-bold"
        style={{ background: `color-mix(in srgb, ${meta.accent} 15%, transparent)`, color: meta.accent }}
      >
        {meta.label.charAt(0)}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="text-[9px] font-bold uppercase tracking-[0.2em]" style={{ color: meta.accent }}>
            {meta.label.toUpperCase()}
          </span>
        </div>
        <div className="mt-0.5 text-sm text-ink truncate">{w.activity}</div>
        <div className="mt-0.5 text-[11px] text-ink-faint flex flex-wrap gap-x-2">
          {w.duration_minutes && <span>{w.duration_minutes} min</span>}
          {w.rpe != null && <span>&middot; RPE {w.rpe}</span>}
          {summaryChips(w).map((s, i) => <span key={i}>&middot; {s}</span>)}
        </div>
      </div>
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 text-ink-faint" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="m9 18 6-6-6-6" /></svg>
    </button>
  );
}
