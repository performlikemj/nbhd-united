"use client";

import type { ReactNode } from "react";

import type { CoreStats as CoreStatsData } from "@/lib/core";

/**
 * Four calm stat cards — sessions, total minutes, streak, last sat.
 * Mono numbers, soft surface cards, staggered reveal on load.
 */
export function CoreStats({ stats }: { stats: CoreStatsData }) {
  const items: { label: string; value: ReactNode; unit?: string; icon: ReactNode }[] = [
    { label: "This week", value: stats.sessionsThisWeek, unit: "sessions", icon: <IconSpark /> },
    { label: "Total", value: stats.totalMinutes, unit: "min", icon: <IconClock /> },
    { label: "Streak", value: stats.streakDays, unit: "days", icon: <IconFlame /> },
    { label: "Last sat", value: stats.lastSatLabel, icon: <IconMoon /> },
  ];

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {items.map((it, i) => (
        <div
          key={it.label}
          className="group relative overflow-hidden rounded-2xl border border-border bg-surface/70 p-4 transition-colors duration-300 hover:border-signal/30"
          style={{ animation: `reveal 420ms ease-out ${100 + i * 70}ms both` }}
        >
          <div className="mb-3 flex h-8 w-8 items-center justify-center rounded-lg bg-signal-faint text-signal">
            {it.icon}
          </div>
          <div className="flex items-baseline gap-1.5">
            <span className="font-mono text-2xl font-medium text-ink">{it.value}</span>
            {it.unit && <span className="text-xs text-ink-faint">{it.unit}</span>}
          </div>
          <p className="mt-0.5 text-[11px] uppercase tracking-[0.12em] text-ink-faint">{it.label}</p>
        </div>
      ))}
    </div>
  );
}

const ico = "h-4 w-4";

function IconSpark() {
  return (
    <svg viewBox="0 0 24 24" className={ico} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8" />
    </svg>
  );
}
function IconClock() {
  return (
    <svg viewBox="0 0 24 24" className={ico} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    </svg>
  );
}
function IconFlame() {
  return (
    <svg viewBox="0 0 24 24" className={ico} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 3s5 4.5 5 9a5 5 0 0 1-10 0c0-1.6.7-3 1.5-4 .2 1 .8 1.8 1.6 2 0-2.3 .9-5 1.9-7Z" />
    </svg>
  );
}
function IconMoon() {
  return (
    <svg viewBox="0 0 24 24" className={ico} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z" />
    </svg>
  );
}
