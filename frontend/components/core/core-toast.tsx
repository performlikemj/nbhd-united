"use client";

/**
 * The "your meditation is ready" notification — mirrors the channel ping the
 * backend sends on render completion (Telegram/LINE). Slides in top-right.
 */
export function CoreToast({
  title,
  channel = "Telegram",
  onPlay,
  onDismiss,
}: {
  title: string;
  channel?: string;
  onPlay: () => void;
  onDismiss: () => void;
}) {
  return (
    <div
      role="status"
      className="animate-reveal fixed right-4 top-[4.5rem] z-[60] flex w-[340px] max-w-[calc(100vw-2rem)] items-center gap-3 rounded-2xl border border-signal/25 bg-[#10151b]/95 p-3 pl-3.5 shadow-panel backdrop-blur-2xl"
    >
      <span
        className="relative h-8 w-8 shrink-0 rounded-full"
        style={{
          background: "radial-gradient(circle at 34% 28%, rgba(180,245,238,0.95), #4ECDC4 40%, #6E5FE6 100%)",
          boxShadow: "0 0 14px rgba(78,205,196,0.45)",
        }}
      >
        <span className="absolute -bottom-1 -right-1 grid h-4 w-4 place-items-center rounded-full border-2 border-[#10151b] bg-[#229ED9]">
          <svg viewBox="0 0 24 24" className="h-2 w-2" fill="#fff" aria-hidden>
            <path d="M21.6 2.4 2.3 10.1c-1 .4-1 1.8.1 2.1l5 1.5 1.9 6c.3.8 1.3 1 1.9.3l2.6-2.6 4.7 3.5c.7.5 1.7.1 1.9-.8L23.4 4c.2-1.1-.8-2-1.8-1.6Z" />
          </svg>
        </span>
      </span>

      <div className="min-w-0 flex-1">
        <p className="text-[13px] font-semibold text-ink">Your ten minutes is ready</p>
        <p className="mt-0.5 truncate text-[11px] text-ink-faint">
          {title} · via {channel}
        </p>
      </div>

      <button
        type="button"
        onClick={onPlay}
        className="shrink-0 rounded-full bg-signal px-3.5 py-1.5 text-xs font-semibold text-[#0b0f13] transition hover:brightness-110"
      >
        Play
      </button>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss"
        className="grid h-6 w-6 shrink-0 place-items-center rounded-full text-ink-faint transition hover:bg-white/5 hover:text-ink"
      >
        <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
          <path d="M6 6l12 12M18 6L6 18" />
        </svg>
      </button>
    </div>
  );
}
