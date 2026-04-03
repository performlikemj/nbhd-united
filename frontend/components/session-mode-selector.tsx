"use client";

/* ------------------------------------------------------------------ */
/*  Session Mode Selector                                              */
/*  Replaces the confusing toggle with two explicit selectable cards.  */
/* ------------------------------------------------------------------ */

interface SessionModeSelectorProps {
  value: "main" | "isolated";
  onChange: (value: "main" | "isolated") => void;
}

function ChatBubbleIcon() {
  return (
    <svg
      className="h-5 w-5"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg
      className="h-5 w-5"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
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
      <p className="mb-2 text-sm font-medium text-ink-muted">Session mode</p>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2" role="radiogroup" aria-label="Session mode">
        {/* Main / Foreground */}
        <button
          type="button"
          role="radio"
          aria-checked={value === "main"}
          onClick={() => onChange("main")}
          className={[
            "relative flex flex-col gap-2 rounded-panel p-4 text-left transition-all duration-200 min-h-[44px]",
            value === "main"
              ? "border-2 border-accent bg-accent/[0.06] shadow-[0_0_16px_rgba(124,107,240,0.12)]"
              : "border border-border bg-surface opacity-60 hover:opacity-80 hover:border-border-strong",
          ].join(" ")}
        >
          {/* Radio dot */}
          <span
            className={[
              "absolute top-3 right-3 flex h-4 w-4 items-center justify-center rounded-full border-2 transition-all",
              value === "main"
                ? "border-accent bg-accent"
                : "border-ink-faint",
            ].join(" ")}
          >
            {value === "main" && (
              <span className="block h-1.5 w-1.5 rounded-full bg-white" />
            )}
          </span>

          <span
            className={[
              "flex h-8 w-8 items-center justify-center rounded-lg",
              value === "main" ? "bg-accent/15 text-accent" : "bg-surface-hover text-ink-muted",
            ].join(" ")}
          >
            <ChatBubbleIcon />
          </span>

          <div>
            <p className={[
              "text-sm font-semibold",
              value === "main" ? "text-ink" : "text-ink-muted",
            ].join(" ")}>
              Main
            </p>
            <p className={[
              "mt-0.5 text-xs leading-relaxed",
              value === "main" ? "text-ink-muted" : "text-ink-faint",
            ].join(" ")}>
              Results <strong className={value === "main" ? "text-ink" : "text-ink-muted"}>are shared</strong> with your assistant
            </p>
          </div>

          <p className={[
            "mt-auto text-[11px] font-mono tracking-wide",
            value === "main" ? "text-ink-faint" : "text-ink-faint/60",
          ].join(" ")}>
            Best for: reminders, briefings, summaries
          </p>
        </button>

        {/* Isolated / Background */}
        <button
          type="button"
          role="radio"
          aria-checked={value === "isolated"}
          onClick={() => onChange("isolated")}
          className={[
            "relative flex flex-col gap-2 rounded-panel p-4 text-left transition-all duration-200 min-h-[44px]",
            value === "isolated"
              ? "border-2 border-accent bg-accent/[0.06] shadow-[0_0_16px_rgba(124,107,240,0.12)]"
              : "border border-border bg-surface opacity-60 hover:opacity-80 hover:border-border-strong",
          ].join(" ")}
        >
          {/* Radio dot */}
          <span
            className={[
              "absolute top-3 right-3 flex h-4 w-4 items-center justify-center rounded-full border-2 transition-all",
              value === "isolated"
                ? "border-accent bg-accent"
                : "border-ink-faint",
            ].join(" ")}
          >
            {value === "isolated" && (
              <span className="block h-1.5 w-1.5 rounded-full bg-white" />
            )}
          </span>

          <span
            className={[
              "flex h-8 w-8 items-center justify-center rounded-lg",
              value === "isolated" ? "bg-accent/15 text-accent" : "bg-surface-hover text-ink-muted",
            ].join(" ")}
          >
            <MoonIcon />
          </span>

          <div>
            <p className={[
              "text-sm font-semibold",
              value === "isolated" ? "text-ink" : "text-ink-muted",
            ].join(" ")}>
              Background
            </p>
            <p className={[
              "mt-0.5 text-xs leading-relaxed",
              value === "isolated" ? "text-ink-muted" : "text-ink-faint",
            ].join(" ")}>
              Results <strong className={value === "isolated" ? "text-ink" : "text-ink-muted"}>are not shared</strong> with your assistant
            </p>
          </div>

          <p className={[
            "mt-auto text-[11px] font-mono tracking-wide",
            value === "isolated" ? "text-ink-faint" : "text-ink-faint/60",
          ].join(" ")}>
            Best for: logging, file updates, data collection
          </p>
        </button>
      </div>
    </div>
  );
}
