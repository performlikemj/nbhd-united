"use client";

/**
 * The "your meditation is ready" notification — mirrors the push the backend
 * sends on render completion. Slides in top-right.
 */
export function CoreToast({
  title,
  onPlay,
  onDismiss,
}: {
  title: string;
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
        <span className="absolute -bottom-1 -right-1 grid h-4 w-4 place-items-center rounded-full border-2 border-[#10151b] bg-signal">
          <svg viewBox="0 0 24 24" className="h-2.5 w-2.5" fill="#0b0f13" aria-hidden>
            <path d="M12 22a2.2 2.2 0 0 0 2.2-2.2H9.8A2.2 2.2 0 0 0 12 22Zm6.4-6.2V11a6.4 6.4 0 0 0-4.9-6.2V4.3a1.5 1.5 0 0 0-3 0v.5A6.4 6.4 0 0 0 5.6 11v4.8L4 17.4v.8h16v-.8Z" />
          </svg>
        </span>
      </span>

      <div className="min-w-0 flex-1">
        <p className="text-[13px] font-semibold text-ink">Your ten minutes is ready</p>
        <p className="mt-0.5 truncate text-[11px] text-ink-faint">
          {title}
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
