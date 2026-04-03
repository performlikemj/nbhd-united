"use client";

/* ------------------------------------------------------------------ */
/*  Session Mode Selector                                              */
/*  Two explicit cards replacing the ambiguous toggle.                 */
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
        className="grid grid-cols-1 gap-3 sm:grid-cols-2"
        role="radiogroup"
        aria-label="Session mode"
      >
        {/* ── Main / Foreground ── */}
        <button
          type="button"
          role="radio"
          aria-checked={value === "main"}
          onClick={() => onChange("main")}
          className={[
            "group flex items-start gap-4 rounded-panel p-5 text-left transition-all duration-200 min-h-[44px]",
            value === "main"
              ? "border-2 border-accent bg-surface-elevated shadow-[0_0_20px_rgba(124,107,240,0.15)]"
              : "border border-border bg-surface-hover/50 hover:bg-surface-hover hover:border-border-strong",
          ].join(" ")}
        >
          <span
            className={[
              "flex h-10 w-10 shrink-0 items-center justify-center rounded-lg transition-colors",
              value === "main"
                ? "bg-accent/20 text-accent"
                : "bg-surface-hover text-ink-faint group-hover:text-ink-muted",
            ].join(" ")}
          >
            <ChatBubbleIcon filled={value === "main"} />
          </span>

          <div className="min-w-0">
            <p
              className={[
                "font-headline text-sm font-semibold",
                value === "main" ? "text-accent" : "text-ink-muted",
              ].join(" ")}
            >
              Main
            </p>
            <p
              className={[
                "mt-1 text-xs leading-relaxed",
                value === "main" ? "text-ink" : "text-ink-faint",
              ].join(" ")}
            >
              Runs visibly. Results{" "}
              <strong className={value === "main" ? "text-ink" : "text-ink-muted"}>
                are shared
              </strong>{" "}
              with your assistant.
            </p>
            <p
              className={[
                "mt-3 text-[10px] font-mono tracking-wide",
                value === "main" ? "text-accent/50" : "text-ink-faint/50",
              ].join(" ")}
            >
              Best for: reminders, briefings, summaries
            </p>
          </div>
        </button>

        {/* ── Isolated / Background ── */}
        <button
          type="button"
          role="radio"
          aria-checked={value === "isolated"}
          onClick={() => onChange("isolated")}
          className={[
            "group flex items-start gap-4 rounded-panel p-5 text-left transition-all duration-200 min-h-[44px]",
            value === "isolated"
              ? "border-2 border-accent bg-surface-elevated shadow-[0_0_20px_rgba(124,107,240,0.15)]"
              : "border border-border bg-surface-hover/50 hover:bg-surface-hover hover:border-border-strong",
          ].join(" ")}
        >
          <span
            className={[
              "flex h-10 w-10 shrink-0 items-center justify-center rounded-lg transition-colors",
              value === "isolated"
                ? "bg-accent/20 text-accent"
                : "bg-surface-hover text-ink-faint group-hover:text-ink-muted",
            ].join(" ")}
          >
            <MoonIcon />
          </span>

          <div className="min-w-0">
            <p
              className={[
                "font-headline text-sm font-semibold",
                value === "isolated" ? "text-accent" : "text-ink-muted",
              ].join(" ")}
            >
              Background
            </p>
            <p
              className={[
                "mt-1 text-xs leading-relaxed",
                value === "isolated" ? "text-ink" : "text-ink-faint",
              ].join(" ")}
            >
              Runs silently. Results{" "}
              <strong className={value === "isolated" ? "text-ink" : "text-ink-muted"}>
                are not shared
              </strong>{" "}
              with your assistant.
            </p>
            <p
              className={[
                "mt-3 text-[10px] font-mono tracking-wide",
                value === "isolated" ? "text-accent/50" : "text-ink-faint/50",
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
