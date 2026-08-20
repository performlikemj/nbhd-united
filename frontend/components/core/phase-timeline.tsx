"use client";

import type { MeditationPhase } from "@/lib/core";

/**
 * This sit's phase arc as a thin segmented timeline. Segment widths are
 * proportional to each phase's time budget — communicating the structured manifest
 * at a glance. Labels collapse to dots on small screens. The arc varies per sit,
 * so a phase name can repeat and the length can differ from six.
 */
export function PhaseTimeline({ phases }: { phases: MeditationPhase[] }) {
  const total = phases.reduce((sum, p) => sum + p.weight, 0);
  // The heart of the sit carries the accent: the longest phase, which for the
  // classic arc is core_practice (the position this used to hard-code).
  const heartIndex = phases.reduce((best, p, i) => (p.weight > phases[best].weight ? i : best), 0);

  return (
    <div className="w-full">
      <div className="flex items-stretch gap-1.5">
        {phases.map((p, i) => (
          <div
            key={`${p.name}-${i}`}
            className="group/seg relative h-1.5 rounded-full transition-all duration-300 hover:brightness-125"
            style={{
              flexGrow: p.weight,
              flexBasis: 0,
              background:
                i === heartIndex
                  ? "linear-gradient(90deg, var(--signal), var(--accent))"
                  : "rgba(78,205,196,0.30)",
            }}
          >
            {/* tooltip */}
            <span className="pointer-events-none absolute -top-7 left-1/2 hidden -translate-x-1/2 whitespace-nowrap rounded-md border border-border bg-surface-elevated px-2 py-1 text-[10px] text-ink-muted opacity-0 shadow-panel transition-opacity group-hover/seg:opacity-100 sm:block">
              {p.label}
            </span>
          </div>
        ))}
      </div>

      {/* labels — full on sm+, abbreviated ticks on mobile */}
      <div className="mt-2.5 hidden items-center justify-between sm:flex">
        {phases.map((p, i) => (
          <span
            key={`${p.name}-${i}`}
            className="text-[10px] uppercase tracking-[0.14em] text-ink-faint"
            style={{ flexGrow: p.weight, flexBasis: 0, textAlign: "center" }}
          >
            {p.label}
          </span>
        ))}
      </div>
      <div className="mt-2 flex items-center justify-between text-[10px] uppercase tracking-[0.16em] text-ink-faint sm:hidden">
        <span>Arrival</span>
        <span aria-hidden>· · ·</span>
        <span>Closing</span>
      </div>

      <p className="mt-3 text-center text-[11px] text-ink-faint">
        {Math.round(total / 60)} minutes · narration woven with stillness
      </p>
    </div>
  );
}
