"use client";

import { useEffect, useRef, useState } from "react";

import type { Meditation } from "@/lib/core";

function fmt(s: number) {
  if (!Number.isFinite(s) || s <= 0) return "0:00";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}

/**
 * A calm floating player. Wraps a real <audio> element; degrades gracefully to a
 * static visual if the sample file is absent (duration falls back to the meta).
 * Floats above the mobile tab bar, centered on desktop.
 */
export function CoreAudioPlayer({
  meditation,
  playing,
  onTogglePlay,
  onClose,
}: {
  meditation: Meditation;
  playing: boolean;
  onTogglePlay: () => void;
  onClose: () => void;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [cur, setCur] = useState(0);
  const [dur, setDur] = useState(0);
  const [loadError, setLoadError] = useState(false);
  const [loadedId, setLoadedId] = useState(meditation.id);

  // Reset the scrubber + error when the track changes — done during render (the
  // documented "adjust state when a prop changes" pattern) so we don't call
  // setState synchronously inside an effect.
  if (loadedId !== meditation.id) {
    setLoadedId(meditation.id);
    setCur(0);
    setDur(0);
    setLoadError(false);
  }

  // Tell the <audio> element to (re)load the new source (DOM side-effect only).
  useEffect(() => {
    audioRef.current?.load();
  }, [meditation.id]);

  // Drive play/pause from the parent's `playing` state.
  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    if (playing) el.play().catch(() => undefined);
    else el.pause();
  }, [playing, meditation.id]);

  const effectiveDur = dur || meditation.durationMin * 60;
  const pct = effectiveDur ? Math.min(100, (cur / effectiveDur) * 100) : 0;

  // Only seek once metadata has loaded (dur > 0) so the bar never gives false
  // feedback before the audio is actually seekable (preload="none").
  const seekToRatio = (ratio: number) => {
    const el = audioRef.current;
    if (!el || !dur) return;
    const clamped = Math.min(1, Math.max(0, ratio));
    el.currentTime = clamped * dur;
    setCur(clamped * dur);
  };

  const seek = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    seekToRatio((e.clientX - rect.left) / rect.width);
  };

  // Keyboard control for the slider (WCAG 2.1.1): arrows nudge ±5s, Home/End jump.
  const seekBy = (deltaSec: number) => {
    const el = audioRef.current;
    if (!el || !dur) return;
    const from = el.currentTime;
    const next = Math.min(dur, Math.max(0, from + deltaSec));
    el.currentTime = next;
    setCur(next);
  };

  const onScrubKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    switch (e.key) {
      case "ArrowRight":
      case "ArrowUp":
        e.preventDefault();
        seekBy(5);
        break;
      case "ArrowLeft":
      case "ArrowDown":
        e.preventDefault();
        seekBy(-5);
        break;
      case "Home":
        e.preventDefault();
        seekToRatio(0);
        break;
      case "End":
        e.preventDefault();
        seekToRatio(1);
        break;
    }
  };

  return (
    <div className="fixed inset-x-0 bottom-16 z-40 px-3 lg:bottom-5">
      <div className="bottom-sheet-enter mx-auto flex w-full max-w-3xl items-center gap-3 rounded-2xl border border-signal/20 bg-[#10151b]/90 p-2.5 pr-3 shadow-panel backdrop-blur-2xl sm:gap-4 sm:p-3 sm:pr-4">
        <audio
          ref={audioRef}
          preload="none"
          onTimeUpdate={(e) => setCur(e.currentTarget.currentTime)}
          onLoadedMetadata={(e) => {
            setDur(e.currentTarget.duration);
            setLoadError(false);
          }}
          onError={() => setLoadError(true)}
          onEnded={onTogglePlay}
        >
          {meditation.audioUrl && <source src={meditation.audioUrl} type="audio/mpeg" />}
        </audio>

        {/* mini breathing dot */}
        <button
          type="button"
          onClick={onTogglePlay}
          aria-label={playing ? "Pause" : "Play"}
          className="relative grid h-11 w-11 shrink-0 place-items-center rounded-full outline-none focus-visible:ring-2 focus-visible:ring-signal/60"
          style={{
            background: "radial-gradient(circle at 34% 28%, rgba(180,245,238,0.95), #4ECDC4 40%, #6E5FE6 100%)",
            boxShadow: "inset 0 2px 10px rgba(255,255,255,0.35), 0 6px 22px rgba(78,205,196,0.25)",
          }}
        >
          <span className="text-[#0B0F13]/85">
            {playing ? (
              <svg viewBox="0 0 24 24" className="h-5 w-5" fill="currentColor" aria-hidden>
                <rect x="6.5" y="5" width="3.5" height="14" rx="1.2" />
                <rect x="14" y="5" width="3.5" height="14" rx="1.2" />
              </svg>
            ) : (
              <svg viewBox="0 0 24 24" className="ml-0.5 h-5 w-5" fill="currentColor" aria-hidden>
                <path d="M8 5.5v13c0 .8.9 1.3 1.6.9l10-6.5a1 1 0 0 0 0-1.8l-10-6.5A1 1 0 0 0 8 5.5Z" />
              </svg>
            )}
          </span>
        </button>

        {/* title + scrubber */}
        <div className="min-w-0 flex-1">
          <div className="mb-1.5 flex items-center justify-between gap-3">
            <p className="truncate font-display text-sm italic text-ink">{meditation.title}</p>
            {!loadError && (
              <span className="shrink-0 font-mono text-[10px] text-ink-faint">
                {fmt(cur)} <span className="text-ink-faint/50">/ {fmt(effectiveDur)}</span>
              </span>
            )}
          </div>
          {loadError ? (
            <p className="py-1 text-[11px] text-rose-text" role="alert">
              Couldn&rsquo;t load this audio. Please refresh and try again.
            </p>
          ) : (
            <div
              onClick={seek}
              onKeyDown={onScrubKeyDown}
              className="group/scrub flex cursor-pointer items-center rounded-full py-2 outline-none focus-visible:ring-2 focus-visible:ring-signal/60"
              role="slider"
              aria-label="Seek"
              aria-valuenow={Math.round(pct)}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuetext={`${fmt(cur)} of ${fmt(effectiveDur)}`}
              tabIndex={0}
            >
              <div className="relative h-2 w-full rounded-full bg-white/[0.06]">
                <div
                  className="relative h-full rounded-full bg-gradient-to-r from-signal to-accent transition-[width] duration-150"
                  style={{ width: `${pct}%` }}
                >
                  <span className="absolute right-0 top-1/2 h-3 w-3 -translate-y-1/2 translate-x-1/2 rounded-full bg-signal opacity-0 shadow-[0_0_8px_rgba(78,205,196,0.6)] transition-opacity group-hover/scrub:opacity-100 group-focus-visible/scrub:opacity-100" />
                </div>
              </div>
            </div>
          )}
        </div>

        {/* close */}
        <button
          type="button"
          onClick={onClose}
          aria-label="Close player"
          className="grid h-9 w-9 shrink-0 place-items-center rounded-full text-ink-faint transition hover:bg-white/[0.05] hover:text-ink"
        >
          <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>
      </div>
    </div>
  );
}
