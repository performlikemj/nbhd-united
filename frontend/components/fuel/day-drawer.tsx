"use client";

import { useWorkoutsQuery } from "@/lib/queries";
import type { FuelWorkout, WorkoutCategory } from "@/lib/types";
import { CATEGORIES } from "./category-meta";

function fmtLongDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-US", {
    weekday: "long",
    month: "long",
    day: "numeric",
    year: "numeric",
  });
}

function fmtShortDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-US", { month: "short", day: "numeric" });
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
    return [
      `${d.rounds || "?"} rounds`,
      d.peak_hr && `peak ${d.peak_hr}`,
    ].filter(Boolean) as string[];
  }
  if (w.category === "calisthenics") {
    return [`${((d.skills as unknown[]) || []).length} skills`];
  }
  if (w.category === "mobility") {
    return [`${((d.blocks as unknown[]) || []).length} blocks`];
  }
  return [];
}

interface DayDrawerProps {
  iso: string | null;
  onClose: () => void;
  onNavigate: (delta: number) => void;
  onAddWorkout: (iso: string) => void;
  onOpenWorkout: (id: string) => void;
}

export function DayDrawer({ iso, onClose, onNavigate, onAddWorkout, onOpenWorkout }: DayDrawerProps) {
  const { data: allWorkouts } = useWorkoutsQuery(
    iso ? { date_from: iso, date_to: iso } : undefined,
  );

  if (!iso) return null;

  const items = (allWorkouts || []).filter((w) => w.date === iso);
  const planned = items.filter((w) => w.status === "planned");
  const done = items.filter((w) => w.status === "done");
  const todayISO = new Date().toISOString().slice(0, 10);
  const isToday = iso === todayISO;

  return (
    <div className="fixed inset-0 z-50 flex" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative ml-auto h-full w-full sm:w-[540px] bg-surface border-l border-border overflow-y-auto animate-reveal"
      >
        {/* Header */}
        <div className="sticky top-0 z-10 backdrop-blur bg-surface/90 border-b border-border px-4 sm:px-6 py-3 sm:py-4 flex items-center justify-between">
          <div className="flex items-center gap-1.5">
            <button
              onClick={() => onNavigate(-1)}
              className="h-11 w-11 sm:h-10 sm:w-10 rounded-full hover:bg-surface-hover text-ink-muted flex items-center justify-center"
              aria-label="Previous day"
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="m15 18-6-6 6-6" /></svg>
            </button>
            <button
              onClick={() => onNavigate(1)}
              className="h-11 w-11 sm:h-10 sm:w-10 rounded-full hover:bg-surface-hover text-ink-muted flex items-center justify-center"
              aria-label="Next day"
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="m9 18 6-6-6-6" /></svg>
            </button>
            <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-ink-faint ml-1">
              {isToday ? "TODAY" : "DATE"}
            </span>
          </div>
          <button
            onClick={onClose}
            className="h-11 w-11 sm:h-10 sm:w-10 rounded-full hover:bg-surface-hover text-ink-muted flex items-center justify-center"
            aria-label="Close"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6 6 18M6 6l12 12" /></svg>
          </button>
        </div>

        <div className="p-4 sm:p-6 space-y-5 sm:space-y-6">
          {/* Date heading */}
          <div>
            <h2 className="text-2xl sm:text-3xl font-semibold italic">{fmtLongDate(iso)}</h2>
            <div className="mt-1 text-xs text-ink-faint font-mono">
              {items.length === 0 ? "Rest day" : `${done.length} done \u00b7 ${planned.length} planned`}
            </div>
          </div>

          {/* Planned */}
          {planned.length > 0 && (
            <div>
              <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-accent mb-2">PLANNED</div>
              <div className="space-y-2">
                {planned.map((w) => (
                  <WorkoutRow key={w.id} w={w} onClick={() => onOpenWorkout(w.id)} />
                ))}
              </div>
            </div>
          )}

          {/* Done */}
          {done.length > 0 && (
            <div>
              <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-emerald-text mb-2">COMPLETED</div>
              <div className="space-y-2">
                {done.map((w) => (
                  <WorkoutRow key={w.id} w={w} onClick={() => onOpenWorkout(w.id)} />
                ))}
              </div>
            </div>
          )}

          {/* Add button */}
          <button
            onClick={() => onAddWorkout(iso)}
            className="w-full rounded-xl border border-dashed border-border hover:border-border-strong bg-surface-elevated hover:bg-surface-hover transition px-4 min-h-[48px] py-3 flex items-center justify-center gap-2 text-sm text-ink-muted hover:text-ink"
          >
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M12 5v14M5 12h14" /></svg>
            Add workout on {fmtShortDate(iso)}
          </button>
        </div>
      </div>
    </div>
  );
}

function WorkoutRow({ w, onClick }: { w: FuelWorkout; onClick: () => void }) {
  const meta = CATEGORIES[w.category as WorkoutCategory];
  const planned = w.status === "planned";

  return (
    <button
      onClick={onClick}
      className="w-full rounded-panel border border-border bg-surface-elevated hover:border-border-strong hover:bg-surface-hover transition px-3 sm:px-4 py-3 text-left flex items-center gap-2.5 sm:gap-3 min-h-[44px]"
    >
      <span
        className="shrink-0 h-9 w-9 sm:h-8 sm:w-8 rounded-lg flex items-center justify-center text-xs font-bold"
        style={{ background: `color-mix(in srgb, ${meta.accent} 15%, transparent)`, color: meta.accent }}
      >
        {meta.label.charAt(0)}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="text-[9px] font-bold uppercase tracking-[0.2em]" style={{ color: meta.accent }}>
            {meta.label.toUpperCase()}
          </span>
          {planned && (
            <span className="text-[8px] font-bold uppercase tracking-wider rounded px-1.5 py-0.5 bg-accent/10 text-accent">
              PLANNED
            </span>
          )}
        </div>
        <div className="mt-0.5 text-sm text-ink truncate">{w.activity}</div>
        <div className="mt-0.5 text-[11px] text-ink-faint flex flex-wrap gap-x-2">
          {w.duration_minutes && <span>{w.duration_minutes} min</span>}
          {w.rpe != null && <span>&middot; RPE {w.rpe}</span>}
          {summaryChips(w).map((s, i) => (
            <span key={i}>&middot; {s}</span>
          ))}
        </div>
      </div>
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 text-ink-faint" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="m9 18 6-6-6-6" /></svg>
    </button>
  );
}
