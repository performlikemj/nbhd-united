"use client";

import clsx from "clsx";

import {
  fireEngagementReward,
  reachPerfectDay,
  toggleEngagementAction,
  toggleMoveRestDay,
  type EngagementActionId,
  type EngagementState,
} from "@/lib/engagement/store";

const ACTIONS: Array<{ id: EngagementActionId; label: string }> = [
  { id: "showUp", label: "Show up" },
  { id: "move", label: "Move" },
  { id: "sit", label: "Sit" },
  { id: "reflect", label: "Reflect" },
];

const ACTIVE_CLASSES: Record<EngagementActionId, string> = {
  showUp:
    "border-engagement-show-up/40 bg-engagement-show-up/10 text-engagement-show-up",
  move:
    "border-engagement-move/40 bg-engagement-move/10 text-engagement-move",
  sit: "border-engagement-sit/40 bg-engagement-sit/10 text-engagement-sit",
  reflect:
    "border-engagement-reflect/40 bg-engagement-reflect/10 text-engagement-reflect",
};

export function DemoControls({ state }: { state: EngagementState }) {
  // PROTOTYPE: mocked
  const fireShowUp = () => fireEngagementReward("showUp");
  // PROTOTYPE: mocked
  const fireMove = () => fireEngagementReward("move");
  // PROTOTYPE: mocked
  const fireSit = () => fireEngagementReward("sit");
  // PROTOTYPE: mocked
  const fireReflect = () => fireEngagementReward("reflect");

  const rewardHandlers: Record<EngagementActionId, () => void> = {
    showUp: fireShowUp,
    move: fireMove,
    sit: fireSit,
    reflect: fireReflect,
  };

  return (
    <div className="mt-5 border-t border-border pt-4" aria-label="Demo controls">
      <p className="mb-2 font-mono text-[9px] font-semibold uppercase tracking-[0.16em] text-ink-faint">
        Demo controls
      </p>
      <div className="flex flex-wrap gap-3">
        <fieldset className="flex min-w-0 flex-wrap items-center gap-1.5">
          <legend className="sr-only">Fire reward moments</legend>
          {ACTIONS.map((action) => (
            <button
              key={`reward-${action.id}`}
              type="button"
              onClick={rewardHandlers[action.id]}
              className="min-h-[44px] rounded-lg border border-border bg-white/[0.02] px-3 font-mono text-[10px] uppercase tracking-[0.08em] text-ink-faint transition hover:border-border-strong hover:bg-surface-hover hover:text-ink-muted active:scale-[0.98]"
              aria-label={`Fire ${action.label} reward moment`}
            >
              {action.label}
            </button>
          ))}
        </fieldset>

        <span className="hidden h-11 w-px bg-border sm:block" aria-hidden="true" />

        <fieldset className="flex min-w-0 flex-wrap items-center gap-1.5">
          <legend className="sr-only">Toggle completed states</legend>
          {ACTIONS.map((action) => {
            const isDone = state.completed[action.id];
            return (
              <button
                key={`state-${action.id}`}
                type="button"
                onClick={() => toggleEngagementAction(action.id)}
                aria-pressed={isDone}
                className={clsx(
                  "min-h-[44px] rounded-lg border px-3 font-mono text-[10px] uppercase tracking-[0.08em] transition active:scale-[0.98]",
                  isDone
                    ? ACTIVE_CLASSES[action.id]
                    : "border-border bg-transparent text-ink-faint hover:border-border-strong hover:text-ink-muted",
                )}
              >
                {action.label} {isDone ? "on" : "off"}
              </button>
            );
          })}
          <button
            type="button"
            onClick={toggleMoveRestDay}
            aria-pressed={state.moveRestDay}
            className={clsx(
              "min-h-[44px] rounded-lg border px-3 font-mono text-[10px] uppercase tracking-[0.08em] transition active:scale-[0.98]",
              state.moveRestDay
                ? "border-engagement-rest/50 bg-engagement-rest/10 text-engagement-rest"
                : "border-border bg-transparent text-ink-faint hover:border-border-strong hover:text-ink-muted",
            )}
          >
            Rest day
          </button>
          <button
            type="button"
            onClick={reachPerfectDay}
            className="min-h-[44px] rounded-lg border border-accent/40 bg-accent/10 px-3 font-mono text-[10px] uppercase tracking-[0.08em] text-accent transition hover:bg-accent/20 active:scale-[0.98]"
          >
            All four
          </button>
        </fieldset>
      </div>
    </div>
  );
}
