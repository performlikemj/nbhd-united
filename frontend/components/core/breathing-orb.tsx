"use client";

import clsx from "clsx";

/**
 * The signature Core element: a softly glowing orb that slowly "breathes".
 * Doubles as the primary Begin / play-pause control. Teal→purple gradient ties
 * mindfulness (signal/teal) to the app's accent (purple). Reduced-motion safe
 * (the .core-breathe* classes disable their animation under prefers-reduced-motion).
 */
export function BreathingOrb({
  playing = false,
  compose = false,
  onClick,
  size = 208,
  label,
}: {
  playing?: boolean;
  /** show the on-demand "+" affordance instead of play/pause */
  compose?: boolean;
  onClick?: () => void;
  size?: number;
  label?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label ?? (compose ? "Compose today's meditation" : playing ? "Pause meditation" : "Begin meditation")}
      className="group relative grid place-items-center rounded-full outline-none focus-visible:ring-2 focus-visible:ring-signal/60 focus-visible:ring-offset-4 focus-visible:ring-offset-[#0B0F13]"
      style={{ width: size, height: size }}
    >
      {/* expanding breath rings */}
      <span className="core-breathe-ring pointer-events-none absolute inset-0 rounded-full border border-signal/30" />
      <span
        className="core-breathe-ring pointer-events-none absolute inset-0 rounded-full border border-accent/25"
        style={{ animationDelay: "2.3s" }}
      />
      <span
        className="core-breathe-ring pointer-events-none absolute inset-0 rounded-full border border-signal/20"
        style={{ animationDelay: "4.6s" }}
      />

      {/* soft outer halo */}
      <span
        className="core-breathe-glow pointer-events-none absolute inset-[-14%] rounded-full blur-2xl"
        style={{
          background:
            "radial-gradient(circle, rgba(78,205,196,0.50), rgba(124,107,240,0.30) 55%, transparent 72%)",
        }}
      />

      {/* orb body */}
      <span
        className="core-breathe relative grid place-items-center rounded-full transition-transform duration-300 group-hover:scale-[1.03] group-active:scale-[0.98]"
        style={{
          width: "76%",
          height: "76%",
          background:
            "radial-gradient(circle at 34% 28%, rgba(180,245,238,0.95), #4ECDC4 38%, #6E5FE6 100%)",
          boxShadow:
            "inset 0 3px 22px rgba(255,255,255,0.40), inset 0 -8px 26px rgba(11,15,19,0.30), 0 18px 60px rgba(78,205,196,0.28)",
        }}
      >
        {/* glossy highlight */}
        <span
          className="pointer-events-none absolute left-[16%] top-[12%] h-[28%] w-[40%] rounded-full blur-md"
          style={{ background: "radial-gradient(circle, rgba(255,255,255,0.55), transparent 70%)" }}
        />
        {compose ? <ComposeIcon /> : <PlayPauseIcon playing={playing} />}
      </span>
    </button>
  );
}

function ComposeIcon() {
  return (
    <span className="relative z-10 text-[#0B0F13]/85" aria-hidden>
      <svg viewBox="0 0 24 24" className="h-9 w-9" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
        <path d="M12 6v12M6 12h12" />
      </svg>
    </span>
  );
}

function PlayPauseIcon({ playing }: { playing: boolean }) {
  return (
    <span
      className={clsx(
        "relative z-10 text-[#0B0F13]/85 transition-opacity duration-200",
        playing ? "opacity-90" : "opacity-95",
      )}
    >
      {playing ? (
        <svg viewBox="0 0 24 24" className="h-9 w-9" fill="currentColor" aria-hidden>
          <rect x="6.5" y="5" width="3.5" height="14" rx="1.2" />
          <rect x="14" y="5" width="3.5" height="14" rx="1.2" />
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" className="ml-1 h-10 w-10" fill="currentColor" aria-hidden>
          <path d="M8 5.2v13.6c0 .9 1 1.45 1.76.97l10.5-6.8a1.15 1.15 0 0 0 0-1.94l-10.5-6.8A1.15 1.15 0 0 0 8 5.2Z" />
        </svg>
      )}
    </span>
  );
}
