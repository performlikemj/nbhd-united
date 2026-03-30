"use client";

import clsx from "clsx";

type NodeState = "complete" | "active" | "pending";

const LABELS = ["Private space", "AI model", "Secure links", "Warming up...", "Ready"];

function CheckIcon() {
  return (
    <svg className="h-4 w-4 sm:h-[18px] sm:w-[18px]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
    </svg>
  );
}

export function ConstellationProgress({ completedSteps }: { completedSteps: number }) {
  const states: NodeState[] = LABELS.map((_, i) => {
    if (i < completedSteps) return "complete";
    if (i === completedSteps) return "active";
    return "pending";
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
              return <div key={i} className="flex-1 h-full bg-[#5dd9d0]/80" />;
            }
            if (leftState === "complete" && rightState === "active") {
              return <div key={i} className="flex-1 h-full bg-gradient-to-r from-[#5dd9d0]/80 to-[#c7bfff]/60" />;
            }
            return <div key={i} className="flex-1 h-full border-t-2 border-dashed border-white/15" />;
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
                  state === "complete" && "bg-[#5dd9d0] text-[#003734] shadow-[0_0_16px_rgba(93,217,208,0.4)]",
                  state === "active" && "bg-[#c7bfff] shadow-[0_0_20px_rgba(199,191,255,0.4)] animate-[pulseNode_2s_ease-out_infinite]",
                  state === "pending" && "border-2 border-white/20 bg-transparent",
                )}
              >
                {state === "complete" && <CheckIcon />}
                {state === "active" && <div className="w-2 h-2 sm:w-2.5 sm:h-2.5 rounded-full bg-white" />}
              </div>
              <span
                className={clsx(
                  "font-mono text-[8px] sm:text-[10px] uppercase tracking-wider whitespace-nowrap",
                  state === "complete" && "text-[#5dd9d0]/90",
                  state === "active" && "text-[#c7bfff]",
                  state === "pending" && "text-white/30",
                )}
              >
                {state === "active" && i === completedSteps
                  ? label
                  : i < completedSteps
                    ? LABELS[i]
                    : label}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
