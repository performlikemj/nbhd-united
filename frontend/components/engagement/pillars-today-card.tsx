"use client";

import clsx from "clsx";
import { useCallback, useSyncExternalStore } from "react";

import { DemoControls } from "@/components/engagement/demo-controls";
import { RewardMoment } from "@/components/engagement/reward-moment";
import {
  dismissEngagementReward,
  getEngagementSnapshot,
  subscribeToEngagementStore,
  type EngagementActionId,
  type EngagementState,
} from "@/lib/engagement/store";

const ACTIONS: Array<{
  id: EngagementActionId;
  label: string;
  positionClass: string;
}> = [
  { id: "showUp", label: "Show up", positionClass: "pt-[50px]" },
  { id: "move", label: "Move", positionClass: "pt-2" },
  { id: "sit", label: "Sit", positionClass: "pt-[70px]" },
  { id: "reflect", label: "Reflect", positionClass: "pt-5" },
];

const ACCENT_CLASSES: Record<
  EngagementActionId,
  { text: string; background: string }
> = {
  showUp: {
    text: "text-engagement-show-up",
    background: "bg-engagement-show-up",
  },
  move: {
    text: "text-engagement-move",
    background: "bg-engagement-move",
  },
  sit: {
    text: "text-engagement-sit",
    background: "bg-engagement-sit",
  },
  reflect: {
    text: "text-engagement-reflect",
    background: "bg-engagement-reflect",
  },
};

const NODE_POINTS = [
  [100, 72],
  [300, 30],
  [500, 92],
  [700, 42],
];

function StarIcon({ filled }: { filled: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className="relative h-7 w-7"
      fill={filled ? "currentColor" : "none"}
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="m12 2.8 2.2 5.8 6.2.3-4.8 3.9 1.7 6-5.3-3.4-5.3 3.4 1.7-6-4.8-3.9 6.2-.3L12 2.8Z"
      />
    </svg>
  );
}

function actionIsFulfilled(
  state: EngagementState,
  action: EngagementActionId,
): boolean {
  return state.completed[action] || (action === "move" && state.moveRestDay);
}

function TodayConstellation({ state }: { state: EngagementState }) {
  const isPerfect = ACTIONS.every((action) =>
    actionIsFulfilled(state, action.id),
  );
  const edges = isPerfect
    ? [
        [0, 1],
        [1, 2],
        [2, 3],
        [3, 0],
      ]
    : [
        [0, 1],
        [1, 2],
        [2, 3],
      ];

  return (
    <div
      className="relative h-[150px] w-full"
      role="group"
      aria-label={
        isPerfect ? "Today’s constellation complete" : "Today’s constellation"
      }
    >
      <svg
        className="pointer-events-none absolute inset-0 h-[140px] w-full"
        viewBox="0 0 800 140"
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        {edges.map(([start, end]) => {
          const active =
            actionIsFulfilled(state, ACTIONS[start].id) &&
            actionIsFulfilled(state, ACTIONS[end].id);
          return active ? (
            <line
              key={`${start}-${end}`}
              x1={NODE_POINTS[start][0]}
              y1={NODE_POINTS[start][1]}
              x2={NODE_POINTS[end][0]}
              y2={NODE_POINTS[end][1]}
              className="stroke-ink-muted stroke-[1.25] opacity-40 transition-all duration-500"
            />
          ) : null;
        })}
      </svg>

      <ol className="relative grid h-[140px] grid-cols-4">
        {ACTIONS.map((action) => {
          const isDone = state.completed[action.id];
          const isRestDay = action.id === "move" && state.moveRestDay;
          const isFulfilled = isDone || isRestDay;
          const status = isRestDay ? "rest day" : isDone ? "done" : "not yet";
          const accent = ACCENT_CLASSES[action.id];

          return (
            <li
              key={action.id}
              className={clsx(
                "flex min-w-0 flex-col items-center",
                action.positionClass,
              )}
            >
              <span
                className={clsx(
                  "relative flex h-11 w-11 items-center justify-center rounded-full transition-colors duration-500",
                  isRestDay
                    ? "border border-engagement-rest/60 text-engagement-rest"
                    : isDone
                      ? accent.text
                      : "text-ink-faint",
                )}
              >
                {isFulfilled ? (
                  <>
                    <span
                      className={clsx(
                        "absolute inset-0 scale-150 rounded-full opacity-10",
                        isRestDay ? "bg-engagement-rest" : accent.background,
                        isPerfect &&
                          "opacity-[0.15] motion-safe:animate-constellation-shimmer",
                      )}
                    />
                    <span
                      className={clsx(
                        "absolute inset-1 rounded-full opacity-15",
                        isRestDay ? "bg-engagement-rest" : accent.background,
                      )}
                    />
                  </>
                ) : null}
                <StarIcon filled={isDone} />
              </span>
              <span
                className={clsx(
                  "mt-1.5 whitespace-nowrap font-mono text-[9px] font-semibold uppercase tracking-[0.08em] sm:text-[10px]",
                  isRestDay
                    ? "text-engagement-rest"
                    : isDone
                      ? accent.text
                      : "text-ink-faint",
                )}
              >
                {action.label}
              </span>
              <span className="sr-only">
                {action.label}: {status}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

export function PillarsTodayCard({ delay }: { delay: number }) {
  const state = useSyncExternalStore(
    subscribeToEngagementStore,
    getEngagementSnapshot,
    getEngagementSnapshot,
  );
  const dismissReward = useCallback(
    (id: number) => dismissEngagementReward(id),
    [],
  );

  return (
    <section
      className="glass-card-horizons animate-reveal min-w-0 overflow-visible p-5 shadow-panel motion-reduce:animate-none sm:p-8"
      style={{ animationDelay: `${delay}ms` }}
    >
      <header className="mb-2">
        <h2 className="font-headline text-[17px] font-bold tracking-tight text-ink">
          Today
        </h2>
      </header>
      <div className="relative min-h-[190px]">
        <TodayConstellation state={state} />
        <p className="mt-2 text-center text-xs text-ink-faint">
          {state.rhythm.days} of the last {state.rhythm.window} days · best{" "}
          {state.rhythm.best}
        </p>
        {state.reward ? (
          <RewardMoment
            key={state.reward.id}
            reward={state.reward}
            onDismiss={dismissReward}
          />
        ) : null}
      </div>
      <DemoControls state={state} />
    </section>
  );
}
