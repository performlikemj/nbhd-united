"use client";

import { useMemo, useState } from "react";

import { emitToast } from "@/components/toast";
import { discardOrphan, type OrphanDraft } from "@/lib/orphan-drafts";
import { pickAction } from "@/lib/orphan-recovery";
import {
  useCreateWorkoutMutation,
  useUpdateWorkoutMutation,
  useWorkoutsQuery,
} from "@/lib/queries";
import type { FuelWorkout, WorkoutCategory } from "@/lib/types";

import { CATEGORIES } from "./category-meta";
import { WorkoutDetailReadOnly } from "./workout-detail-readonly";

interface OrphanRecoveryPanelProps {
  tenantId: string;
  draft: OrphanDraft;
  onClose: () => void;
}

/**
 * Modal-style recovery panel triggered from the orphan-drafts banner.
 * Reads the live workouts for the draft's date (not the stash) and
 * decides which of the four cases applies, then renders the matching
 * action set. Never auto-commits — every path requires a click.
 */
export function OrphanRecoveryPanel({ tenantId, draft, onClose }: OrphanRecoveryPanelProps) {
  // Live, fresh fetch — never trust the schedule list's cache for the
  // duplicate check. Same-day window is enough.
  const { data: sameDayWorkouts, isLoading } = useWorkoutsQuery({
    date_from: draft.date,
    date_to: draft.date,
  });

  const action = useMemo(
    () => (sameDayWorkouts ? pickAction(draft, sameDayWorkouts) : null),
    [draft, sameDayWorkouts],
  );

  const createMutation = useCreateWorkoutMutation();
  const updateMutation = useUpdateWorkoutMutation();
  const [submitting, setSubmitting] = useState(false);

  const meta = CATEGORIES[draft.category as WorkoutCategory] ?? CATEGORIES.other;

  const discardAndClose = (toastMsg?: string) => {
    discardOrphan(tenantId, draft.stashId);
    if (toastMsg) emitToast(toastMsg, "success");
    onClose();
  };

  const saveAsNew = async (status: "done" | "planned" = "done") => {
    setSubmitting(true);
    try {
      await createMutation.mutateAsync({
        date: draft.date,
        category: draft.category as WorkoutCategory,
        activity: draft.activity || meta.label,
        status,
        duration_minutes: draft.duration_minutes,
        rpe: draft.rpe,
        notes: draft.notes,
        detail_json: draft.detail_json,
      });
      discardAndClose("Saved your workout.");
    } catch (e) {
      // The global mutation onError already toasts the failure.
      setSubmitting(false);
      void e;
    }
  };

  const applyToCandidate = async (candidate: FuelWorkout, markComplete: boolean) => {
    setSubmitting(true);
    try {
      await updateMutation.mutateAsync({
        id: candidate.id,
        data: {
          status: markComplete ? "done" : candidate.status,
          duration_minutes: draft.duration_minutes ?? candidate.duration_minutes,
          rpe: draft.rpe ?? candidate.rpe,
          notes: draft.notes || candidate.notes,
          detail_json: { ...candidate.detail_json, ...draft.detail_json },
          // Activity stays as the candidate's planned name unless the
          // user explicitly retyped something different.
          activity:
            draft.activity && draft.activity !== candidate.activity ? draft.activity : candidate.activity,
        },
      });
      discardAndClose(markComplete ? "Applied your data and marked complete." : "Applied your data.");
    } catch (e) {
      setSubmitting(false);
      void e;
    }
  };

  const mergeNotesOnly = async (candidate: FuelWorkout) => {
    setSubmitting(true);
    try {
      await updateMutation.mutateAsync({
        id: candidate.id,
        data: { notes: draft.notes },
      });
      discardAndClose("Added your notes to the existing workout.");
    } catch (e) {
      setSubmitting(false);
      void e;
    }
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-end sm:items-center justify-center" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative w-full sm:max-w-lg bg-surface border border-border rounded-t-panel sm:rounded-panel overflow-hidden animate-reveal"
        role="dialog"
        aria-modal="true"
        aria-label="Recover unsaved workout draft"
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span
              className="text-[10px] font-bold uppercase tracking-[0.2em] shrink-0"
              style={{ color: meta.accent }}
            >
              {meta.label.toUpperCase()}
            </span>
            <span className="font-mono text-[10px] text-ink-faint">{draft.date}</span>
          </div>
          <button
            onClick={onClose}
            className="h-9 w-9 rounded-full hover:bg-surface-hover text-ink-muted flex items-center justify-center"
            aria-label="Close"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
          </button>
        </header>

        <div className="px-5 py-4 space-y-4">
          <DraftSummary draft={draft} />

          {isLoading || !action ? (
            <div className="text-sm text-ink-muted">Checking what&apos;s already on your schedule…</div>
          ) : action.kind === "save_as_new" ? (
            <CaseSaveAsNew
              draft={draft}
              submitting={submitting}
              onSaveAsNew={() => saveAsNew(draft.status ?? "planned")}
              onDiscard={() => discardAndClose()}
            />
          ) : action.kind === "fill_in" ? (
            <CaseFillIn
              candidate={action.candidate}
              sameActivity={action.sameActivity}
              submitting={submitting}
              onApply={() => applyToCandidate(action.candidate, true)}
              onSaveAsSecond={() => saveAsNew(draft.status ?? "planned")}
              onDiscard={() => discardAndClose()}
            />
          ) : (
            <CasePotentialDup
              candidate={action.candidate}
              existingHasNotes={action.existingHasNotes}
              draft={draft}
              submitting={submitting}
              onSame={() => discardAndClose("Discarded duplicate draft.")}
              onDifferent={() => saveAsNew(draft.status ?? "planned")}
              onMergeNotes={() => mergeNotesOnly(action.candidate)}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function DraftSummary({ draft }: { draft: OrphanDraft }) {
  const detail = (draft.detail_json ?? {}) as Record<string, unknown>;
  // Cardio top-line "bits" are useful in the header even though the
  // read-only preview below shows them as a stat grid — they signal
  // "this draft had real metrics" without making the user scan.
  const distanceKm = typeof detail?.distance_km === "number" ? detail.distance_km : null;
  const pace = typeof detail?.pace === "string" || typeof detail?.pace === "number" ? String(detail.pace) : null;
  const avgHr = typeof detail?.avg_hr === "number" ? detail.avg_hr : null;
  const elevation = typeof detail?.elevation === "number" ? detail.elevation : null;
  const bits: string[] = [];
  if (distanceKm != null) bits.push(`${distanceKm} km`);
  if (pace != null) bits.push(pace);
  if (avgHr != null) bits.push(`${avgHr} bpm`);
  if (elevation != null) bits.push(`${elevation} elev`);
  if (draft.duration_minutes != null) bits.push(`${draft.duration_minutes} min`);
  if (draft.rpe != null) bits.push(`RPE ${draft.rpe}`);

  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-border bg-surface-elevated px-4 py-3">
        <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">YOUR DRAFT</div>
        <div className="mt-1 text-base font-semibold italic text-ink truncate">
          {draft.activity || "Untitled workout"}
        </div>
        {bits.length > 0 && (
          <div className="mt-1 font-mono text-xs text-ink-muted">{bits.join(" · ")}</div>
        )}
        {draft.notes && (
          <div className="mt-2 text-sm text-ink-muted whitespace-pre-line">&ldquo;{draft.notes}&rdquo;</div>
        )}
      </div>
      <WorkoutDetailReadOnly detail={draft.detail_json} category={draft.category} />
    </div>
  );
}

function CaseSaveAsNew({
  submitting,
  onSaveAsNew,
  onDiscard,
}: {
  draft: OrphanDraft;
  submitting: boolean;
  onSaveAsNew: () => void;
  onDiscard: () => void;
}) {
  return (
    <div className="space-y-3">
      <p className="text-sm text-ink-muted">
        Nothing else on your schedule for this day. Save what you entered as a new workout?
      </p>
      <div className="flex flex-col sm:flex-row gap-2">
        <button
          onClick={onSaveAsNew}
          disabled={submitting}
          className="flex-1 glow-purple rounded-full bg-accent text-white font-semibold min-h-[44px] py-2.5 text-sm hover:brightness-110 active:scale-[0.98] transition disabled:opacity-50"
        >
          {submitting ? "Saving…" : "Save as a new workout"}
        </button>
        <button
          onClick={onDiscard}
          disabled={submitting}
          className="rounded-full border border-border hover:border-border-strong text-ink-muted hover:text-ink min-h-[44px] px-4 py-2.5 text-sm transition disabled:opacity-50"
        >
          Discard
        </button>
      </div>
    </div>
  );
}

function CaseFillIn({
  candidate,
  sameActivity,
  submitting,
  onApply,
  onSaveAsSecond,
  onDiscard,
}: {
  candidate: FuelWorkout;
  sameActivity: boolean;
  submitting: boolean;
  onApply: () => void;
  onSaveAsSecond: () => void;
  onDiscard: () => void;
}) {
  return (
    <div className="space-y-3">
      <p className="text-sm text-ink-muted">
        {sameActivity ? (
          <>We found a planned session that matches:</>
        ) : (
          <>You have a planned {candidate.category} session this day:</>
        )}
      </p>
      <CandidateRow workout={candidate} />
      <p className="text-sm text-ink-muted">
        Apply your data to it and mark complete, or log a separate session?
      </p>
      <div className="flex flex-col gap-2">
        <button
          onClick={onApply}
          disabled={submitting}
          className="glow-purple rounded-full bg-accent text-white font-semibold min-h-[44px] py-2.5 text-sm hover:brightness-110 active:scale-[0.98] transition disabled:opacity-50"
        >
          {submitting ? "Applying…" : "Apply & mark complete"}
        </button>
        <button
          onClick={onSaveAsSecond}
          disabled={submitting}
          className="rounded-full border border-border hover:border-border-strong text-ink-muted hover:text-ink min-h-[44px] px-4 py-2.5 text-sm transition disabled:opacity-50"
        >
          Save as a separate workout
        </button>
        <button
          onClick={onDiscard}
          disabled={submitting}
          className="rounded-full text-ink-faint hover:text-ink min-h-[44px] py-2 text-sm transition disabled:opacity-50"
        >
          Discard draft
        </button>
      </div>
    </div>
  );
}

function CasePotentialDup({
  candidate,
  existingHasNotes,
  draft,
  submitting,
  onSame,
  onDifferent,
  onMergeNotes,
}: {
  candidate: FuelWorkout;
  existingHasNotes: boolean;
  draft: OrphanDraft;
  submitting: boolean;
  onSame: () => void;
  onDifferent: () => void;
  onMergeNotes: () => void;
}) {
  // The merge-notes affordance only shows when (a) the existing record
  // has no notes and (b) the draft does — otherwise nothing useful to
  // graft on.
  const showMergeNotes = !existingHasNotes && draft.notes.trim().length > 0;

  return (
    <div className="space-y-3">
      <p className="text-sm text-ink-muted">You already logged this session today:</p>
      <CandidateRow workout={candidate} />
      <p className="text-sm text-ink">Same workout, or a separate session?</p>
      <div className="flex flex-col gap-2">
        <button
          onClick={onSame}
          disabled={submitting}
          className="rounded-full border border-border hover:border-border-strong text-ink-muted hover:text-ink min-h-[44px] px-4 py-2.5 text-sm transition disabled:opacity-50"
        >
          Same — discard draft
        </button>
        <button
          onClick={onDifferent}
          disabled={submitting}
          className="glow-purple rounded-full bg-accent text-white font-semibold min-h-[44px] py-2.5 text-sm hover:brightness-110 active:scale-[0.98] transition disabled:opacity-50"
        >
          {submitting ? "Saving…" : "Different — save as 2nd workout"}
        </button>
        {showMergeNotes && (
          <button
            onClick={onMergeNotes}
            disabled={submitting}
            className="rounded-full border border-border hover:border-border-strong text-ink-muted hover:text-ink min-h-[44px] px-4 py-2.5 text-sm transition disabled:opacity-50"
          >
            Merge my notes into the existing one
          </button>
        )}
      </div>
    </div>
  );
}

function CandidateRow({ workout }: { workout: FuelWorkout }) {
  const meta = CATEGORIES[workout.category] ?? CATEGORIES.other;
  const detail = workout.detail_json as Record<string, unknown>;
  const distanceKm = typeof detail?.distance_km === "number" ? detail.distance_km : null;
  const pace = typeof detail?.pace === "string" || typeof detail?.pace === "number" ? String(detail.pace) : null;
  const bits: string[] = [];
  if (distanceKm != null) bits.push(`${distanceKm} km`);
  if (pace != null) bits.push(pace);
  if (workout.duration_minutes != null) bits.push(`${workout.duration_minutes} min`);
  if (workout.rpe != null) bits.push(`RPE ${workout.rpe}`);

  return (
    <div
      className="rounded-lg border border-border bg-surface-elevated px-4 py-3 border-l-2"
      style={{ borderLeftColor: meta.accent }}
    >
      <div className="flex items-center gap-2">
        <span className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">
          {workout.status === "planned" ? "PLANNED" : "DONE"}
        </span>
        {workout.scheduled_at && (
          <span className="font-mono text-[10px] text-ink-faint">
            · {new Date(workout.scheduled_at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}
          </span>
        )}
      </div>
      <div className="mt-1 text-sm font-medium text-ink truncate">{workout.activity}</div>
      {bits.length > 0 && (
        <div className="mt-1 font-mono text-xs text-ink-muted">{bits.join(" · ")}</div>
      )}
      {workout.notes && (
        <div className="mt-1.5 text-sm text-ink-muted whitespace-pre-line line-clamp-2">{workout.notes}</div>
      )}
    </div>
  );
}
