"use client";

import clsx from "clsx";

import type { Meditation } from "@/lib/core";

/**
 * The library of past meditations. Each card: date eyebrow, lyrical serif title,
 * one-line theme, duration pill, and a play affordance that becomes a small
 * equalizer when that meditation is the one playing.
 */
export function MeditationLibrary({
  items,
  currentId,
  playing,
  onPlay,
}: {
  items: Meditation[];
  currentId?: string | null;
  playing?: boolean;
  onPlay: (m: Meditation) => void;
}) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {items.map((m, i) => {
        const active = currentId === m.id;
        return (
          <button
            key={m.id}
            type="button"
            onClick={() => onPlay(m)}
            style={{ animation: `reveal 420ms ease-out ${120 + i * 80}ms both` }}
            className={clsx(
              "group flex items-center gap-4 rounded-2xl border bg-surface/60 p-4 text-left transition-all duration-300 hover:-translate-y-0.5 hover:bg-surface-hover",
              active ? "border-signal/40 shadow-[0_0_0_1px_rgba(78,205,196,0.15)]" : "border-border hover:border-border-strong",
            )}
          >
            {/* play / equalizer chip */}
            <span
              className={clsx(
                "grid h-12 w-12 shrink-0 place-items-center rounded-full border transition-colors",
                active
                  ? "border-signal/40 bg-signal-faint text-signal"
                  : "border-border bg-surface-elevated text-ink-muted group-hover:text-signal",
              )}
            >
              {active && playing ? <Equalizer /> : <PlayGlyph />}
            </span>

            <span className="min-w-0 flex-1">
              <span className="mb-0.5 flex items-center gap-2">
                <span className="text-[10px] uppercase tracking-[0.16em] text-ink-faint">{m.dateLabel}</span>
                <span className="h-1 w-1 rounded-full bg-ink-faint/50" />
                <span className="font-mono text-[10px] text-ink-faint">{m.durationMin} min</span>
              </span>
              <span className="block truncate font-display text-lg italic text-ink">{m.title}</span>
              <span className="mt-0.5 block truncate text-xs text-ink-muted">{m.theme}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function PlayGlyph() {
  return (
    <svg viewBox="0 0 24 24" className="ml-0.5 h-5 w-5" fill="currentColor" aria-hidden>
      <path d="M8 5.5v13c0 .8.9 1.3 1.6.9l10-6.5a1 1 0 0 0 0-1.8l-10-6.5A1 1 0 0 0 8 5.5Z" />
    </svg>
  );
}

function Equalizer() {
  return (
    <span className="flex h-4 items-end gap-[3px]" aria-hidden>
      {[0, 1, 2, 3].map((n) => (
        <span
          key={n}
          className="w-[3px] rounded-full bg-signal"
          style={{ height: "100%", animation: `eqbar 900ms ease-in-out ${n * 120}ms infinite` }}
        />
      ))}
    </span>
  );
}
