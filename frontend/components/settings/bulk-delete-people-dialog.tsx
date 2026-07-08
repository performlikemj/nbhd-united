"use client";

import { useEffect, useRef } from "react";

interface BulkDeletePeopleDialogProps {
  open: boolean;
  /** How many bindings will be deleted (the full selection, names or not). */
  count: number;
  /** Checkbox state — also add these names to the Ignore list on delete. */
  alsoIgnore: boolean;
  onAlsoIgnoreChange: (value: boolean) => void;
  /** Disables the controls and suppresses close-while-in-flight. */
  busy: boolean;
  /** Optional error surfaced under the actions (e.g. a failed request). */
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Destructive confirmation for bulk-deleting PII bindings.
 *
 * We can't reuse the journal ConfirmDialog here: this needs a live checkbox
 * (the "also ignore" toggle) inside the body, which that component's
 * string-only `message` prop can't express. It mirrors ConfirmDialog's visual
 * vocabulary (bottom-sheet on mobile, centered panel on desktop) so the app
 * reads as one system, and adds Escape-to-close + focus-on-open that the
 * simpler component lacks.
 */
export function BulkDeletePeopleDialog({
  open,
  count,
  alsoIgnore,
  onAlsoIgnoreChange,
  busy,
  error,
  onConfirm,
  onCancel,
}: BulkDeletePeopleDialogProps) {
  const overlayRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  // Body scroll lock + Escape-to-close + focus-on-open. Focus lands on Cancel
  // (never the destructive button) so a stray Enter can't delete.
  useEffect(() => {
    if (!open) return;
    document.body.style.overflow = "hidden";
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onCancel();
    };
    document.addEventListener("keydown", onKeyDown);
    cancelRef.current?.focus();
    return () => {
      document.body.style.overflow = "";
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, busy, onCancel]);

  if (!open) return null;

  const noun = count === 1 ? "binding" : "bindings";

  return (
    <div
      ref={overlayRef}
      className="fixed inset-0 z-[100] flex items-end md:items-center justify-center"
    >
      {/* Backdrop — clicks land here (the panel sits above), so this is the
          click-outside-to-close surface. */}
      <div
        className="absolute inset-0 bg-overlay/60 backdrop-blur-sm"
        onClick={() => {
          if (!busy) onCancel();
        }}
      />

      {/* Dialog */}
      <div
        className="relative z-10 w-full bg-surface-elevated border-t border-border md:border md:rounded-2xl md:m-4 md:max-w-md bottom-sheet-enter md:bottom-auto motion-reduce:animate-none"
        role="dialog"
        aria-modal="true"
        aria-labelledby="bulk-delete-title"
        aria-describedby="bulk-delete-body"
      >
        {/* Handle on mobile */}
        <div className="flex justify-center pt-3 md:hidden">
          <div className="h-1 w-10 rounded-full bg-white/10" />
        </div>

        <div className="p-6 md:p-5">
          <h3 id="bulk-delete-title" className="text-base font-semibold text-ink">
            Delete {count} {noun}?
          </h3>
          <p
            id="bulk-delete-body"
            className="mt-2 text-sm text-ink-muted leading-relaxed"
          >
            This removes {count === 1 ? "this binding" : "these bindings"} from the
            registry. Old journal entries and chat history that still contain the
            placeholder can no longer be translated back to the real name — the
            placeholder will show through as-is.
          </p>

          <label className="mt-4 flex cursor-pointer select-none items-start gap-3 rounded-xl border border-border bg-surface/60 px-3 py-3">
            <input
              type="checkbox"
              checked={alsoIgnore}
              onChange={(e) => onAlsoIgnoreChange(e.target.checked)}
              disabled={busy}
              className="mt-0.5 h-4 w-4 rounded border-white/20 bg-white/[0.05] accent-accent disabled:cursor-not-allowed disabled:opacity-50"
            />
            <span className="text-sm text-ink-muted">
              Also add these to the Ignore list so they stop being redacted in future
              messages.{" "}
              <span className="text-ink-faint">
                Deleting alone doesn&rsquo;t stop redaction — your assistant may mint a
                new placeholder for the same name again.
              </span>
            </span>
          </label>

          {error && (
            <div
              role="alert"
              className="mt-4 rounded-xl border border-rose-border bg-rose-bg px-3 py-2 text-xs text-rose-text"
            >
              {error}
            </div>
          )}

          <div className="mt-6 flex items-center gap-3">
            <button
              ref={cancelRef}
              type="button"
              onClick={onCancel}
              disabled={busy}
              className="flex-1 rounded-xl border border-border px-4 py-2.5 text-sm font-medium text-ink-muted transition hover:bg-surface-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={onConfirm}
              disabled={busy}
              className="flex-1 rounded-xl border border-rose-border bg-rose-bg px-4 py-2.5 text-sm font-medium text-rose-text transition hover:brightness-125 disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
            >
              {busy ? "Deleting…" : `Delete ${count} ${noun}`}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
