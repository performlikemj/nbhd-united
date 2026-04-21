"use client";

import { useState } from "react";

import { useCreateWorkoutMutation } from "@/lib/queries";
import type { WorkoutCategory } from "@/lib/types";
import { CATEGORIES, CATEGORY_IDS } from "./category-meta";

interface NewWorkoutDialogProps {
  open: boolean;
  presetDate: string | null;
  onClose: () => void;
  onCreated: (id: string) => void;
}

/** Wrapper — remounts inner form each time dialog opens so state resets. */
export function NewWorkoutDialog({ open, presetDate, onClose, onCreated }: NewWorkoutDialogProps) {
  if (!open) return null;
  return <NewWorkoutDialogInner presetDate={presetDate} onClose={onClose} onCreated={onCreated} />;
}

function NewWorkoutDialogInner({ presetDate, onClose, onCreated }: Omit<NewWorkoutDialogProps, "open">) {
  const todayISO = new Date().toISOString().slice(0, 10);
  const [step, setStep] = useState(0);
  const [cat, setCat] = useState<WorkoutCategory | null>(null);
  const [date, setDate] = useState(presetDate || todayISO);
  const [status, setStatus] = useState<"done" | "planned">(
    presetDate && presetDate > todayISO ? "planned" : "done",
  );
  const [activity, setActivity] = useState("");
  const createMutation = useCreateWorkoutMutation();

  const suggestions = cat ? CATEGORIES[cat].suggest : [];

  const handleCreate = async () => {
    if (!cat) return;
    const detailJson: Record<string, unknown> = {};
    if (cat === "strength") detailJson.exercises = [];
    if (cat === "calisthenics") detailJson.skills = [];
    if (cat === "mobility") detailJson.blocks = [];

    try {
      const result = await createMutation.mutateAsync({
        category: cat,
        activity: activity || CATEGORIES[cat].label,
        date,
        status,
        duration_minutes: 45,
        detail_json: detailJson,
      });
      onClose();
      onCreated(result.id);
    } catch {
      // mutation error handled by React Query
    }
  };

  return (
    <div className="fixed inset-0 z-[65] flex" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative ml-auto h-full w-full sm:w-[480px] bg-surface border-l border-border overflow-y-auto animate-reveal"
      >
        {/* Header */}
        <div className="sticky top-0 z-10 backdrop-blur bg-surface/90 border-b border-border px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            {step === 1 && (
              <button
                onClick={() => setStep(0)}
                className="h-7 w-7 rounded-full hover:bg-surface-hover text-ink-muted flex items-center justify-center"
              >
                <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="m15 18-6-6 6-6" /></svg>
              </button>
            )}
            <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-ink-faint">
              {step === 0 ? "NEW WORKOUT \u00b7 PICK CATEGORY" : `${CATEGORIES[cat!].label.toUpperCase()} \u00b7 QUICK ADD`}
            </span>
          </div>
          <button
            onClick={onClose}
            className="h-7 w-7 rounded-full hover:bg-surface-hover text-ink-muted flex items-center justify-center"
            aria-label="Close"
          >
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6 6 18M6 6l12 12" /></svg>
          </button>
        </div>

        {/* Step 0: Category picker */}
        {step === 0 && (
          <div className="p-6">
            <h2 className="text-2xl font-semibold italic">What kind of session?</h2>
            <p className="text-xs text-ink-faint mt-1">Category determines which fields you&apos;ll log.</p>
            <div className="mt-5 grid grid-cols-2 gap-2">
              {CATEGORY_IDS.map((c) => (
                <button
                  key={c}
                  onClick={() => { setCat(c); setStep(1); }}
                  className="rounded-panel border border-border bg-surface-elevated hover:border-border-strong hover:bg-surface-hover transition p-4 text-left"
                >
                  <div className="flex items-center gap-2">
                    <span
                      className="h-8 w-8 rounded-lg flex items-center justify-center text-xs font-bold"
                      style={{ background: `color-mix(in srgb, ${CATEGORIES[c].accent} 15%, transparent)`, color: CATEGORIES[c].accent }}
                    >
                      {CATEGORIES[c].label.charAt(0)}
                    </span>
                    <div className="text-base font-medium text-ink">{CATEGORIES[c].label}</div>
                  </div>
                  <div className="mt-2 text-[11px] text-ink-faint">{CATEGORIES[c].hint}</div>
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Step 1: Activity details */}
        {step === 1 && cat && (
          <div className="p-6 space-y-5">
            <div>
              <label className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">ACTIVITY</label>
              <input
                value={activity}
                onChange={(e) => setActivity(e.target.value)}
                placeholder="Anything \u2014 tennis, bouldering, push day\u2026"
                className="mt-1.5 w-full rounded-lg border border-border bg-surface-elevated px-4 py-2.5 text-sm text-ink focus:outline-none focus:border-accent placeholder:text-ink-faint"
              />
              <div className="mt-2 flex flex-wrap gap-1.5">
                {suggestions.map((s) => (
                  <button
                    key={s}
                    onClick={() => setActivity(s)}
                    className="rounded-full border border-border hover:border-border-strong text-[11px] px-2.5 py-1 text-ink-muted hover:text-ink transition"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">DATE</label>
                <input
                  type="date"
                  value={date}
                  onChange={(e) => setDate(e.target.value)}
                  className="mt-1.5 w-full rounded-lg border border-border bg-surface-elevated px-3 py-2.5 font-mono text-sm text-ink focus:outline-none focus:border-accent"
                />
              </div>
              <div>
                <label className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">STATUS</label>
                <div className="mt-1.5 flex gap-1">
                  {(["done", "planned"] as const).map((s) => (
                    <button
                      key={s}
                      onClick={() => setStatus(s)}
                      className={`flex-1 rounded-lg py-2 text-[11px] font-bold uppercase tracking-wider transition border ${
                        status === s
                          ? "bg-surface-hover text-ink border-border-strong"
                          : "border-border text-ink-muted hover:text-ink"
                      }`}
                    >
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            <div className="pt-2 text-[11px] text-ink-faint">
              You&apos;ll add the full details (sets, reps, pace, etc.) after saving.
            </div>

            <button
              onClick={handleCreate}
              disabled={createMutation.isPending}
              className="w-full rounded-full bg-accent text-white font-medium py-3 text-sm hover:opacity-90 transition disabled:opacity-50"
            >
              {createMutation.isPending ? "Creating\u2026" : "Create workout"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
