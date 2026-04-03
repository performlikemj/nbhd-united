"use client";

/* ------------------------------------------------------------------ */
/*  Session Mode Selector                                              */
/*  Two explicit cards replacing the ambiguous toggle.                 */
/*  Design reference: Stitch "Cerebral Flow" scheduled task panel.     */
/* ------------------------------------------------------------------ */

interface SessionModeSelectorProps {
  value: "main" | "isolated";
  onChange: (value: "main" | "isolated") => void;
}

function ChatBubbleIcon({ filled }: { filled?: boolean }) {
  return filled ? (
    <svg className="h-5 w-5" viewBox="0 0 24 24" fill="currentColor">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  ) : (
    <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}

export default function SessionModeSelector({
  value,
  onChange,
}: SessionModeSelectorProps) {
  return (
    <div>
      <p className="mb-3 text-sm font-medium text-ink-muted">Session mode</p>
      <div
        className="grid grid-cols-1 gap-4 md:grid-cols-2"
        role="radiogroup"
        aria-label="Session mode"
      >
        {/* ── Foreground (main) ── */}
        <button
          type="button"
          role="radio"
          aria-checked={value === "main"}
          onClick={() => onChange("main")}
          className={[
            "group relative flex items-start gap-4 rounded-xl p-5 text-left transition-all duration-200 min-h-[44px]",
            value === "main"
              ? "border-2 border-accent bg-surface-elevated shadow-[0_0_24px_rgba(124,107,240,0.18)]"
              : "border border-border/40 bg-card/40 hover:bg-card/60 hover:border-border/60",
          ].join(" ")}
        >
          <span
            className={[
              "flex h-10 w-10 shrink-0 items-center justify-center rounded-lg transition-colors",
              value === "main"
                ? "bg-accent/20 text-accent"
                : "bg-surface-hover text-ink-faint group-hover:text-accent/60",
            ].join(" ")}
          >
            <ChatBubbleIcon filled={value === "main"} />
          </span>

          <div className="min-w-0">
            <h4
              className={[
                "font-headline font-semibold text-sm",
                value === "main" ? "text-accent" : "text-ink-muted",
              ].join(" ")}
            >
              Foreground
            </h4>
            <p
              className={[
                "mt-1 text-xs leading-relaxed",
                value === "main" ? "text-ink" : "text-ink-faint/70",
              ].join(" ")}
            >
              Runs visibly. Results ARE shared with your assistant.
            </p>
            <p
              className={[
                "mt-3 text-[10px] tracking-wide",
                value === "main" ? "text-accent/60" : "text-ink-faint/40",
              ].join(" ")}
            >
              Best for: reminders, briefings, daily summaries
            </p>
          </div>
        </button>

        {/* ── Background (isolated) ── */}
        <button
          type="button"
          role="radio"
          aria-checked={value === "isolated"}
          onClick={() => onChange("isolated")}
          className={[
            "group relative flex items-start gap-4 rounded-xl p-5 text-left transition-all duration-200 min-h-[44px]",
            value === "isolated"
              ? "border-2 border-accent bg-surface-elevated shadow-[0_0_24px_rgba(124,107,240,0.18)]"
              : "border border-border/40 bg-card/40 hover:bg-card/60 hover:border-border/60",
          ].join(" ")}
        >
          <span
            className={[
              "flex h-10 w-10 shrink-0 items-center justify-center rounded-lg transition-colors",
              value === "isolated"
                ? "bg-accent/20 text-accent"
                : "bg-surface-hover text-ink-faint group-hover:text-accent/60",
            ].join(" ")}
          >
            <MoonIcon />
          </span>

          <div className="min-w-0">
            <h4
              className={[
                "font-headline font-semibold text-sm",
                value === "isolated" ? "text-accent" : "text-ink-muted",
              ].join(" ")}
            >
              Background
            </h4>
            <p
              className={[
                "mt-1 text-xs leading-relaxed",
                value === "isolated" ? "text-ink" : "text-ink-faint/70",
              ].join(" ")}
            >
              Runs silently. Results are NOT shared with your assistant.
            </p>
            <p
              className={[
                "mt-3 text-[10px] tracking-wide",
                value === "isolated" ? "text-accent/60" : "text-ink-faint/40",
              ].join(" ")}
            >
              Best for: logging, file updates, data collection
            </p>
          </div>
        </button>
      </div>
    </div>
  );
}
