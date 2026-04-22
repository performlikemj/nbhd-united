"use client";

import { useState } from "react";

import { BodyWeight } from "@/components/fuel/body-weight";
import { Calendar } from "@/components/fuel/calendar";
import { DayDrawer } from "@/components/fuel/day-drawer";
import { History } from "@/components/fuel/history";
import { NewWorkoutDialog } from "@/components/fuel/new-workout-dialog";
import { Progress } from "@/components/fuel/progress";
import { WorkoutDetail } from "@/components/fuel/workout-detail";
import { useWorkoutsQuery } from "@/lib/queries";

type Tab = "calendar" | "history" | "progress";

export default function FuelPage() {
  const [tab, setTab] = useState<Tab>("calendar");
  const [dayIso, setDayIso] = useState<string | null>(null);
  const [workoutId, setWorkoutId] = useState<string | null>(null);
  const [newSheet, setNewSheet] = useState<{ open: boolean; date: string | null }>({ open: false, date: null });

  const { data: doneWorkouts } = useWorkoutsQuery({ status: "done", limit: 500 });
  const doneCount = doneWorkouts?.length ?? 0;

  const navigateDay = (delta: number) => {
    if (!dayIso) return;
    const [y, m, d] = dayIso.split("-").map(Number);
    const date = new Date(y, m - 1, d + delta);
    const iso = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
    setDayIso(iso);
  };

  return (
    <div className="max-w-5xl mx-auto py-2 sm:py-6 overflow-x-hidden">
      {/* Header — stacks on mobile, row on sm+ */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-6 sm:mb-8">
        <div className="min-w-0">
          <span className="text-accent text-[10px] font-bold uppercase tracking-[0.2em] mb-1 block">FUEL</span>
          <h1 className="text-3xl sm:text-4xl md:text-5xl font-semibold italic leading-tight">
            Every session,<br />
            <span className="text-ink-muted">on the calendar.</span>
          </h1>
          <p className="mt-3 text-sm text-ink-muted max-w-[560px] hidden sm:block">
            Click a day to open it. Plan ahead, log what you did, and edit anything after the fact.
            Pick a category for the logger shape — the activity name is yours to write.
          </p>
        </div>
        <button
          onClick={() => setNewSheet({ open: true, date: null })}
          className="self-start sm:self-auto rounded-full bg-accent text-white min-h-[44px] px-5 py-2.5 text-sm font-medium hover:opacity-90 transition flex items-center gap-1.5 shrink-0"
        >
          <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 5v14M5 12h14" /></svg>
          Log workout
        </button>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-border mb-6 overflow-x-auto">
        {([
          { id: "calendar" as Tab, label: "Calendar" },
          { id: "history" as Tab, label: "History", count: doneCount },
          { id: "progress" as Tab, label: "Progress" },
        ]).map((t) => {
          const on = tab === t.id;
          return (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`relative min-h-[44px] px-4 py-3 text-sm whitespace-nowrap transition ${on ? "text-ink" : "text-ink-muted hover:text-ink"}`}
            >
              {t.label}
              {t.count != null && (
                <span className={`ml-2 font-mono text-[10px] ${on ? "text-ink-muted" : "text-ink-faint"}`}>{t.count}</span>
              )}
              {on && <span className="absolute left-3 right-3 bottom-0 h-[1.5px] bg-ink rounded-full" />}
            </button>
          );
        })}
      </div>

      {/* Tab content */}
      {tab === "calendar" && <Calendar onSelectDay={setDayIso} />}
      {tab === "history" && <History onOpenWorkout={setWorkoutId} />}
      {tab === "progress" && (
        <div className="space-y-8">
          <Progress />
          <BodyWeight />
        </div>
      )}

      {/* Overlays */}
      {dayIso && (
        <DayDrawer
          iso={dayIso}
          onClose={() => setDayIso(null)}
          onNavigate={navigateDay}
          onAddWorkout={(iso) => setNewSheet({ open: true, date: iso })}
          onOpenWorkout={(id) => setWorkoutId(id)}
        />
      )}

      <WorkoutDetail
        workoutId={workoutId}
        onClose={() => setWorkoutId(null)}
      />

      <NewWorkoutDialog
        open={newSheet.open}
        presetDate={newSheet.date}
        onClose={() => setNewSheet({ open: false, date: null })}
        onCreated={(id) => setWorkoutId(id)}
      />
    </div>
  );
}
