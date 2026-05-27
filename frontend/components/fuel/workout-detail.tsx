"use client";

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { SkelBar } from "@/components/ui/skeleton";
import { getErrorMessage, isNotFoundError } from "@/lib/errors";
import { stashOrphan } from "@/lib/orphan-drafts";
import { useCompleteWorkoutMutation, useCreateWorkoutTemplateMutation, useDeleteWorkoutMutation, useDuplicateWorkoutMutation, useMeQuery, useUpdateWorkoutMutation, useWorkoutQuery } from "@/lib/queries";
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

/**
 * Normalize a raw pace string to canonical MM:SS for storage.
 *
 * Accepts colon (8:34), prime/double-prime (8'34"), period (8.34),
 * comma (8,34), or m/s markers (8m34s). Strips an optional /km or /mi
 * suffix that Garmin/Strava pastes carry. Seconds are zero-padded.
 *
 * Pass-through: anything that doesn't match the digit-separator-digit
 * shape (descriptive entries like "tempo" or "comfortably hard") is
 * returned trimmed but otherwise unchanged. Backend stores either form
 * — only MM:SS graphs in apps/fuel/services.py.
 */
function normalizePace(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return "";
  const m = trimmed.match(/^(\d{1,2})\s*[':,.m:]\s*(\d{1,2})\s*["s]?\s*(?:\/(?:km|mi))?\s*$/i);
  if (!m) return trimmed;
  const [, mins, secs] = m;
  return `${parseInt(mins, 10)}:${secs.padStart(2, "0")}`;
}

/* ---- Per-set metric contract (#593 Phase 5) ----
 *
 * Mirrors apps/fuel/set_contract.set_metric: prefer the explicit `type`
 * the backend now stamps, fall back to field presence for any row not
 * yet touched by migration 0010. The toggle lets a user re-type a
 * miscategorised set in-place instead of arguing with the assistant.
 */
type SetMetric = "weighted_reps" | "bodyweight_reps" | "hold_time";

const SET_METRIC_OPTIONS: { id: SetMetric; label: string }[] = [
  { id: "weighted_reps", label: "WEIGHT" },
  { id: "bodyweight_reps", label: "BODY" },
  { id: "hold_time", label: "HOLD" },
];

type RawSet = { type?: SetMetric; reps?: number; weight?: number; hold_s?: number; pr?: boolean };

function setMetric(s: RawSet): SetMetric {
  if (s.type === "weighted_reps" || s.type === "bodyweight_reps" || s.type === "hold_time") {
    return s.type;
  }
  if (s.hold_s != null) return "hold_time";
  if (typeof s.weight === "number" && s.weight > 0) return "weighted_reps";
  return "bodyweight_reps";
}

/** Re-shape a set when its metric changes: keep carry-over values, drop
 *  fields that don't belong, always stamp the explicit `type`. */
function reshapeSet(s: RawSet, m: SetMetric): RawSet {
  const reps = typeof s.reps === "number" ? s.reps : 8;
  const weight = typeof s.weight === "number" ? s.weight : 0;
  const hold = typeof s.hold_s === "number" ? s.hold_s : 30;
  const pr = s.pr ? { pr: true } : {};
  if (m === "hold_time") return { type: m, hold_s: hold, ...pr };
  if (m === "bodyweight_reps") return { type: m, reps, ...pr };
  return { type: m, reps, weight, ...pr };
}

function MetricToggle({
  value,
  onChange,
  options = SET_METRIC_OPTIONS,
}: {
  value: SetMetric;
  onChange: (m: SetMetric) => void;
  options?: { id: SetMetric; label: string }[];
}) {
  return (
    <div role="group" aria-label="Set metric type" className="inline-flex rounded-full border border-border bg-surface p-0.5">
      {options.map((m) => {
        const active = m.id === value;
        return (
          <button
            key={m.id}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(m.id)}
            className={`rounded-full px-2.5 py-1 text-[8px] font-bold uppercase tracking-wider transition min-h-[36px] ${
              active ? "bg-accent text-white" : "text-ink-faint hover:text-ink"
            }`}
          >
            {m.label}
          </button>
        );
      })}
    </div>
  );
}

/**
 * Numeric input that holds a string during editing and only commits on blur.
 *
 * Why not `<input type="number">`? Two problems with the native control here:
 *   1. `+e.target.value` (number coercion) drops trailing dots, so typing
 *      "60." then "2" rendered "602" instead of "60.2".
 *   2. When `value={0}` is rendered (e.g. bodyweight exercises), a typed
 *      digit at the cursor produces `"0X"` that the controlled-input cycle
 *      may leave visible until the user backspaces.
 *
 * The fix: keep a local string while the user types, accept only digits and
 * (optionally) a single decimal point, and convert on blur. Mirrors the
 * `body-weight.tsx` pattern that doesn't have this bug.
 */
function NumericInput({
  value,
  onCommit,
  allowDecimal = false,
  placeholder,
  className,
  "aria-label": ariaLabel,
}: {
  value: number | null;
  onCommit: (v: number | null) => void;
  allowDecimal?: boolean;
  placeholder?: string;
  className?: string;
  "aria-label"?: string;
}) {
  const [text, setText] = useState<string>(value == null ? "" : String(value));

  const commit = () => {
    if (text === "" || text === "-" || text === ".") {
      onCommit(null);
      return;
    }
    const n = allowDecimal ? parseFloat(text) : parseInt(text, 10);
    onCommit(Number.isFinite(n) ? n : null);
  };

  const pattern = allowDecimal ? /^-?\d*\.?\d*$/ : /^-?\d*$/;

  return (
    <input
      type="text"
      inputMode={allowDecimal ? "decimal" : "numeric"}
      value={text}
      onChange={(e) => {
        const v = e.target.value;
        if (pattern.test(v)) setText(v);
      }}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
      placeholder={placeholder}
      aria-label={ariaLabel}
      className={className}
    />
  );
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
  const { data: me } = useMeQuery();
  const tenantId = me?.tenant?.id ?? null;
  const updateMutation = useUpdateWorkoutMutation();
  const completeMutation = useCompleteWorkoutMutation();
  const deleteMutation = useDeleteWorkoutMutation();
  const duplicateMutation = useDuplicateWorkoutMutation();
  const templateMutation = useCreateWorkoutTemplateMutation();

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Partial<FuelWorkout>>({});
  const [initialized, setInitialized] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Always-current view of `draft` for callbacks that fire outside React's
  // render flow (404 useEffect, mutation onError catches). Avoids stale
  // closure on the typed draft at stash time.
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const stashedRef = useRef(false);

  const stashCurrentDraft = (source: "phantom_404" | "mutation_404") => {
    if (!tenantId || stashedRef.current) return;
    const d = draftRef.current;
    const stashId = stashOrphan(tenantId, {
      originalWorkoutId: workoutId,
      date: d.date ?? workout?.date ?? new Date().toISOString().slice(0, 10),
      category: (d.category ?? workout?.category ?? "other") as string,
      activity: d.activity ?? workout?.activity ?? "",
      duration_minutes: d.duration_minutes ?? null,
      rpe: d.rpe ?? null,
      notes: d.notes ?? "",
      detail_json: (d.detail_json ?? {}) as Record<string, unknown>,
      source,
    });
    if (stashId) stashedRef.current = true;
  };

  // Stale client state: the workout exists in the in-memory React Query
  // cache (e.g. seeded from a list response) but the server doesn't have it
  // anymore — usually because the assistant runtime deleted/replaced it.
  // Invalidate the parent lists so the next render purges the phantom,
  // and stash any meaningful in-progress edits to the orphan-drafts
  // store so the Fuel-page banner can offer recovery.
  useEffect(() => {
    if (isNotFoundError(workoutError)) {
      void qc.invalidateQueries({ queryKey: ["fuel-workouts"] });
      void qc.invalidateQueries({ queryKey: ["fuel-calendar"] });
      void qc.invalidateQueries({ queryKey: ["fuel-schedule"] });
      stashCurrentDraft("phantom_404");
    }
    // tenantId & workoutId are stable per drawer lifetime; intentionally
    // omitted to keep the stash a one-shot side-effect of the 404.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
    return <WorkoutDetailSkeleton onClose={onClose} />;
  }

  const meta = CATEGORIES[workout.category as WorkoutCategory];

  const save = async () => {
    setSaveError(null);
    try {
      await updateMutation.mutateAsync({ id: workout.id, data: draft });
      setEditing(false);
    } catch (e) {
      if (isNotFoundError(e)) {
        stashCurrentDraft("mutation_404");
        onClose();
        return;
      }
      setSaveError(getErrorMessage(e));
    }
  };

  const markComplete = async () => {
    setSaveError(null);
    try {
      await completeMutation.mutateAsync({
        id: workout.id,
        data: {
          notes: draft.notes ?? undefined,
          rpe: draft.rpe ?? undefined,
          duration_minutes: draft.duration_minutes ?? undefined,
        },
      });
      setDraft((d) => ({ ...d, status: "done" }));
      setEditing(false);
    } catch (e) {
      if (isNotFoundError(e)) {
        stashCurrentDraft("mutation_404");
        onClose();
        return;
      }
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
            {workout.status === "done" && (
              <span className="text-[8px] font-bold uppercase tracking-wider rounded px-1.5 py-0.5 bg-status-emerald text-status-emerald-text inline-flex items-center gap-1">
                <svg viewBox="0 0 24 24" className="h-2.5 w-2.5" fill="none" stroke="currentColor" strokeWidth="3" aria-hidden="true">
                  <path d="M5 12l5 5L20 7" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                DONE
              </span>
            )}
            {workout.status === "skipped" && (
              <span className="text-[8px] font-bold uppercase tracking-wider rounded px-1.5 py-0.5 bg-status-slate text-status-slate-text">
                SKIPPED
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
              label="RPE · effort 1–10"
              hint="Rate of perceived exertion: 1 = barely working, 10 = absolute max."
            >
              {editing ? (
                <input type="number" min="1" max="10" value={draft.rpe ?? ""} onChange={(e) => setDraft({ ...draft, rpe: e.target.value ? +e.target.value : null })} placeholder="—" className="w-full bg-transparent font-mono text-sm text-ink focus:outline-none" />
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
              ["peak_hr", "HEART RATE · PEAK", "bpm"],
              ["avg_hr", "HEART RATE · AVG", "bpm"],
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

          {/* Inline save error — keeps the user in edit mode with their
              draft intact so they can copy values out or retry. */}
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
            {workout.status !== "done" && (
              <button
                onClick={markComplete}
                disabled={completeMutation.isPending || updateMutation.isPending}
                className="flex-1 rounded-full bg-emerald-bg text-emerald-text border border-emerald-border font-medium min-h-[44px] py-2.5 text-sm hover:opacity-90 transition disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {completeMutation.isPending ? "Completing…" : "Mark complete"}
              </button>
            )}
            {editing ? (
              <button onClick={save} disabled={updateMutation.isPending || completeMutation.isPending} className="flex-1 glow-purple rounded-full bg-accent text-white font-semibold min-h-[44px] py-2.5 text-sm hover:brightness-110 active:scale-[0.98] transition disabled:opacity-50 disabled:cursor-not-allowed">
                {updateMutation.isPending ? "Saving\u2026" : draft.status === "planned" ? "Save plan" : "Save changes"}
              </button>
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
 * assistant runtime removed it after a planning pass, or another browser
 * tab deleted it. The 404 effect in `WorkoutDetailInner` already
 * refetches the parent lists.
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

function WorkoutDetailSkeleton({ onClose }: { onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-[55] flex" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative ml-auto h-full w-full sm:w-[620px] bg-surface border-l border-border overflow-y-auto animate-reveal"
        role="status"
        aria-busy="true"
        aria-label="Loading workout"
      >
        <div className="sticky top-0 z-10 backdrop-blur bg-surface/90 border-b border-border px-4 sm:px-6 py-3 sm:py-4 flex items-center justify-between">
          <SkelBar className="h-3 w-24" />
          <button
            onClick={onClose}
            className="h-11 w-11 sm:h-10 sm:w-10 rounded-full hover:bg-surface-hover text-ink-muted flex items-center justify-center"
            aria-label="Close"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6 6 18M6 6l12 12" /></svg>
          </button>
        </div>
        <div className="p-4 sm:p-6 space-y-5 sm:space-y-6">
          <div>
            <SkelBar className="h-8 w-2/3" />
            <SkelBar className="mt-2 h-3 w-1/3" />
          </div>
          <div className="grid grid-cols-3 gap-2">
            <SkelBar className="h-16" />
            <SkelBar className="h-16" />
            <SkelBar className="h-16" />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <SkelBar className="h-16" />
            <SkelBar className="h-16" />
          </div>
          <div>
            <SkelBar className="h-3 w-12" />
            <SkelBar className="mt-2 h-20 w-full" />
          </div>
          <div className="flex flex-col sm:flex-row gap-2 pt-4 border-t border-border">
            <SkelBar className="h-11 flex-1" />
            <SkelBar className="h-11 flex-1" />
            <SkelBar className="h-11 w-20" />
          </div>
        </div>
        <span className="sr-only">Loading workout details</span>
      </div>
    </div>
  );
}

/* ---- Strength Editor ----
 *
 * A "strength" workout can contain isometric sets (e.g. a plank slotted
 * into an upper-pull session). The LLM logs those with `hold_s` instead
 * of `reps`/`weight`. We detect per-set rather than per-exercise so a
 * single workout can mix weighted, bodyweight, and hold-time sets.
 */
function StrengthEditor({ detail, editing, onChange }: { detail: Record<string, unknown>; editing: boolean; onChange: (u: Record<string, unknown>) => void }) {
  const exercises = (detail.exercises as { name: string; sets: { type?: SetMetric; reps?: number; weight?: number; hold_s?: number; pr?: boolean }[] }[]) || [];
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
        {exercises.map((ex, i) => {
          // 1RM math only applies to weighted sets; ignore hold-time entries.
          const weightedSets = ex.sets.filter((s) => setMetric(s) === "weighted_reps" && typeof s.weight === "number" && typeof s.reps === "number");
          const oneRm = weightedSets.length > 0 ? Math.max(...weightedSets.map((s) => est1RM(s.weight!, s.reps!))) : 0;
          return (
          <div key={i} className="rounded-lg border border-border bg-surface-elevated p-3">
            <div className="flex items-center gap-2 mb-2">
              {editing ? (
                <input value={ex.name} onChange={(e) => updateEx(i, { ...ex, name: e.target.value })} placeholder={`Exercise ${i + 1}`} className="flex-1 bg-transparent text-sm text-ink focus:outline-none placeholder:text-ink-faint" />
              ) : (
                <div className="flex-1 text-sm font-medium text-ink">{ex.name}</div>
              )}
              {oneRm > 0 && (
                <div className="font-mono text-[10px] text-ink-faint">
                  1RM est {dw(oneRm)} {unit}
                </div>
              )}
            </div>
            <div className="space-y-1">
              {ex.sets.map((s, j) => {
                const metric = setMetric(s);
                const setType = (m: SetMetric) => {
                  const sets = [...ex.sets];
                  sets[j] = reshapeSet(s, m);
                  updateEx(i, { ...ex, sets });
                };
                return (
                <div key={j} className="rounded border border-border/60 bg-surface/40 p-1.5">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="font-mono text-[10px] text-ink-faint">{j + 1}</span>
                    {editing && <MetricToggle value={metric} onChange={setType} />}
                    {s.pr && <span className="ml-auto text-[8px] font-bold uppercase tracking-wider text-accent">PR</span>}
                  </div>
                  <div className="flex items-center gap-2 text-xs">
                    {metric === "hold_time" ? (
                      <div className="flex items-center gap-1">
                        {editing ? (
                          <NumericInput
                            value={s.hold_s ?? null}
                            onCommit={(v) => { const sets = [...ex.sets]; sets[j] = { ...s, type: "hold_time", hold_s: v ?? 0 }; updateEx(i, { ...ex, sets }); }}
                            aria-label={`set ${j + 1} hold seconds`}
                            className="w-24 bg-surface rounded border border-border px-2 py-1.5 font-mono text-xs text-ink focus:outline-none focus:border-accent min-h-[36px]"
                          />
                        ) : (
                          <span className="font-mono">{s.hold_s}s</span>
                        )}
                        <span className="text-[8px] font-bold uppercase tracking-wider text-ink-faint">SEC</span>
                      </div>
                    ) : (
                      <>
                        <div className="flex items-center gap-1">
                          {editing ? (
                            <NumericInput
                              value={s.reps ?? null}
                              onCommit={(v) => { const sets = [...ex.sets]; sets[j] = { ...s, reps: v ?? 0 }; updateEx(i, { ...ex, sets }); }}
                              aria-label={`set ${j + 1} reps`}
                              className="w-20 bg-surface rounded border border-border px-2 py-1.5 font-mono text-xs text-ink focus:outline-none focus:border-accent min-h-[36px]"
                            />
                          ) : (
                            <span className="font-mono">{s.reps}</span>
                          )}
                          <span className="text-[8px] font-bold uppercase tracking-wider text-ink-faint">REPS</span>
                        </div>
                        {metric === "weighted_reps" && (
                          <div className="flex items-center gap-1">
                            {editing ? (
                              <NumericInput
                                value={Math.round(dw(s.weight ?? 0) * 10) / 10}
                                onCommit={(v) => { const sets = [...ex.sets]; sets[j] = { ...s, weight: iw(v ?? 0) }; updateEx(i, { ...ex, sets }); }}
                                allowDecimal
                                aria-label={`set ${j + 1} weight in ${unit}`}
                                className="w-20 bg-surface rounded border border-border px-2 py-1.5 font-mono text-xs text-ink focus:outline-none focus:border-accent min-h-[36px]"
                              />
                            ) : (
                              <span className="font-mono">{s.weight ? dw(s.weight) : "BW"}</span>
                            )}
                            <span className="text-[8px] font-bold uppercase tracking-wider text-ink-faint">{unit.toUpperCase()}</span>
                          </div>
                        )}
                      </>
                    )}
                  </div>
                </div>
              );
              })}
              {editing && (
                <button onClick={() => updateEx(i, { ...ex, sets: [...ex.sets, { reps: 8, weight: ex.sets.at(-1)?.weight || (unit === "lbs" ? 27 : 60) }] })} className="text-[9px] font-bold uppercase tracking-wider text-ink-faint hover:text-ink transition">
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

/* ---- Stats Editor (Cardio, HIIT) ---- */
/**
 * Convention: stat boxes always render in read mode, with an em-dash for
 * empty values. Hiding empty boxes makes successful saves with partial
 * data look broken ("where did my fields go?") and is one of the two
 * root causes behind the Fuel "data disappeared" bug class.
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
            displayValue={(detail[key] as string | number | null | undefined) ?? null}
          >
            <NumericInput
              value={(detail[key] as number | null) ?? null}
              onCommit={(v) => onChange({ [key]: v })}
              placeholder="—"
              aria-label={label}
              className="min-w-0 flex-1 bg-transparent font-mono text-base text-ink focus:outline-none placeholder:text-ink-faint"
            />
          </StatBox>
        ))}
      </div>
    </div>
  );
}

/* ---- Cardio Stats Editor ---- */
/**
 * Cardio-specific stats with km/mi unit toggle and tolerant pace input.
 * Storage is canonical (km for distance, m for elevation, MM:SS for pace)
 * — conversion + normalization happen only at the display/commit boundary.
 *
 * Label convention: plain English over abbreviations, with optional
 * middle-dot qualifiers for domain context ("HEART RATE", not "AVG HR";
 * "RPE · effort 1–10"). The `title=` hover tooltip is desktop-only
 * progressive enhancement — never the primary surface for what a field
 * means, since mobile users get no hover. If a label needs a tooltip to
 * be understood, rename the label.
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
          displayValue={distanceDisplay}
          hint={`Total distance covered, in ${unit === "mi" ? "miles" : "kilometers"}.`}
        >
          <NumericInput
            value={distanceDisplay}
            onCommit={(v) =>
              onChange({ distance_km: v != null ? displayToKm(v, unit) : null })
            }
            allowDecimal
            placeholder={unit === "mi" ? "3.1" : "5.0"}
            aria-label="Distance"
            className="min-w-0 flex-1 bg-transparent font-mono text-base text-ink focus:outline-none placeholder:text-ink-faint"
          />
        </StatBox>
        <StatBox
          label="PACE"
          unit={`/${unit}`}
          editing={editing}
          displayValue={(detail.pace as string | null | undefined) ?? null}
          hint={`Try 5:30, 5'30", or 5m30s per ${unit} — all save as MM:SS. Descriptive text ("tempo") saves but won't graph.`}
        >
          <input
            type="text"
            // inputMode="text" so iOS keyboards include `:`, `'`, `"` etc.
            // The numeric/decimal pads hide most of these. Normalization
            // happens on blur via normalizePace(), so users can type any
            // common notation and the stored value is canonical MM:SS.
            inputMode="text"
            maxLength={10}
            value={(detail.pace as string) ?? ""}
            onChange={(e) => onChange({ pace: e.target.value || null })}
            onBlur={(e) => {
              const normalized = normalizePace(e.target.value);
              if (normalized !== e.target.value) {
                onChange({ pace: normalized || null });
              }
            }}
            placeholder="5:30"
            aria-label="Pace"
            className="min-w-0 flex-1 bg-transparent font-mono text-base text-ink focus:outline-none placeholder:text-ink-faint"
          />
        </StatBox>
        <StatBox
          label="HEART RATE"
          unit="bpm"
          editing={editing}
          displayValue={(detail.avg_hr as number | null | undefined) ?? null}
          hint="Average heart rate during the session, in beats per minute (whole numbers)."
        >
          <NumericInput
            value={(detail.avg_hr as number | null) ?? null}
            onCommit={(v) => onChange({ avg_hr: v != null ? Math.round(v) : null })}
            placeholder="150"
            aria-label="Average heart rate"
            className="min-w-0 flex-1 bg-transparent font-mono text-base text-ink focus:outline-none placeholder:text-ink-faint"
          />
        </StatBox>
        <StatBox
          label="ELEVATION"
          unit={elevUnit}
          editing={editing}
          displayValue={elevationDisplay}
          hint={`Total elevation gain (sum of all climbs) in ${elevUnit === "ft" ? "feet" : "meters"}.`}
        >
          <NumericInput
            value={elevationDisplay}
            onCommit={(v) =>
              onChange({ elevation: v != null ? displayToMeters(v, unit) : null })
            }
            placeholder={unit === "mi" ? "200" : "60"}
            aria-label="Elevation"
            className="min-w-0 flex-1 bg-transparent font-mono text-base text-ink focus:outline-none placeholder:text-ink-faint"
          />
        </StatBox>
        <StatBox
          label="POWER"
          unit="w"
          editing={editing}
          displayValue={(detail.avg_power as number | null | undefined) ?? null}
          hint={"Optional — average watts from a power meter (cyclist) or Stryd pod (runner)."}
        >
          <NumericInput
            value={(detail.avg_power as number | null) ?? null}
            onCommit={(v) => onChange({ avg_power: v != null ? Math.round(v) : null })}
            placeholder="—"
            aria-label="Average power"
            className="min-w-0 flex-1 bg-transparent font-mono text-base text-ink focus:outline-none placeholder:text-ink-faint"
          />
        </StatBox>
      </div>
    </div>
  );
}

/**
 * Generic stat-box primitive used by both StatsEditor (HIIT) and
 * CardioStatsEditor. Always rendered — empty values show an em-dash
 * so saves with partial data don't visually drop fields. The caller
 * provides the edit-mode input (NumericInput, plain text, etc.) as
 * children.
 */
function StatBox({
  label,
  unit,
  editing,
  displayValue,
  hint,
  children,
}: {
  label: string;
  unit: string;
  editing: boolean;
  displayValue: string | number | null;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className="rounded-lg border border-border bg-surface-elevated px-3 py-2.5"
      title={hint}
    >
      <div className="text-[8px] font-bold uppercase tracking-[0.2em] text-ink-faint">{label}</div>
      {editing ? (
        <div className="mt-1 flex items-baseline gap-1.5">
          {children}
          {unit && <span className="text-[10px] text-ink-faint shrink-0">{unit}</span>}
        </div>
      ) : (
        <div className="mt-1 text-xl font-semibold italic">
          {displayValue != null && displayValue !== "" ? (
            <>
              {String(displayValue)}
              {unit && <span className="text-[10px] font-sans text-ink-faint ml-1">{unit}</span>}
            </>
          ) : (
            <span className="text-ink-faint">—</span>
          )}
        </div>
      )}
    </div>
  );
}

/* ---- Calisthenics Editor ---- */
function CalisthenicsEditor({ detail, editing, onChange }: { detail: Record<string, unknown>; editing: boolean; onChange: (u: Record<string, unknown>) => void }) {
  const skills = (detail.skills as { name: string; sets: { type?: SetMetric; reps?: number; hold_s?: number; pr?: boolean }[] }[]) || [];

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
          return (
            <div key={i} className="rounded-lg border border-border bg-surface-elevated p-3">
              <div className="flex items-center gap-2 mb-2">
                {editing ? (
                  <input value={sk.name} onChange={(e) => updateSkill(i, { ...sk, name: e.target.value })} placeholder={`Skill ${i + 1}`} className="flex-1 bg-transparent text-sm text-ink focus:outline-none placeholder:text-ink-faint" />
                ) : (
                  <div className="flex-1 text-sm font-medium text-ink">{sk.name}</div>
                )}
              </div>
              <div className="space-y-1.5">
                {sk.sets.map((s, j) => {
                  const metric = setMetric(s);
                  const calisMetric = metric === "weighted_reps" ? "bodyweight_reps" : metric;
                  const setType = (m: SetMetric) => {
                    const sets = [...sk.sets];
                    sets[j] = reshapeSet(s, m);
                    updateSkill(i, { ...sk, sets });
                  };
                  return (
                  <div key={j} className="rounded border border-border/60 bg-surface/40 p-1.5">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="font-mono text-[10px] text-ink-faint">{j + 1}</span>
                      {editing && (
                        <MetricToggle
                          value={calisMetric}
                          onChange={setType}
                          options={SET_METRIC_OPTIONS.filter((o) => o.id !== "weighted_reps")}
                        />
                      )}
                      {s.pr && <span className="ml-auto text-[8px] font-bold uppercase tracking-wider text-accent">PR</span>}
                    </div>
                    <div className="flex items-center gap-1 text-xs">
                      {editing ? (
                        <NumericInput
                          value={calisMetric === "hold_time" ? (s.hold_s ?? null) : (s.reps ?? null)}
                          onCommit={(v) => { const sets = [...sk.sets]; sets[j] = calisMetric === "hold_time" ? { ...s, type: "hold_time", hold_s: v ?? 0 } : { ...s, type: "bodyweight_reps", reps: v ?? 0 }; updateSkill(i, { ...sk, sets }); }}
                          aria-label={`set ${j + 1} ${calisMetric === "hold_time" ? "hold seconds" : "reps"}`}
                          className="w-24 bg-surface rounded border border-border px-2 py-1.5 font-mono text-xs text-ink focus:outline-none focus:border-accent min-h-[36px]"
                        />
                      ) : (
                        <span className="font-mono">{calisMetric === "hold_time" ? `${s.hold_s}s` : `${s.reps} reps`}</span>
                      )}
                      <span className="text-[8px] font-bold uppercase tracking-wider text-ink-faint">{calisMetric === "hold_time" ? "SEC" : "REPS"}</span>
                    </div>
                  </div>
                  );
                })}
                {editing && (
                  <button onClick={() => { const last = sk.sets.at(-1); const lm = last ? setMetric(last) : "bodyweight_reps"; const seed = lm === "weighted_reps" ? "bodyweight_reps" : lm; updateSkill(i, { ...sk, sets: [...sk.sets, reshapeSet({}, seed)] }); }} className="text-[9px] font-bold uppercase tracking-wider text-ink-faint hover:text-ink transition">
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
