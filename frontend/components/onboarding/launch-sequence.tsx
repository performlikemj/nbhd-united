"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { ConstellationProgress } from "./constellation-progress";
import { useProvisioningStatusQuery, useRetryProvisioningMutation } from "@/lib/queries";

const TIPS = [
  "Your assistant runs in its own private container \u2014 nobody else can see your conversations or data.",
  "Try sending \u201cHow are you?\u201d as your first message. Your assistant is ready to chat about anything.",
  "Ask about goal tracking, daily reflections, or just have a casual conversation.",
];

const STEP_TIMINGS = [0, 8, 18, 30, 48]; // seconds at which each step "completes"

export function LaunchSequence() {
  const router = useRouter();
  const { data: provisioningStatus } = useProvisioningStatusQuery(true);
  const retryMutation = useRetryProvisioningMutation();
  const [elapsed, setElapsed] = useState(0);
  const [tipIndex, setTipIndex] = useState(0);
  const [isReady, setIsReady] = useState(false);

  // Timer
  useEffect(() => {
    const interval = setInterval(() => setElapsed((e) => e + 1), 1000);
    return () => clearInterval(interval);
  }, []);

  // Tip rotation
  useEffect(() => {
    const interval = setInterval(() => {
      setTipIndex((i) => (i + 1) % TIPS.length);
    }, 8000);
    return () => clearInterval(interval);
  }, []);

  // Check provisioning status
  useEffect(() => {
    if (provisioningStatus?.status === "active" && provisioningStatus?.container_id) {
      setIsReady(true);
    }
  }, [provisioningStatus]);

  // Calculate visual progress based on elapsed time
  const timedSteps = STEP_TIMINGS.filter((t) => elapsed >= t).length;
  const completedSteps = isReady ? 5 : Math.min(timedSteps, 4);

  return (
    <div className="w-full max-w-[640px] flex flex-col items-center text-center">
      {/* Eyebrow */}
      <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-[#5dd9d0] mb-4">
        STEP 3 OF 3
      </span>

      {/* Headline */}
      <h1 className="font-display text-3xl sm:text-5xl font-extrabold text-[#e0e3e8] tracking-tight mb-4 leading-tight">
        {isReady ? "Your universe is ready" : "Launching your\nuniverse"}
      </h1>

      {/* Subtitle */}
      <p className="text-white/50 text-[15px] max-w-[420px] leading-relaxed mb-16 sm:mb-20">
        {isReady
          ? "Your private AI assistant is active and waiting for your first message."
          : "We\u2019re building your private AI space. This takes about a minute."}
      </p>

      {/* Constellation Progress */}
      <ConstellationProgress completedSteps={completedSteps} />

      {/* CTA when ready */}
      {isReady ? (
        <button
          type="button"
          onClick={() => router.push("/journal")}
          className="mt-12 glow-purple rounded-full bg-[#7C6BF0] px-10 py-3.5 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-95"
        >
          Start exploring
        </button>
      ) : (
        <>
          {/* Retry button after 90s */}
          {elapsed > 90 && !isReady && (
            <div className="mt-6 flex flex-col items-center gap-2">
              <button
                type="button"
                onClick={() => retryMutation.mutate()}
                disabled={retryMutation.isPending}
                className="rounded-full border border-[#c7bfff]/30 bg-[#c7bfff]/10 px-5 py-2 text-sm font-medium text-[#c7bfff] transition hover:bg-[#c7bfff]/15 disabled:opacity-50"
              >
                {retryMutation.isPending ? "Retrying..." : "Retry provisioning"}
              </button>
              {retryMutation.isError && (
                <span className="text-xs text-rose-400">Could not retry right now. Try again shortly.</span>
              )}
            </div>
          )}

          {/* Tip Card */}
          <div className="mt-12 sm:mt-16 w-full bg-[#12161b]/60 backdrop-blur-xl border border-white/[0.06] rounded-[20px] p-5 sm:p-6 flex flex-col items-center gap-4">
            <div className="flex items-start gap-3 sm:gap-4 text-left">
              <div className="shrink-0 rounded-full bg-[#5dd9d0]/15 p-2.5">
                <svg className="h-5 w-5 text-[#5dd9d0]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456z" />
                </svg>
              </div>
              <p className="text-white/50 text-sm leading-relaxed pt-0.5">
                {TIPS[tipIndex]}
              </p>
            </div>
            <div className="flex gap-2 mt-1">
              {TIPS.map((_, i) => (
                <div
                  key={i}
                  className={`w-1.5 h-1.5 rounded-full transition-colors duration-300 ${i === tipIndex ? "bg-[#5dd9d0]" : "bg-white/15"}`}
                />
              ))}
            </div>
          </div>

          {/* Footer hint */}
          <p className="mt-10 font-mono text-[10px] uppercase tracking-[0.15em] text-white/20">
            This usually takes under a minute
          </p>
        </>
      )}
    </div>
  );
}
