"use client";

import { useEffect, useMemo, useState } from "react";

import {
  useCompleteWorkoutMutation,
  useDeleteWorkoutMutation,
  useScheduleWindowQuery,
  useSkipWorkoutMutation,
} from "@/lib/queries";
import type { FuelWorkout, WorkoutCategory } from "@/lib/types";
import { StatusPill } from "@/components/status-pill";
import { CATEGORIES } from "./category-meta";

interface ScheduleWeekProps {
  onAddSession: (date: string) => void;
  onOpenWorkout: (id: string) => void;
}

const DAY_LABEL_LONG = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const DAY_LABEL_SHORT = ["M", "T", "W", "T", "F", "S", "S"];

function isoDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function nextSevenDays(): { iso: string; date: Date }[] {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const out: { iso: string; date: Date }[] = [];
  for (let i = 0; i < 7; i++) {
    const d = new Date(today);
    d.setDate(d.getDate() + i);
    out.push({ iso: isoDate(d), date: d });
  }
  return out;
}

function formatTime(scheduledAt: string | null): string | null {
  if (!scheduledAt) return null;
  const d = new Date(scheduledAt);
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

export function ScheduleWeek({ onAddSession, onOpenWorkout }: ScheduleWeekProps) {
  const { data, isLoading } = useScheduleWindowQuery("7d");

  const days = useMemo(() => nextSevenDays(), []);
  const todayIso = days[0].iso;

  const byDate = useMemo(() => {
    const m: Record<string, FuelWorkout[]> = {};
    for (const w of data || []) {
      (m[w.date] ||= []).push(w);
    }
    // Sort each day's sessions by scheduled_at (nulls last) then created_at
    for (const iso in m) {
      m[iso].sort((a, b) => {
        const aTime = a.scheduled_at ? new Date(a.scheduled_at).getTime() : Number.POSITIVE_INFINITY;
        const bTime = b.scheduled_at ? new Date(b.scheduled_at).getTime() : Number.POSITIVE_INFINITY;
        if (aTime !== bTime) return aTime - bTime;
        return new Date(a.created_at).getTime() - new Date(b.created_at).getTime();
      });
    }
    return m;
  }, [data]);

  // First "planned" session in chronological order — what's coming next
  // (or what's overdue, when scheduled_at is in the past).
  const nextUp = useMemo(() => {
    const planned = (data || []).filter((w) => w.status === "planned");
    planned.sort((a, b) => {
      const aTime = a.scheduled_at ? new Date(a.scheduled_at).getTime() : Date.parse(a.date);
      const bTime = b.scheduled_at ? new Date(b.scheduled_at).getTime() : Date.parse(b.date);
      return aTime - bTime;
    });
    return planned[0];
  }, [data]);

  return (
    <div className="space-y-3">
      {nextUp && <NextUpBanner workout={nextUp} onOpen={() => onOpenWorkout(nextUp.id)} />}

      <div className="flex items-center justify-between gap-3">
        <h2 className="font-headline text-lg font-semibold text-ink">Next 7 days</h2>
        {isLoading && <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-ink-faint">loading…</span>}
      </div>

      {/*
        Mobile: horizontal scroll-snap, one full-width card visible at a time.
        Tablet (≥640px): 2-up grid.
        Desktop (≥1024px): full 7-day row.
      */}
      <div
        className="
          flex snap-x snap-mandatory gap-3 overflow-x-auto pb-2
          sm:grid sm:snap-none sm:overflow-visible sm:grid-cols-2 sm:pb-0
          lg:grid-cols-7 lg:gap-2
        "
      >
        {days.map(({ iso, date }, idx) => {
          const sessions = byDate[iso] || [];
          const isToday = iso === todayIso;
          const dow = (date.getDay() + 6) % 7; // Mon = 0
          const dayLabel = (
            <>
              <span className="lg:hidden">{DAY_LABEL_LONG[dow]}</span>
              <span className="hidden lg:inline">{DAY_LABEL_SHORT[dow]}</span>
            </>
          );
          return (
            <article
              key={iso}
              className={`
                snap-start shrink-0 basis-[88%] sm:basis-auto
                rounded-panel border border-border bg-card/95 backdrop-blur-md p-4 shadow-panel
                ${isToday ? "ring-1 ring-accent/30" : ""}
              `}
            >
              <header className="flex items-baseline justify-between gap-2 mb-3">
                <div className="flex items-baseline gap-2 min-w-0">
                  <span
                    className={`font-mono text-[10px] uppercase tracking-[0.2em] ${
                      isToday ? "text-accent" : "text-ink-faint"
                    }`}
                  >
                    {dayLabel}
                  </span>
                  <span className="font-headline text-base font-semibold text-ink">
                    {date.getDate()}
                  </span>
                  {isToday && (
                    <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-accent">today</span>
                  )}
                </div>
                <button
                  type="button"
                  aria-label={`Add a session on ${date.toDateString()}`}
                  onClick={() => onAddSession(iso)}
                  className="-mr-1.5 inline-flex h-8 w-8 items-center justify-center rounded-full text-ink-faint transition hover:bg-surface-hover hover:text-ink active:scale-95"
                >
                  <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M12 5v14M5 12h14" strokeLinecap="round" />
                  </svg>
                </button>
              </header>

              <div className="space-y-2">
                {sessions.length === 0 ? (
                  <button
                    type="button"
                    onClick={() => onAddSession(iso)}
                    className="block w-full rounded-lg border border-dashed border-border px-3 py-3 text-xs text-ink-faint transition hover:bg-surface-hover hover:text-ink"
                  >
                    Rest day · tap to add
                  </button>
                ) : (
                  sessions.map((s, i) => (
                    <SessionCard
                      key={s.id}
                      workout={s}
                      onOpen={() => onOpenWorkout(s.id)}
                      autoFocus={isToday && i === 0}
                    />
                  ))
                )}
              </div>
            </article>
          );
        })}
      </div>
    </div>
  );
}

interface NextUpBannerProps {
  workout: FuelWorkout;
  onOpen: () => void;
}

function formatRelative(scheduledAt: string | null, fallbackDate: string, now: number): string {
  const target = scheduledAt
    ? new Date(scheduledAt).getTime()
    : new Date(fallbackDate + "T00:00:00").getTime();
  const diffMs = target - now;
  const diffMin = Math.round(diffMs / 60_000);
  if (diffMin < -120) {
    const d = new Date(target);
    return `was ${d.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" })}`;
  }
  if (diffMin < 0) return "overdue";
  if (diffMin < 5) return "starting soon";
  if (diffMin < 60) return `in ${diffMin} min`;
  const target_d = new Date(target);
  const today = new Date(now);
  const isToday =
    target_d.getFullYear() === today.getFullYear() &&
    target_d.getMonth() === today.getMonth() &&
    target_d.getDate() === today.getDate();
  if (isToday) {
    return `today at ${target_d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
  }
  const tomorrow = new Date(today);
  tomorrow.setDate(tomorrow.getDate() + 1);
  const isTomorrow =
    target_d.getFullYear() === tomorrow.getFullYear() &&
    target_d.getMonth() === tomorrow.getMonth() &&
    target_d.getDate() === tomorrow.getDate();
  if (isTomorrow) {
    return `tomorrow at ${target_d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
  }
  return target_d.toLocaleString(undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

function NextUpBanner({ workout, onOpen }: NextUpBannerProps) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    // Re-render every 30s so the relative time stays accurate without
    // refetching the whole window.
    const id = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(id);
  }, []);

  const skip = useSkipWorkoutMutation();
  const complete = useCompleteWorkoutMutation();

  const cat = CATEGORIES[workout.category as WorkoutCategory] ?? CATEGORIES.other;
  const relative = formatRelative(workout.scheduled_at, workout.date, now);
  const isOverdue = relative === "overdue" || relative.startsWith("was ");

  return (
    <section
      aria-label="Next workout"
      className="
        rounded-panel border border-border bg-card/95 p-4 sm:p-5 shadow-panel backdrop-blur-md
        relative overflow-hidden
      "
      style={{
        // accent stripe along the left edge in the category color
        borderLeftWidth: "3px",
        borderLeftColor: cat.accent,
      }}
    >
      <div className="flex items-start gap-4 sm:items-center sm:justify-between flex-col sm:flex-row">
        <div className="min-w-0">
          <div className="flex items-center gap-2 mb-1.5">
            <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-accent">
              Next up
            </span>
            <span
              className={`font-mono text-[10px] uppercase tracking-[0.18em] ${
                isOverdue ? "text-status-amber-text" : "text-ink-faint"
              }`}
            >
              · {relative}
            </span>
          </div>
          <h3 className="font-headline text-xl font-semibold text-ink truncate">
            {workout.activity}
          </h3>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
            <span className="capitalize">{cat.label}</span>
            {workout.duration_minutes && <span>· {workout.duration_minutes} min</span>}
          </div>
        </div>

        <div className="flex w-full sm:w-auto items-center gap-2 shrink-0">
          <button
            type="button"
            onClick={() => complete.mutate({ id: workout.id })}
            disabled={complete.isPending}
            className="glow-purple flex-1 sm:flex-none rounded-full bg-accent px-4 py-2.5 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98] disabled:opacity-50 min-h-[44px] flex items-center justify-center"
          >
            Complete
          </button>
          <button
            type="button"
            onClick={() => {
              const reason = window.prompt("Skip reason (optional):") || "";
              skip.mutate({ id: workout.id, reason });
            }}
            disabled={skip.isPending}
            className="rounded-full border border-border bg-transparent px-4 py-2.5 text-sm font-medium text-ink-muted transition hover:bg-surface-hover hover:text-ink active:scale-95 disabled:opacity-50 min-h-[44px]"
          >
            Skip
          </button>
          <button
            type="button"
            onClick={onOpen}
            className="rounded-full border border-border bg-transparent px-4 py-2.5 text-sm font-medium text-ink-muted transition hover:bg-surface-hover hover:text-ink active:scale-95 min-h-[44px] hidden sm:inline-flex"
          >
            View
          </button>
        </div>
      </div>
    </section>
  );
}

interface SessionCardProps {
  workout: FuelWorkout;
  onOpen: () => void;
  autoFocus?: boolean;
}

function SessionCard({ workout, onOpen }: SessionCardProps) {
  const time = formatTime(workout.scheduled_at);
  const cat = CATEGORIES[workout.category as WorkoutCategory] ?? CATEGORIES.other;
  const accentBorder = { borderLeftColor: cat.accent };

  return (
    <div
      className="group relative flex items-center gap-2.5 rounded-lg border border-border border-l-2 bg-surface/60 pl-3 pr-1 py-2 transition hover:bg-surface-hover"
      style={accentBorder}
    >
      <button
        type="button"
        onClick={onOpen}
        className="flex flex-1 min-w-0 flex-col items-start text-left min-h-[44px]"
      >
        <div className="flex items-baseline gap-2 min-w-0">
          {time && (
            <span className="font-mono text-[11px] uppercase tracking-[0.1em] text-ink-faint shrink-0">
              {time}
            </span>
          )}
          <span className="truncate text-sm font-medium text-ink">{workout.activity}</span>
        </div>
        <div className="mt-0.5 flex items-center gap-2 text-[11px] text-ink-muted">
          <span className="capitalize">{workout.category}</span>
          {workout.duration_minutes && <span>· {workout.duration_minutes}m</span>}
          {workout.status !== "planned" && <StatusPill status={workout.status} size="sm" />}
        </div>
      </button>
      <SessionMenu workout={workout} />
    </div>
  );
}

function SessionMenu({ workout }: { workout: FuelWorkout }) {
  const [open, setOpen] = useState(false);
  const skip = useSkipWorkoutMutation();
  const complete = useCompleteWorkoutMutation();
  const del = useDeleteWorkoutMutation();

  const close = () => setOpen(false);
  const action = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
    } finally {
      close();
    }
  };

  return (
    <div className="relative">
      <button
        type="button"
        aria-label="Session actions"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="inline-flex h-9 w-9 items-center justify-center rounded-full text-ink-faint transition hover:bg-surface-hover hover:text-ink"
      >
        <svg viewBox="0 0 24 24" className="h-4 w-4" fill="currentColor" aria-hidden="true">
          <circle cx="5" cy="12" r="1.5" />
          <circle cx="12" cy="12" r="1.5" />
          <circle cx="19" cy="12" r="1.5" />
        </svg>
      </button>
      {open && (
        <>
          {/* Click-away */}
          <button
            type="button"
            aria-label="Dismiss menu"
            className="fixed inset-0 z-10 cursor-default"
            onClick={close}
          />
          <div
            role="menu"
            className="absolute right-0 top-full z-20 mt-1 min-w-[180px] rounded-xl border border-border bg-surface-elevated shadow-panel backdrop-blur-md"
          >
            {workout.status === "planned" && (
              <>
                <MenuItem
                  onSelect={() =>
                    action(() => complete.mutateAsync({ id: workout.id }))
                  }
                  disabled={complete.isPending}
                  label="Mark complete"
                />
                <MenuItem
                  onSelect={() => {
                    const reason = window.prompt("Skip reason (optional):") || "";
                    void action(() => skip.mutateAsync({ id: workout.id, reason }));
                  }}
                  disabled={skip.isPending}
                  label="Skip…"
                />
              </>
            )}
            <MenuItem
              onSelect={() => {
                if (window.confirm(`Delete "${workout.activity}"?`)) {
                  void action(() => del.mutateAsync(workout.id));
                }
              }}
              disabled={del.isPending}
              label="Delete"
              tone="rose"
            />
          </div>
        </>
      )}
    </div>
  );
}

function MenuItem({
  onSelect,
  label,
  disabled,
  tone,
}: {
  onSelect: () => void;
  label: string;
  disabled?: boolean;
  tone?: "rose";
}) {
  const toneCls = tone === "rose" ? "text-rose-text hover:bg-rose-bg" : "text-ink hover:bg-surface-hover";
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onSelect}
      disabled={disabled}
      className={`block w-full px-3 py-2.5 text-left text-sm transition disabled:opacity-50 ${toneCls}`}
    >
      {label}
    </button>
  );
}
