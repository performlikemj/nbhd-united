"use client";

import clsx from "clsx";

type NodeState = "complete" | "lit" | "pending";

const LABELS = ["Private space", "AI model", "Secure links", "Warming up...", "Ready"];

interface ConstellationProgressProps {
  isComplete: boolean;
  litSteps: number;
}

function CheckIcon() {
  return (
    <svg className="h-4 w-4 sm:h-[18px] sm:w-[18px]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
    </svg>
  );
}

export function ConstellationProgress({ isComplete, litSteps }: ConstellationProgressProps) {
  const visibleLitSteps = Math.min(Math.max(litSteps, 0), LABELS.length);
  const latestLitStep = isComplete ? -1 : visibleLitSteps - 1;
  const states: NodeState[] = LABELS.map((_, i) => {
    if (isComplete) return "complete";
    return i < visibleLitSteps ? "lit" : "pending";
  });

  return (
    <div className="w-full relative py-8">
      {/* Connection Lines */}
      <div className="absolute top-1/2 left-[24px] right-[24px] h-[2px] -translate-y-[calc(50%+12px)] sm:-translate-y-[calc(50%+14px)]">
        <div className="flex h-full w-full">
          {[0, 1, 2, 3].map((i) => {
            const leftState = states[i];
            const rightState = states[i + 1];
            if (leftState === "complete" && rightState === "complete") {
              return <div key={i} className="h-full flex-1 bg-signal/80" />;
            }
            if (leftState === "lit" && rightState === "lit") {
              return <div key={i} className="h-full flex-1 bg-accent-hover/60" />;
            }
            return <div key={i} className="h-full flex-1 border-t-2 border-dashed border-border" />;
          })}
        </div>
      </div>

      {/* Nodes */}
      <div className="relative flex justify-between items-start w-full">
        {LABELS.map((label, i) => {
          const state = states[i];
          return (
            <div key={i} className="flex flex-col items-center gap-2 sm:gap-3 flex-1">
              <div
                className={clsx(
                  "flex items-center justify-center rounded-full transition-all duration-500",
                  "w-8 h-8 sm:w-10 sm:h-10",
                  state === "complete" && "glow-signal bg-signal text-bg",
                  state === "lit" && "glow-purple bg-accent-hover",
                  state === "lit" && i === latestLitStep && "animate-[pulseNode_2s_ease-out_infinite]",
                  state === "pending" && "border-2 border-border-strong bg-transparent",
                )}
                aria-label={`${label}: ${state === "complete" ? "complete" : state === "lit" ? "in progress" : "waiting"}`}
                role="img"
              >
                {state === "complete" && <CheckIcon />}
                {state === "lit" && <div className="h-2 w-2 rounded-full bg-white sm:h-2.5 sm:w-2.5" />}
              </div>
              <span
                className={clsx(
                  "font-mono text-[8px] sm:text-[10px] uppercase tracking-wider whitespace-nowrap",
                  state === "complete" && "text-signal-text",
                  state === "lit" && "text-accent-hover",
                  state === "pending" && "text-ink-faint",
                )}
              >
                {label}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
