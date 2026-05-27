/**
 * Pure categorizer for orphan-draft recovery. Decides what action the
 * recovery panel should offer given the orphan and the live workouts
 * for the same date.
 *
 * The strong signal is `category + normalized(activity)`. Two cardio
 * sessions in a day with different activity names are not duplicates
 * (e.g. "Morning Run" + "Evening Run"). Same activity name + status
 * done is the dangerous case — we never auto-dedupe; the panel always
 * asks the user.
 */

import type { FuelWorkout } from "@/lib/types";

import type { OrphanDraft } from "./orphan-drafts";

export type MatchKind =
  | "fill_in" // same date+category+activity, candidate is planned → apply & complete
  | "loose_fill_in" // same date+category, different activity, planned → apply & complete
  | "potential_dup" // same date+category+activity, candidate is done → ask user
  | "coexisting" // same date+category, different activity, done → not a dup
  | "irrelevant";

export interface CategorizedCandidate {
  workout: FuelWorkout;
  kind: MatchKind;
}

const normName = (s: string | null | undefined) => (s ?? "").trim().toLowerCase();

export function categorize(orphan: OrphanDraft, candidate: FuelWorkout): MatchKind {
  if (candidate.date !== orphan.date) return "irrelevant";
  if (candidate.category !== orphan.category) return "irrelevant";
  const sameName = normName(candidate.activity) === normName(orphan.activity);
  if (candidate.status === "planned") return sameName ? "fill_in" : "loose_fill_in";
  if (candidate.status === "done") return sameName ? "potential_dup" : "coexisting";
  // skipped / cancelled / etc. — treat as fair game for a fresh log
  return "irrelevant";
}

/** Bin candidates by kind. Within each bin, newest-scheduled first. */
export function categorizeAll(orphan: OrphanDraft, candidates: FuelWorkout[]): CategorizedCandidate[] {
  return candidates
    .map((w) => ({ workout: w, kind: categorize(orphan, w) }))
    .filter((c) => c.kind !== "irrelevant")
    .sort((a, b) => {
      // Prefer fill_in > loose_fill_in > potential_dup > coexisting
      const rank: Record<Exclude<MatchKind, "irrelevant">, number> = {
        fill_in: 0,
        loose_fill_in: 1,
        potential_dup: 2,
        coexisting: 3,
      };
      return rank[a.kind as Exclude<MatchKind, "irrelevant">] - rank[b.kind as Exclude<MatchKind, "irrelevant">];
    });
}

/**
 * Top-level recovery shape — what the UI should render. Computed from
 * the categorizer's output. The naming preserves the four-case model
 * but folds "loose fill-in" into "fill_in" (the panel treats it the
 * same way, just with a more careful "Apply to this workout?" phrasing).
 */
export type RecoveryAction =
  | { kind: "save_as_new" } // case A: no candidates
  | { kind: "fill_in"; candidate: FuelWorkout; sameActivity: boolean } // case B
  | { kind: "potential_dup"; candidate: FuelWorkout; existingHasNotes: boolean }; // case C

export function pickAction(orphan: OrphanDraft, candidates: FuelWorkout[]): RecoveryAction {
  const ranked = categorizeAll(orphan, candidates);
  if (ranked.length === 0) return { kind: "save_as_new" };

  const fillIn = ranked.find((c) => c.kind === "fill_in" || c.kind === "loose_fill_in");
  if (fillIn) {
    return {
      kind: "fill_in",
      candidate: fillIn.workout,
      sameActivity: fillIn.kind === "fill_in",
    };
  }

  const dup = ranked.find((c) => c.kind === "potential_dup");
  if (dup) {
    return {
      kind: "potential_dup",
      candidate: dup.workout,
      existingHasNotes: (dup.workout.notes ?? "").trim().length > 0,
    };
  }

  // Only coexisting matches — different activity but same category, both
  // done. Means the orphan is a brand-new session for the day; save as new.
  return { kind: "save_as_new" };
}
