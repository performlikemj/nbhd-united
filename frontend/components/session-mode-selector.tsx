"use client";

/* ------------------------------------------------------------------ */
/*  Session Mode Selector                                              */
/*  Two explicit cards replacing the ambiguous toggle.                 */
/*  Design reference: Stitch "Cerebral Flow" scheduled task panel.     */
/*                                                                     */
/*  Under the universal isolation cron model, every task runs          */
/*  isolated. The choice here is whether the task pushes a Phase 2     */
/*  summary back into the main session after running:                  */
/*    foreground=true  → reports back to assistant if it sent the      */
/*                       user a message                                */
/*    foreground=false → silent, never reports back                    */
/* ------------------------------------------------------------------ */

interface SessionModeSelectorProps {
  value: boolean; // foreground
  onChange: (foreground: boolean) => void;
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
  const isForeground = value;
  const isBackground = !value;
  return (
    <div>
      <p className="mb-3 text-sm font-medium text-ink-muted">Session mode</p>
      <div
        className="grid grid-cols-1 gap-4 md:grid-cols-2"
        role="radiogroup"
        aria-label="Session mode"
      >
        {/* ── Foreground ── */}
        <button
          type="button"
          role="radio"
          aria-checked={isForeground}
          onClick={() => onChange(true)}
          className={[
            "group relative flex items-start gap-4 rounded-xl p-5 text-left transition-all duration-200 min-h-[44px]",
            isForeground
              ? "border-2 border-accent bg-surface-elevated shadow-[0_0_24px_rgba(124,107,240,0.18)]"
              : "border border-border/40 bg-card/40 hover:bg-card/60 hover:border-border/60",
          ].join(" ")}
        >
          <span
            className={[
              "flex h-10 w-10 shrink-0 items-center justify-center rounded-lg transition-colors",
              isForeground
                ? "bg-accent/20 text-accent"
                : "bg-surface-hover text-ink-faint group-hover:text-accent/60",
            ].join(" ")}
          >
            <ChatBubbleIcon filled={isForeground} />
          </span>

          <div className="min-w-0">
            <h4
              className={[
                "font-headline font-semibold text-sm",
                isForeground ? "text-accent" : "text-ink-muted",
              ].join(" ")}
            >
              Foreground
            </h4>
            <p
              className={[
                "mt-1 text-xs leading-relaxed",
                isForeground ? "text-ink" : "text-ink-faint/70",
              ].join(" ")}
            >
              Reports back to your assistant if it sends you a message.
            </p>
            <p
              className={[
                "mt-3 text-[10px] tracking-wide",
                isForeground ? "text-accent/60" : "text-ink-faint/40",
              ].join(" ")}
            >
              Best for: reminders, briefings, daily summaries
            </p>
          </div>
        </button>

        {/* ── Background ── */}
        <button
          type="button"
          role="radio"
          aria-checked={isBackground}
          onClick={() => onChange(false)}
          className={[
            "group relative flex items-start gap-4 rounded-xl p-5 text-left transition-all duration-200 min-h-[44px]",
            isBackground
              ? "border-2 border-accent bg-surface-elevated shadow-[0_0_24px_rgba(124,107,240,0.18)]"
              : "border border-border/40 bg-card/40 hover:bg-card/60 hover:border-border/60",
          ].join(" ")}
        >
          <span
            className={[
              "flex h-10 w-10 shrink-0 items-center justify-center rounded-lg transition-colors",
              isBackground
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
                isBackground ? "text-accent" : "text-ink-muted",
              ].join(" ")}
            >
              Background
            </h4>
            <p
              className={[
                "mt-1 text-xs leading-relaxed",
                isBackground ? "text-ink" : "text-ink-faint/70",
              ].join(" ")}
            >
              Runs silently. Never reports back to your assistant.
            </p>
            <p
              className={[
                "mt-3 text-[10px] tracking-wide",
                isBackground ? "text-accent/60" : "text-ink-faint/40",
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
