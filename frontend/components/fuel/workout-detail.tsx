"use client";

import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { getErrorMessage, isNotFoundError } from "@/lib/errors";
import { useCreateWorkoutTemplateMutation, useDeleteWorkoutMutation, useDuplicateWorkoutMutation, useUpdateWorkoutMutation, useWorkoutQuery } from "@/lib/queries";
import type { FuelWorkout, WorkoutCategory } from "@/lib/types";
import { CATEGORIES, CATEGORY_IDS } from "./category-meta";
import {
  displayToKm,
  displayToMeters,
  elevationLabel,
  kmToDisplay,
  metersToDisplay,
  useDistanceUnit,
} from "./use-distance-unit";
import { displayToKg, kgToDisplay, useWeightUnit } from "./use-weight-unit";

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

function est1RM(weight: number, reps: number): number {
  if (!weight || reps < 1) return 0;
  if (reps === 1) return weight;
  return Math.round(weight * (1 + reps / 30) * 10) / 10;
}

interface WorkoutDetailProps {
  workoutId: string | null;
  onClose: () => void;
}

export function WorkoutDetail({ workoutId, onClose }: WorkoutDetailProps) {
  if (!workoutId) return null;
  return <WorkoutDetailInner key={workoutId} workoutId={workoutId} onClose={onClose} />;
}

function WorkoutDetailInner({ workoutId, onClose }: { workoutId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const { data: workout, error: workoutError } = useWorkoutQuery(workoutId);
  const updateMutation = useUpdateWorkoutMutation();
  const deleteMutation = useDeleteWorkoutMutation();
  const duplicateMutation = useDuplicateWorkoutMutation();
  const templateMutation = useCreateWorkoutTemplateMutation();

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Partial<FuelWorkout>>({});
  const [initialized, setInitialized] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Stale client state: the workout exists in the in-memory React Query
  // cache (e.g. seeded from a list response) but the server doesn't have it
  // anymore — usually because the assistant runtime deleted/replaced it.
  // Invalidate the parent lists so the next render purges the phantom.
  useEffect(() => {
    if (isNotFoundError(workoutError)) {
      void qc.invalidateQueries({ queryKey: ["fuel-workouts"] });
      void qc.invalidateQueries({ queryKey: ["fuel-calendar"] });
      void qc.invalidateQueries({ queryKey: ["fuel-schedule"] });
    }
  }, [workoutError, qc]);

  if (workout && !initialized) {
    setInitialized(true);
    setDraft({
      activity: workout.activity,
      date: workout.date,
      status: workout.status,
      category: workout.category,
      duration_minutes: workout.duration_minutes,
      rpe: workout.rpe,
      notes: workout.notes,
      detail_json: workout.detail_json,
    });
    setEditing(workout.status === "planned");
  }

  if (!workout) {
    if (isNotFoundError(workoutError)) {
      return <PhantomWorkoutRecovery onClose={onClose} />;
    }
    return null;
  }

  const meta = CATEGORIES[workout.category as WorkoutCategory];

  const save = async () => {
    setSaveError(null);
    try {
      await updateMutation.mutateAsync({ id: workout.id, data: draft });
      setEditing(false);
    } catch (e) {
      setSaveError(getErrorMessage(e));
    }
  };

  const markComplete = async () => {
    setSaveError(null);
    try {
      await updateMutation.mutateAsync({ id: workout.id, data: { ...draft, status: "done" } });
      setEditing(false);
    } catch (e) {
      setSaveError(getErrorMessage(e));
    }
  };

  const handleDelete = () => {
    if (confirm("Delete this workout?")) {
      deleteMutation.mutate(workout.id);
      onClose();
    }
  };

  const updateDetailJson = (updates: Record<string, unknown>) => {
    setDraft((d) => ({ ...d, detail_json: { ...(d.detail_json || {}), ...updates } }));
  };

  const detail = (draft.detail_json || {}) as Record<string, unknown>;

  return (
    <div className="fixed inset-0 z-[55] flex" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative ml-auto h-full w-full sm:w-[620px] bg-surface border-l border-border overflow-y-auto animate-reveal"
      >
        {/* Header */}
        <div className="sticky top-0 z-10 backdrop-blur bg-surface/90 border-b border-border px-4 sm:px-6 py-3 sm:py-4 flex items-center justify-between">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-[10px] font-bold uppercase tracking-[0.2em] shrink-0" style={{ color: meta.accent }}>
              {meta.label.toUpperCase()}
            </span>
            <span className="text-ink-faint hidden sm:inline">&middot;</span>
            <span className="font-mono text-[10px] text-ink-faint hidden sm:inline">{fmtShortDate(workout.date)}</span>
            {workout.status === "planned" && (
              <span className="text-[8px] font-bold uppercase tracking-wider rounded px-1.5 py-0.5 bg-accent/10 text-accent">
                PLANNED
              </span>
            )}
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            {!editing && (
              <button
                onClick={() => setEditing(true)}
                className="rounded-full border border-border hover:border-border-strong text-ink-muted hover:text-ink min-h-[44px] px-4 py-2 text-[11px] font-bold uppercase tracking-wider transition"
              >
                EDIT
              </button>
            )}
            <button
              onClick={onClose}
              className="h-11 w-11 sm:h-10 sm:w-10 rounded-full hover:bg-surface-hover text-ink-muted flex items-center justify-center"
              aria-label="Close"
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6 6 18M6 6l12 12" /></svg>
            </button>
          </div>
        </div>

        <div className="p-4 sm:p-6 space-y-5 sm:space-y-6">
          {/* Activity name */}
          <div>
            {editing ? (
              <input
                value={draft.activity || ""}
                onChange={(e) => setDraft({ ...draft, activity: e.target.value })}
                placeholder="Activity name"
                className="w-full bg-transparent text-2xl sm:text-3xl font-semibold italic text-ink focus:outline-none placeholder:text-ink-faint"
              />
            ) : (
              <h2 className="text-2xl sm:text-3xl font-semibold italic">{workout.activity}</h2>
            )}
            <div className="mt-2 text-xs text-ink-faint">{fmtLongDate(workout.date)}</div>
          </div>

          {/* Category switcher (edit mode) */}
          {editing && (
            <div>
              <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint mb-2">CATEGORY</div>
              <div className="overflow-x-auto pb-1">
                <div className="flex gap-1.5 w-max sm:w-auto sm:flex-wrap">
                  {CATEGORY_IDS.map((c) => {
                    const on = draft.category === c;
                    return (
                      <button
                        key={c}
                        onClick={() => setDraft({ ...draft, category: c })}
                        className={`rounded-full min-h-[44px] px-3 py-2 text-[11px] font-bold uppercase tracking-wider transition border flex items-center gap-1.5 whitespace-nowrap ${
                          on ? "text-ink" : "text-ink-muted"
                        }`}
                        style={on ? { background: `color-mix(in srgb, ${CATEGORIES[c].accent} 20%, transparent)`, borderColor: CATEGORIES[c].accent } : { borderColor: "var(--color-border)" }}
                      >
                        {CATEGORIES[c].label}
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>
          )}

          {/* Top-line fields */}
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
            <FieldBox label="DATE">
              {editing ? (
                <input type="date" value={draft.date || ""} onChange={(e) => setDraft({ ...draft, date: e.target.value })} className="w-full bg-transparent font-mono text-sm text-ink focus:outline-none" />
              ) : (
                <div className="font-mono text-sm">{fmtShortDate(workout.date)}</div>
              )}
            </FieldBox>
            <FieldBox label="DURATION">
              {editing ? (
                <div className="flex items-center gap-1">
                  <input type="number" value={draft.duration_minutes ?? ""} onChange={(e) => setDraft({ ...draft, duration_minutes: e.target.value ? +e.target.value : null })} className="w-full bg-transparent font-mono text-sm text-ink focus:outline-none" />
                  <span className="text-[10px] text-ink-faint">min</span>
                </div>
              ) : (
                <div className="font-mono text-sm">{workout.duration_minutes ?? "\u2014"} min</div>
              )}
            </FieldBox>
            <FieldBox
              label="RPE \u00b7 1\u201310"
              hint="Rate of perceived exertion: 1 = barely working, 10 = absolute max."
            >
              {editing ? (
                <input
                  type="number"
                  min="1"
                  max="10"
                  inputMode="numeric"
                  value={draft.rpe ?? ""}
                  onChange={(e) => setDraft({ ...draft, rpe: e.target.value ? +e.target.value : null })}
                  placeholder="\u2014"
                  className="w-full bg-transparent font-mono text-sm text-ink focus:outline-none"
                />
              ) : (
                <div className="font-mono text-sm">{workout.rpe ?? "\u2014"}</div>
              )}
            </FieldBox>
          </div>

          {/* Category-specific body */}
          {(draft.category || workout.category) === "strength" && (
            <StrengthEditor detail={detail} editing={editing} onChange={updateDetailJson} />
          )}
          {(draft.category || workout.category) === "cardio" && (
            <CardioStatsEditor detail={detail} editing={editing} onChange={updateDetailJson} />
          )}
          {(draft.category || workout.category) === "hiit" && (
            <StatsEditor detail={detail} editing={editing} onChange={updateDetailJson} fields={[
              ["rounds", "ROUNDS", ""],
              ["work_s", "WORK", "sec"],
              ["rest_s", "REST", "sec"],
              ["peak_hr", "PEAK HR", "bpm"],
              ["avg_hr", "AVG HR", "bpm"],
              ["calories", "KCAL", ""],
            ]} />
          )}
          {(draft.category || workout.category) === "calisthenics" && (
            <CalisthenicsEditor detail={detail} editing={editing} onChange={updateDetailJson} />
          )}
          {(draft.category || workout.category) === "mobility" && (
            <MobilityEditor detail={detail} editing={editing} onChange={updateDetailJson} />
          )}

          {/* Notes */}
          <div>
            <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint mb-2">NOTES</div>
            {editing ? (
              <textarea
                value={draft.notes || ""}
                onChange={(e) => setDraft({ ...draft, notes: e.target.value })}
                placeholder="How did it feel?"
                rows={3}
                className="w-full rounded-lg border border-border bg-surface-elevated px-3 py-2 text-sm text-ink focus:outline-none focus:border-accent placeholder:text-ink-faint"
              />
            ) : (
              <div className="text-sm text-ink-muted whitespace-pre-line">{workout.notes || "\u2014"}</div>
            )}
          </div>

          {/* Inline save error \u2014 keeps the user in edit mode with their
              draft intact so they can copy out values or retry. */}
          {saveError && (
            <div
              role="alert"
              className="rounded-xl border border-rose-border bg-rose-bg px-4 py-2.5 text-sm text-rose-text"
            >
              <div className="font-medium">Couldn&apos;t save</div>
              <div className="mt-0.5 text-rose-text/80">{saveError}</div>
            </div>
          )}

          {/* Action bar */}
          <div className="flex flex-col sm:flex-row gap-2 pt-4 border-t border-border">
            {editing ? (
              <>
                {draft.status === "planned" && (
                  <button onClick={markComplete} disabled={updateMutation.isPending} className="flex-1 rounded-full bg-emerald-bg text-emerald-text border border-emerald-border font-medium min-h-[44px] py-2.5 text-sm hover:opacity-90 transition disabled:opacity-50 disabled:cursor-not-allowed">
                    Mark complete
                  </button>
                )}
                <button onClick={save} disabled={updateMutation.isPending} className="flex-1 glow-purple rounded-full bg-accent text-white font-semibold min-h-[44px] py-2.5 text-sm hover:brightness-110 active:scale-[0.98] transition disabled:opacity-50 disabled:cursor-not-allowed">
                  {updateMutation.isPending ? "Saving\u2026" : draft.status === "planned" ? "Save plan" : "Save changes"}
                </button>
              </>
            ) : (
              <>
                <button onClick={() => setEditing(true)} className="flex-1 rounded-full bg-surface-elevated hover:bg-surface-hover border border-border text-ink font-medium min-h-[44px] py-2.5 text-sm transition">
                  Edit workout
                </button>
                <button
                  onClick={() => duplicateMutation.mutate(workout.id)}
                  disabled={duplicateMutation.isPending}
                  className="rounded-full border border-border hover:border-border-strong text-ink-muted hover:text-ink min-h-[44px] px-4 py-2.5 text-sm transition disabled:opacity-50"
                >
                  {duplicateMutation.isPending ? "Duplicating\u2026" : "Duplicate"}
                </button>
                <button
                  onClick={() => {
                    const name = prompt("Template name:");
                    if (name) templateMutation.mutate({ name, category: workout.category, activity: workout.activity, duration_minutes: workout.duration_minutes, detail_json: workout.detail_json });
                  }}
                  className="rounded-full border border-border hover:border-border-strong text-ink-muted hover:text-ink min-h-[44px] px-4 py-2.5 text-sm transition"
                >
                  Save as template
                </button>
              </>
            )}
            <button onClick={handleDelete} className="rounded-full border border-border hover:border-rose-border text-ink-muted hover:text-rose-text min-h-[44px] px-4 py-2.5 text-sm transition">
              Delete
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function FieldBox({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-surface-elevated px-3 py-2.5" title={hint}>
      <div className="text-[8px] font-bold uppercase tracking-[0.2em] text-ink-faint">{label}</div>
      <div className="mt-1">{children}</div>
    </div>
  );
}

/**
 * Recovery UI shown when a workout 404s — almost always because the
 * assistant runtime removed it after a planning pass. The 404 effect in
 * `WorkoutDetailInner` already refetches the parent lists.
 */
function PhantomWorkoutRecovery({ onClose }: { onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-[55] flex" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative ml-auto h-full w-full sm:w-[520px] bg-surface border-l border-border overflow-y-auto animate-reveal"
      >
        <div className="sticky top-0 z-10 backdrop-blur bg-surface/90 border-b border-border px-4 sm:px-6 py-3 flex items-center justify-end">
          <button
            onClick={onClose}
            className="h-11 w-11 rounded-full hover:bg-surface-hover text-ink-muted flex items-center justify-center"
            aria-label="Close"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6 6 18M6 6l12 12" /></svg>
          </button>
        </div>
        <div className="p-6 sm:p-8 space-y-4">
          <div className="text-[10px] font-bold uppercase tracking-[0.2em] text-ink-faint">WORKOUT NOT FOUND</div>
          <h2 className="text-2xl sm:text-3xl font-semibold italic">This session was removed.</h2>
          <p className="text-sm text-ink-muted">
            Your fitness assistant or another browser tab deleted this workout. The list has been refreshed —
            head back to the Fuel page to see the current plan.
          </p>
          <button
            onClick={onClose}
            className="rounded-full bg-accent text-white font-semibold min-h-[44px] px-5 py-2.5 text-sm glow-purple hover:brightness-110 active:scale-[0.98] transition"
          >
            Back to Fuel
          </button>
        </div>
      </div>
    </div>
  );
}

/* ---- Strength Editor ---- */
function StrengthEditor({ detail, editing, onChange }: { detail: Record<string, unknown>; editing: boolean; onChange: (u: Record<string, unknown>) => void }) {
  const exercises = (detail.exercises as { name: string; sets: { reps: number; weight: number; pr?: boolean }[] }[]) || [];
  const { unit } = useWeightUnit();

  const updateEx = (i: number, next: typeof exercises[number]) => {
    const x = [...exercises];
    x[i] = next;
    onChange({ exercises: x });
  };

  /** Display weight: stored as kg, convert for display. */
  const dw = (kg: number) => kgToDisplay(kg, unit);
  /** Input weight: convert display value back to kg for storage. */
  const iw = (display: number) => displayToKg(display, unit);

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">EXERCISES</div>
        {editing && (
          <button onClick={() => onChange({ exercises: [...exercises, { name: "", sets: [{ reps: 8, weight: unit === "lbs" ? 60 : 27 }] }] })} className="text-[9px] font-bold uppercase tracking-wider text-accent hover:text-ink transition">
            + EXERCISE
          </button>
        )}
      </div>
      <div className="space-y-2.5">
        {exercises.map((ex, i) => (
          <div key={i} className="rounded-lg border border-border bg-surface-elevated p-3">
            <div className="flex items-center gap-2 mb-2">
              {editing ? (
                <input value={ex.name} onChange={(e) => updateEx(i, { ...ex, name: e.target.value })} placeholder={`Exercise ${i + 1}`} className="flex-1 bg-transparent text-sm text-ink focus:outline-none placeholder:text-ink-faint" />
              ) : (
                <div className="flex-1 text-sm font-medium text-ink">{ex.name}</div>
              )}
              <div className="font-mono text-[10px] text-ink-faint">
                1RM est {dw(Math.max(...ex.sets.map((s) => est1RM(s.weight, s.reps))))} {unit}
              </div>
            </div>
            <div className="space-y-1">
              {ex.sets.map((s, j) => (
                <div key={j} className="grid grid-cols-[20px_1fr_1fr_auto] sm:grid-cols-[22px_1fr_1fr_auto] items-center gap-1.5 sm:gap-2 text-xs">
                  <span className="font-mono text-[10px] text-ink-faint">{j + 1}</span>
                  <div className="flex items-center gap-1">
                    {editing ? (
                      <input type="number" value={s.reps} onChange={(e) => { const sets = [...ex.sets]; sets[j] = { ...s, reps: +e.target.value }; updateEx(i, { ...ex, sets }); }} className="w-full bg-surface rounded border border-border px-1.5 sm:px-2 py-1.5 font-mono text-xs text-ink focus:outline-none focus:border-accent min-h-[36px]" />
                    ) : (
                      <span className="font-mono">{s.reps}</span>
                    )}
                    <span className="text-[8px] font-bold uppercase tracking-wider text-ink-faint">REPS</span>
                  </div>
                  <div className="flex items-center gap-1">
                    {editing ? (
                      <input type="number" value={Math.round(dw(s.weight) * 10) / 10} onChange={(e) => { const sets = [...ex.sets]; sets[j] = { ...s, weight: iw(+e.target.value) }; updateEx(i, { ...ex, sets }); }} className="w-full bg-surface rounded border border-border px-1.5 sm:px-2 py-1.5 font-mono text-xs text-ink focus:outline-none focus:border-accent min-h-[36px]" />
                    ) : (
                      <span className="font-mono">{s.weight ? dw(s.weight) : "BW"}</span>
                    )}
                    <span className="text-[8px] font-bold uppercase tracking-wider text-ink-faint">{unit.toUpperCase()}</span>
                  </div>
                  {s.pr && <span className="text-[8px] font-bold uppercase tracking-wider text-accent">PR</span>}
                </div>
              ))}
              {editing && (
                <button onClick={() => updateEx(i, { ...ex, sets: [...ex.sets, { reps: 8, weight: ex.sets.at(-1)?.weight || (unit === "lbs" ? 27 : 60) }] })} className="text-[9px] font-bold uppercase tracking-wider text-ink-faint hover:text-ink transition">
                  + SET
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---- Stats Editor (HIIT) ---- */
/**
 * Per DESIGN.md convention: stat boxes always render, with `\u2014` for empty
 * values in read mode. Hiding empty boxes makes successful saves with
 * partial data look broken ("where did my fields go?") and is one of the
 * two root causes behind the Fuel "data disappeared" bug class.
 */
function StatsEditor({ detail, editing, onChange, fields }: {
  detail: Record<string, unknown>;
  editing: boolean;
  onChange: (u: Record<string, unknown>) => void;
  fields: [string, string, string][];
}) {
  return (
    <div>
      <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint mb-2">STATS</div>
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-2.5">
        {fields.map(([key, label, unit]) => (
          <StatBox
            key={key}
            label={label}
            unit={unit}
            editing={editing}
            value={(detail[key] as string | number | null | undefined) ?? null}
            onChange={(v) => onChange({ [key]: v })}
            inputType="number"
            inputProps={{ inputMode: "numeric", step: "1", min: "0", placeholder: "\u2014" }}
          />
        ))}
      </div>
    </div>
  );
}

/* ---- Cardio Stats Editor ---- */
/**
 * Cardio-specific stats with km/mi unit toggle, pace MM:SS hint, and
 * every field labeled with its unit + a hover tooltip describing what
 * the value means. Storage is canonical (km for distance, m for
 * elevation) \u2014 conversion happens only at the display boundary.
 */
function CardioStatsEditor({ detail, editing, onChange }: {
  detail: Record<string, unknown>;
  editing: boolean;
  onChange: (u: Record<string, unknown>) => void;
}) {
  const { unit, setUnit, isPending: unitPending } = useDistanceUnit();
  const elevUnit = elevationLabel(unit);

  const storedKm = typeof detail.distance_km === "number" ? (detail.distance_km as number) : null;
  const distanceDisplay = storedKm != null ? kmToDisplay(storedKm, unit) : null;

  const storedElevM = typeof detail.elevation === "number" ? (detail.elevation as number) : null;
  const elevationDisplay = storedElevM != null ? metersToDisplay(storedElevM, unit) : null;

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">STATS</div>
        {editing && (
          <button
            type="button"
            onClick={() => setUnit(unit === "km" ? "mi" : "km")}
            disabled={unitPending}
            className="rounded-full border border-border hover:border-border-strong text-[10px] font-mono uppercase tracking-wider min-h-[28px] px-2.5 py-1 text-ink-muted hover:text-ink transition disabled:opacity-50"
            aria-label="Toggle distance unit"
            title="Switch between kilometers and miles. The change saves to your fitness profile."
          >
            {unit}
          </button>
        )}
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-2.5">
        <StatBox
          label="DISTANCE"
          unit={unit}
          editing={editing}
          value={distanceDisplay}
          onChange={(v) =>
            onChange({ distance_km: typeof v === "number" ? displayToKm(v, unit) : null })
          }
          inputType="number"
          inputProps={{ inputMode: "decimal", step: "0.01", min: "0", placeholder: unit === "mi" ? "3.1" : "5.0" }}
          hint={`Total distance covered, in ${unit === "mi" ? "miles" : "kilometers"}.`}
        />
        <StatBox
          label="PACE"
          unit={`/${unit}`}
          editing={editing}
          value={(detail.pace as string | null | undefined) ?? null}
          onChange={(v) => onChange({ pace: typeof v === "string" && v.length > 0 ? v : null })}
          inputType="text"
          // inputMode="text" so iOS keyboards include `:` (numeric/decimal
          // pads hide it). pattern is advisory — the backend accepts any
          // string but only MM:SS graphs in Progress.
          inputProps={{ inputMode: "text", pattern: "[0-9]{1,2}:[0-9]{2}", placeholder: "5:30", maxLength: 5 }}
          hint={`Format MM:SS per ${unit} (e.g. 5:30). Free-form text saves but won't graph in Progress.`}
        />
        <StatBox
          label="AVG HR"
          unit="bpm"
          editing={editing}
          value={(detail.avg_hr as number | null | undefined) ?? null}
          onChange={(v) => onChange({ avg_hr: typeof v === "number" ? Math.round(v) : null })}
          inputType="number"
          inputProps={{ inputMode: "numeric", step: "1", min: "30", max: "230", placeholder: "150" }}
          hint="Average heart rate during the session, in beats per minute (whole numbers)."
        />
        <StatBox
          label="ELEVATION"
          unit={elevUnit}
          editing={editing}
          value={elevationDisplay}
          onChange={(v) =>
            onChange({ elevation: typeof v === "number" ? displayToMeters(v, unit) : null })
          }
          inputType="number"
          inputProps={{ inputMode: "numeric", step: "1", min: "0", placeholder: unit === "mi" ? "200" : "60" }}
          hint={`Total elevation gain (sum of all climbs) in ${elevUnit === "ft" ? "feet" : "meters"}.`}
        />
        <StatBox
          label="AVG POWER"
          unit="w"
          editing={editing}
          value={(detail.avg_power as number | null | undefined) ?? null}
          onChange={(v) => onChange({ avg_power: typeof v === "number" ? Math.round(v) : null })}
          inputType="number"
          inputProps={{ inputMode: "numeric", step: "1", min: "0", placeholder: "\u2014" }}
          hint="Optional \u2014 average watts from a power meter (cyclist) or Stryd pod (runner)."
        />
      </div>
    </div>
  );
}

/**
 * Generic stat-box primitive used by both StatsEditor (HIIT) and
 * CardioStatsEditor. Always rendered \u2014 empty values show `\u2014` so saves
 * with partial data don't visually drop fields.
 */
function StatBox({
  label,
  unit,
  editing,
  value,
  onChange,
  inputType,
  inputProps,
  hint,
}: {
  label: string;
  unit: string;
  editing: boolean;
  value: string | number | null;
  onChange: (v: string | number | null) => void;
  inputType: "text" | "number";
  inputProps?: React.InputHTMLAttributes<HTMLInputElement>;
  hint?: string;
}) {
  return (
    <div
      className="rounded-lg border border-border bg-surface-elevated px-3 py-2.5"
      title={hint}
    >
      <div className="text-[8px] font-bold uppercase tracking-[0.2em] text-ink-faint">{label}</div>
      {editing ? (
        <div className="mt-1 flex items-baseline gap-1.5">
          <input
            {...inputProps}
            type={inputType}
            value={value ?? ""}
            onChange={(e) => {
              const raw = e.target.value;
              if (raw === "") return onChange(null);
              if (inputType === "number") {
                const n = Number(raw);
                return onChange(Number.isFinite(n) ? n : null);
              }
              return onChange(raw);
            }}
            className="min-w-0 flex-1 bg-transparent font-mono text-base text-ink focus:outline-none placeholder:text-ink-faint"
          />
          {unit && <span className="text-[10px] text-ink-faint shrink-0">{unit}</span>}
        </div>
      ) : (
        <div className="mt-1 text-xl font-semibold italic">
          {value != null && value !== "" ? (
            <>
              {String(value)}
              {unit && <span className="text-[10px] font-sans text-ink-faint ml-1">{unit}</span>}
            </>
          ) : (
            <span className="text-ink-faint">\u2014</span>
          )}
        </div>
      )}
    </div>
  );
}

/* ---- Calisthenics Editor ---- */
function CalisthenicsEditor({ detail, editing, onChange }: { detail: Record<string, unknown>; editing: boolean; onChange: (u: Record<string, unknown>) => void }) {
  const skills = (detail.skills as { name: string; sets: { reps?: number; hold_s?: number; pr?: boolean }[] }[]) || [];

  const updateSkill = (i: number, next: typeof skills[number]) => {
    const x = [...skills];
    x[i] = next;
    onChange({ skills: x });
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">SKILLS</div>
        {editing && (
          <button onClick={() => onChange({ skills: [...skills, { name: "", sets: [{ reps: 5 }] }] })} className="text-[9px] font-bold uppercase tracking-wider text-accent hover:text-ink transition">
            + SKILL
          </button>
        )}
      </div>
      <div className="space-y-2.5">
        {skills.map((sk, i) => {
          const isHold = sk.sets[0]?.hold_s != null;
          return (
            <div key={i} className="rounded-lg border border-border bg-surface-elevated p-3">
              <div className="flex items-center gap-2 mb-2">
                {editing ? (
                  <input value={sk.name} onChange={(e) => updateSkill(i, { ...sk, name: e.target.value })} placeholder={`Skill ${i + 1}`} className="flex-1 bg-transparent text-sm text-ink focus:outline-none placeholder:text-ink-faint" />
                ) : (
                  <div className="flex-1 text-sm font-medium text-ink">{sk.name}</div>
                )}
              </div>
              <div className="space-y-1">
                {sk.sets.map((s, j) => (
                  <div key={j} className="grid grid-cols-[22px_1fr_auto] items-center gap-2 text-xs">
                    <span className="font-mono text-[10px] text-ink-faint">{j + 1}</span>
                    <div className="flex items-center gap-1">
                      {editing ? (
                        <input type="number" value={isHold ? (s.hold_s ?? "") : (s.reps ?? "")} onChange={(e) => { const sets = [...sk.sets]; sets[j] = isHold ? { hold_s: +e.target.value } : { reps: +e.target.value }; updateSkill(i, { ...sk, sets }); }} className="w-full bg-surface rounded border border-border px-2 py-1 font-mono text-xs text-ink focus:outline-none focus:border-accent" />
                      ) : (
                        <span className="font-mono">{isHold ? `${s.hold_s}s` : `${s.reps} reps`}</span>
                      )}
                      {editing && <span className="text-[8px] font-bold uppercase tracking-wider text-ink-faint">{isHold ? "SEC" : "REPS"}</span>}
                    </div>
                    {s.pr && <span className="text-[8px] font-bold uppercase tracking-wider text-accent">PR</span>}
                  </div>
                ))}
                {editing && (
                  <button onClick={() => updateSkill(i, { ...sk, sets: [...sk.sets, isHold ? { hold_s: 10 } : { reps: 5 }] })} className="text-[9px] font-bold uppercase tracking-wider text-ink-faint hover:text-ink transition">
                    + SET
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* ---- Mobility Editor ---- */
function MobilityEditor({ detail, editing, onChange }: { detail: Record<string, unknown>; editing: boolean; onChange: (u: Record<string, unknown>) => void }) {
  const blocks = (detail.blocks as string[]) || [];
  const [newBlock, setNewBlock] = useState("");

  return (
    <div>
      <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint mb-2">BLOCKS</div>
      <div className="flex flex-wrap gap-1.5">
        {blocks.map((b, i) => (
          <span key={i} className="rounded-md border border-border bg-surface-elevated px-2.5 py-1 text-xs text-ink-muted flex items-center gap-1.5">
            {b}
            {editing && (
              <button onClick={() => onChange({ blocks: blocks.filter((_, j) => j !== i) })} className="text-ink-faint hover:text-rose-text">&times;</button>
            )}
          </span>
        ))}
      </div>
      {editing && (
        <div className="mt-2 flex gap-2">
          <input
            value={newBlock}
            onChange={(e) => setNewBlock(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && newBlock.trim()) { onChange({ blocks: [...blocks, newBlock.trim()] }); setNewBlock(""); } }}
            placeholder="Add a block, press Enter"
            className="flex-1 rounded-lg border border-border bg-surface-elevated px-3 py-2 text-sm text-ink focus:outline-none focus:border-accent placeholder:text-ink-faint"
          />
        </div>
      )}
    </div>
  );
}
