"use client";

import { useEffect, useRef, useState } from "react";

import {
  clearDraft,
  loadDraft,
  NEW_WORKOUT_KEY,
  saveDraft,
  type AutosaveEntry,
} from "@/lib/fuel-draft-autosave";
import { useCreateWorkoutMutation, useMeQuery, useWorkoutTemplatesQuery } from "@/lib/queries";
import type { WorkoutCategory } from "@/lib/types";
import { CATEGORIES, CATEGORY_IDS } from "./category-meta";

/** In-progress state of the New Workout wizard — what autosave persists so an
 *  abandoned new entry survives a navigate-away before it's created. */
interface NewWorkoutDraft {
  step: number;
  cat: WorkoutCategory | null;
  date: string;
  status: "done" | "planned";
  activity: string;
  detailFromTemplate: Record<string, unknown> | null;
}

/** Compact "2 hours ago"-style label for the restore prompt. */
function fmtAgo(ms: number): string {
  const secs = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

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
  const [detailFromTemplate, setDetailFromTemplate] = useState<Record<string, unknown> | null>(null);
  const [recoverable, setRecoverable] = useState<AutosaveEntry<NewWorkoutDraft> | null>(null);
  const [restoreChecked, setRestoreChecked] = useState(false);
  const createMutation = useCreateWorkoutMutation();
  const { data: templates } = useWorkoutTemplatesQuery(cat ?? undefined);
  const { data: me } = useMeQuery();
  const tenantId = me?.tenant?.id ?? null;

  const suggestions = cat ? CATEGORIES[cat].suggest : [];
  const catTemplates = (templates || []).filter((t) => t.category === cat);

  // Latest-value refs for the pagehide/unmount autosave flush. Synced via an
  // effect (not during render) to satisfy react-hooks/refs.
  const stateRef = useRef<NewWorkoutDraft>({ step, cat, date, status, activity, detailFromTemplate });
  const recoverableRef = useRef(recoverable);
  const createdRef = useRef(false);
  useEffect(() => {
    stateRef.current = { step, cat, date, status, activity, detailFromTemplate };
    recoverableRef.current = recoverable;
  });

  // One-shot restore detection during render (once `me` resolves tenantId) —
  // mirrors WorkoutDetail's init pattern. Offer to restore an abandoned draft
  // only on a generic "Log workout" open: a presetDate means the user tapped a
  // specific day, an intentional fresh start, so don't resurrect over it.
  if (!restoreChecked && tenantId) {
    setRestoreChecked(true);
    if (!presetDate) {
      const snap = loadDraft<NewWorkoutDraft>(tenantId, NEW_WORKOUT_KEY);
      if (snap && snap.payload.cat) setRecoverable(snap);
    }
  }

  // Debounced autosave once the user has engaged past category selection.
  useEffect(() => {
    if (!tenantId || recoverable || createdRef.current || !cat) return;
    const payload: NewWorkoutDraft = { step, cat, date, status, activity, detailFromTemplate };
    const t = window.setTimeout(() => saveDraft<NewWorkoutDraft>(tenantId, NEW_WORKOUT_KEY, payload), 800);
    return () => window.clearTimeout(t);
  }, [tenantId, recoverable, step, cat, date, status, activity, detailFromTemplate]);

  // Flush on tab-hide / dialog-close so the last edit isn't lost to the debounce.
  useEffect(() => {
    if (!tenantId) return;
    const flush = () => {
      if (recoverableRef.current || createdRef.current) return;
      const d = stateRef.current;
      if (!d.cat) return;
      saveDraft<NewWorkoutDraft>(tenantId, NEW_WORKOUT_KEY, d);
    };
    window.addEventListener("pagehide", flush);
    const onVisibility = () => {
      if (document.visibilityState === "hidden") flush();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("pagehide", flush);
      document.removeEventListener("visibilitychange", onVisibility);
      flush();
    };
  }, [tenantId]);

  const restore = () => {
    if (!recoverable) return;
    const p = recoverable.payload;
    setStep(p.step);
    setCat(p.cat);
    setDate(p.date);
    setStatus(p.status);
    setActivity(p.activity);
    setDetailFromTemplate(p.detailFromTemplate);
    setRecoverable(null);
  };

  const discardRecoverable = () => {
    if (tenantId) clearDraft(tenantId, NEW_WORKOUT_KEY);
    setRecoverable(null);
  };

  const handleCreate = async () => {
    if (!cat) return;
    let detailJson: Record<string, unknown> = detailFromTemplate || {};
    if (!detailFromTemplate) {
      if (cat === "strength") detailJson = { exercises: [] };
      if (cat === "calisthenics") detailJson = { skills: [] };
      if (cat === "mobility") detailJson = { blocks: [] };
    }

    try {
      const result = await createMutation.mutateAsync({
        category: cat,
        activity: activity || CATEGORIES[cat].label,
        date,
        status,
        duration_minutes: 45,
        detail_json: detailJson,
      });
      // Created on the server — drop the autosave snapshot and guard the
      // unmount flush from re-writing it.
      createdRef.current = true;
      if (tenantId) clearDraft(tenantId, NEW_WORKOUT_KEY);
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
        <div className="sticky top-0 z-10 backdrop-blur bg-surface/90 border-b border-border px-4 sm:px-6 py-3 sm:py-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            {step === 1 && (
              <button
                onClick={() => setStep(0)}
                className="h-11 w-11 sm:h-10 sm:w-10 rounded-full hover:bg-surface-hover text-ink-muted flex items-center justify-center"
              >
                <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="m15 18-6-6 6-6" /></svg>
              </button>
            )}
            <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-ink-faint">
              {step === 0 ? "NEW WORKOUT \u00b7 PICK CATEGORY" : `${CATEGORIES[cat!].label.toUpperCase()} \u00b7 QUICK ADD`}
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

        {/* Unsaved new-workout restore prompt */}
        {recoverable && (
          <div className="px-4 sm:px-6 pt-4">
            <div
              role="status"
              className="rounded-xl border border-accent/40 bg-accent/10 px-4 py-3 flex flex-col sm:flex-row sm:items-center gap-3"
            >
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium text-ink">
                  Unfinished workout from {fmtAgo(recoverable.updatedAt)}
                </div>
                <div className="mt-0.5 text-xs text-ink-muted">
                  Pick up where you left off?
                </div>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <button
                  onClick={restore}
                  className="rounded-full bg-accent text-white font-semibold min-h-[44px] px-4 py-2 text-xs hover:brightness-110 active:scale-[0.98] transition"
                >
                  Resume
                </button>
                <button
                  onClick={discardRecoverable}
                  className="rounded-full border border-border hover:border-border-strong text-ink-muted hover:text-ink min-h-[44px] px-4 py-2 text-xs transition"
                >
                  Start fresh
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Step 0: Category picker */}
        {step === 0 && (
          <div className="p-4 sm:p-6">
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
          <div className="p-4 sm:p-6 space-y-5">
            <div>
              <label className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">ACTIVITY</label>
              <input
                value={activity}
                onChange={(e) => setActivity(e.target.value)}
                placeholder="Anything — tennis, bouldering, push day…"
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

            {catTemplates.length > 0 && (
              <div>
                <label className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">FROM TEMPLATE</label>
                <div className="mt-1.5 space-y-1.5">
                  {catTemplates.map((t) => (
                    <button
                      key={t.id}
                      onClick={() => { setActivity(t.activity); setDetailFromTemplate(t.detail_json); }}
                      className="w-full rounded-lg border border-border hover:border-border-strong bg-surface-elevated hover:bg-surface-hover transition px-3 py-2 text-left text-sm text-ink-muted hover:text-ink min-h-[44px]"
                    >
                      {t.name}
                    </button>
                  ))}
                </div>
              </div>
            )}

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
              className="w-full glow-purple rounded-full bg-accent text-white font-semibold min-h-[48px] py-3 text-sm hover:brightness-110 active:scale-[0.98] transition disabled:opacity-50"
            >
              {createMutation.isPending ? "Creating\u2026" : "Create workout"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
