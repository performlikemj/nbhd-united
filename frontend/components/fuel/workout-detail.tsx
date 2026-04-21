"use client";

import { useEffect, useState } from "react";

import { useDeleteWorkoutMutation, useUpdateWorkoutMutation, useWorkoutQuery } from "@/lib/queries";
import type { FuelWorkout, WorkoutCategory } from "@/lib/types";
import { CATEGORIES, CATEGORY_IDS } from "./category-meta";
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
  const { data: workout } = useWorkoutQuery(workoutId);
  const updateMutation = useUpdateWorkoutMutation();
  const deleteMutation = useDeleteWorkoutMutation();

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Partial<FuelWorkout>>({});

  useEffect(() => {
    if (workout) {
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
  }, [workout?.id]);

  if (!workoutId || !workout) return null;

  const meta = CATEGORIES[workout.category as WorkoutCategory];

  const save = () => {
    updateMutation.mutate({ id: workout.id, data: draft });
    setEditing(false);
  };

  const markComplete = () => {
    updateMutation.mutate({ id: workout.id, data: { ...draft, status: "done" } });
    setEditing(false);
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
        <div className="sticky top-0 z-10 backdrop-blur bg-surface/90 border-b border-border px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-bold uppercase tracking-[0.2em]" style={{ color: meta.accent }}>
              {meta.label.toUpperCase()}
            </span>
            <span className="text-ink-faint">&middot;</span>
            <span className="font-mono text-[10px] text-ink-faint">{fmtShortDate(workout.date)}</span>
            {workout.status === "planned" && (
              <span className="text-[8px] font-bold uppercase tracking-wider rounded px-1.5 py-0.5 bg-accent/10 text-accent ml-1">
                PLANNED
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            {!editing && (
              <button
                onClick={() => setEditing(true)}
                className="rounded-full border border-border hover:border-border-strong text-ink-muted hover:text-ink px-3 py-1 text-[11px] font-bold uppercase tracking-wider transition"
              >
                EDIT
              </button>
            )}
            <button
              onClick={onClose}
              className="h-7 w-7 rounded-full hover:bg-surface-hover text-ink-muted flex items-center justify-center"
              aria-label="Close"
            >
              <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6 6 18M6 6l12 12" /></svg>
            </button>
          </div>
        </div>

        <div className="p-6 space-y-6">
          {/* Activity name */}
          <div>
            {editing ? (
              <input
                value={draft.activity || ""}
                onChange={(e) => setDraft({ ...draft, activity: e.target.value })}
                placeholder="Activity name"
                className="w-full bg-transparent text-3xl font-semibold italic text-ink focus:outline-none placeholder:text-ink-faint"
              />
            ) : (
              <h2 className="text-3xl font-semibold italic">{workout.activity}</h2>
            )}
            <div className="mt-2 text-xs text-ink-faint">{fmtLongDate(workout.date)}</div>
          </div>

          {/* Category switcher (edit mode) */}
          {editing && (
            <div>
              <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint mb-2">CATEGORY</div>
              <div className="flex flex-wrap gap-1.5">
                {CATEGORY_IDS.map((c) => {
                  const on = draft.category === c;
                  return (
                    <button
                      key={c}
                      onClick={() => setDraft({ ...draft, category: c })}
                      className={`rounded-full px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider transition border flex items-center gap-1.5 ${
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
          )}

          {/* Top-line fields */}
          <div className="grid grid-cols-3 gap-3">
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
            <FieldBox label="RPE \u00b7 1\u201310">
              {editing ? (
                <input type="number" min="1" max="10" value={draft.rpe ?? ""} onChange={(e) => setDraft({ ...draft, rpe: e.target.value ? +e.target.value : null })} placeholder="\u2014" className="w-full bg-transparent font-mono text-sm text-ink focus:outline-none" />
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
            <StatsEditor detail={detail} editing={editing} onChange={updateDetailJson} fields={[
              ["distance_km", "DISTANCE", "km"],
              ["pace", "PACE", "/km"],
              ["avg_hr", "AVG HR", "bpm"],
              ["elevation", "ELEVATION", "m"],
              ["avg_power", "AVG POWER", "w"],
            ]} />
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

          {/* Action bar */}
          <div className="flex items-center gap-2 pt-4 border-t border-border">
            {editing ? (
              <>
                {draft.status === "planned" && (
                  <button onClick={markComplete} className="flex-1 rounded-full bg-emerald-bg text-emerald-text border border-emerald-border font-medium py-2.5 text-sm hover:opacity-90 transition">
                    Mark complete
                  </button>
                )}
                <button onClick={save} disabled={updateMutation.isPending} className="flex-1 rounded-full bg-accent text-white font-medium py-2.5 text-sm hover:opacity-90 transition disabled:opacity-50">
                  {updateMutation.isPending ? "Saving\u2026" : draft.status === "planned" ? "Save plan" : "Save changes"}
                </button>
              </>
            ) : (
              <button onClick={() => setEditing(true)} className="flex-1 rounded-full bg-surface-elevated hover:bg-surface-hover border border-border text-ink font-medium py-2.5 text-sm transition">
                Edit workout
              </button>
            )}
            <button onClick={handleDelete} className="rounded-full border border-border hover:border-rose-border text-ink-muted hover:text-rose-text px-4 py-2.5 text-xs transition">
              Delete
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function FieldBox({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-surface-elevated px-3 py-2.5">
      <div className="text-[8px] font-bold uppercase tracking-[0.2em] text-ink-faint">{label}</div>
      <div className="mt-1">{children}</div>
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
                <div key={j} className="grid grid-cols-[22px_1fr_1fr_auto] items-center gap-2 text-xs">
                  <span className="font-mono text-[10px] text-ink-faint">{j + 1}</span>
                  <div className="flex items-center gap-1">
                    {editing ? (
                      <input type="number" value={s.reps} onChange={(e) => { const sets = [...ex.sets]; sets[j] = { ...s, reps: +e.target.value }; updateEx(i, { ...ex, sets }); }} className="w-full bg-surface rounded border border-border px-2 py-1 font-mono text-xs text-ink focus:outline-none focus:border-accent" />
                    ) : (
                      <span className="font-mono">{s.reps}</span>
                    )}
                    <span className="text-[8px] font-bold uppercase tracking-wider text-ink-faint">REPS</span>
                  </div>
                  <div className="flex items-center gap-1">
                    {editing ? (
                      <input type="number" value={Math.round(dw(s.weight) * 10) / 10} onChange={(e) => { const sets = [...ex.sets]; sets[j] = { ...s, weight: iw(+e.target.value) }; updateEx(i, { ...ex, sets }); }} className="w-full bg-surface rounded border border-border px-2 py-1 font-mono text-xs text-ink focus:outline-none focus:border-accent" />
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

/* ---- Stats Editor (Cardio, HIIT) ---- */
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
          (editing || detail[key] != null) && (
            <div key={key} className="rounded-lg border border-border bg-surface-elevated px-3 py-2.5">
              <div className="text-[8px] font-bold uppercase tracking-[0.2em] text-ink-faint">{label}</div>
              {editing ? (
                <input
                  type={key === "pace" ? "text" : "number"}
                  value={(detail[key] as string | number) ?? ""}
                  onChange={(e) => onChange({ [key]: key === "pace" ? e.target.value : (e.target.value ? +e.target.value : null) })}
                  placeholder="\u2014"
                  className="mt-1 w-full bg-transparent font-mono text-base text-ink focus:outline-none"
                />
              ) : (
                <div className="mt-1 text-xl font-semibold italic">
                  {String(detail[key] ?? "\u2014")}
                  <span className="text-[10px] font-sans text-ink-faint ml-1">{unit}</span>
                </div>
              )}
            </div>
          )
        ))}
      </div>
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
