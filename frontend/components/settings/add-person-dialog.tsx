"use client";

import { useEffect, useRef, useState } from "react";

import type {
  AddEntityRegistryInput,
  AddEntityRegistryResponse,
  EntityRegistryAddResult,
} from "@/lib/api";

interface AddPersonDialogProps {
  /** Disables the controls and suppresses close-while-in-flight. */
  busy: boolean;
  /**
   * Fires the POST. Resolves to the discriminated add-result — a created/exists
   * binding, OR a hygiene warning the dialog must confirm before retrying.
   * Rejects only on a real failure (400 validation, network); we render that
   * inline.
   */
  onSubmit: (input: AddEntityRegistryInput) => Promise<AddEntityRegistryResponse>;
  /** A binding was minted or matched — the page closes, invalidates, confirms. */
  onSuccess: (result: {
    status: "created" | "exists";
    entry: EntityRegistryAddResult;
  }) => void;
  onCancel: () => void;
}

type EntityType = "PERSON" | "LOCATION";

const inputClass =
  "mt-1 w-full rounded-xl border border-white/10 bg-white/[0.05] px-4 py-3 text-sm text-[#e0e3e8] outline-none placeholder:text-white/25 focus:border-[#5dd9d0]/50 focus:shadow-[0_0_8px_rgba(93,217,208,0.15)] transition";
const labelClass = "font-mono text-[10px] uppercase tracking-[0.14em] text-white/40";

/**
 * Manually add (or re-affirm) a known PII entity.
 *
 * Shares BulkDeletePeopleDialog's visual vocabulary — bottom-sheet on mobile,
 * centered panel on desktop, Escape-to-close, focus-on-open — so the app reads
 * as one system. Two phases in one panel: the entry form, and a warning-confirm
 * state the backend can trigger (422) when a name looks like a common-word
 * footgun. "Hide it anyway?" retries the same payload acknowledged.
 *
 * Mount it only while open (`{addDialogOpen && <AddPersonDialog … />}`) — it
 * relies on a fresh mount for a clean form rather than resetting state in an
 * effect, which the React compiler lint (set-state-in-effect) disallows.
 */
export function AddPersonDialog({ busy, onSubmit, onSuccess, onCancel }: AddPersonDialogProps) {
  const [name, setName] = useState("");
  const [entityType, setEntityType] = useState<EntityType>("PERSON");
  const [relationship, setRelationship] = useState("");
  const [notes, setNotes] = useState("");
  const [phase, setPhase] = useState<"form" | "warning">("form");
  const [warning, setWarning] = useState("");
  const [error, setError] = useState<string | null>(null);

  const nameRef = useRef<HTMLInputElement>(null);

  // External-system side effects only (no setState): lock body scroll, wire
  // Escape-to-close, and focus the first field once the open animation settles.
  // Mirrors BulkDeletePeopleDialog.
  useEffect(() => {
    document.body.style.overflow = "hidden";
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onCancel();
    };
    document.addEventListener("keydown", onKeyDown);
    const focusTimer = setTimeout(() => nameRef.current?.focus(), 20);
    return () => {
      document.body.style.overflow = "";
      document.removeEventListener("keydown", onKeyDown);
      clearTimeout(focusTimer);
    };
  }, [busy, onCancel]);

  const trimmedName = name.trim();
  const canSubmit = trimmedName.length > 0 && trimmedName.length <= 256 && !busy;

  const submit = async (acknowledge: boolean) => {
    setError(null);
    try {
      const res = await onSubmit({
        name: trimmedName,
        entity_type: entityType,
        relationship: relationship.trim() || undefined,
        notes: notes.trim() || undefined,
        acknowledge_warning: acknowledge,
      });
      if (res.status === "warning") {
        setWarning(res.warning);
        setPhase("warning");
        return;
      }
      onSuccess(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add this entry.");
    }
  };

  const kindNoun = entityType === "PERSON" ? "person" : "place";

  return (
    <div className="fixed inset-0 z-[100] flex items-end md:items-center justify-center">
      {/* Backdrop — click-outside-to-close (the panel sits above). */}
      <div
        className="absolute inset-0 bg-overlay/60 backdrop-blur-sm"
        onClick={() => {
          if (!busy) onCancel();
        }}
      />

      <div
        className="relative z-10 w-full bg-surface-elevated border-t border-border md:border md:rounded-2xl md:m-4 md:max-w-md bottom-sheet-enter md:bottom-auto motion-reduce:animate-none"
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-person-title"
      >
        {/* Handle on mobile */}
        <div className="flex justify-center pt-3 md:hidden">
          <div className="h-1 w-10 rounded-full bg-white/10" />
        </div>

        <div className="p-6 md:p-5">
          {phase === "form" ? (
            <>
              <h3 id="add-person-title" className="text-base font-semibold text-ink">
                Add a person or place
              </h3>
              <p className="mt-2 text-sm text-ink-muted leading-relaxed">
                Hide a name your assistant hasn&rsquo;t picked up on its own. Matching
                is by exact word, so nicknames, other spellings, and first-name vs.
                full-name each need their own entry.
              </p>

              <form
                className="mt-4 space-y-4"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (canSubmit) void submit(false);
                }}
              >
                {/* Type toggle */}
                <div>
                  <span className={labelClass}>Type</span>
                  <div className="mt-1 inline-flex rounded-xl border border-border p-1">
                    {(
                      [
                        ["PERSON", "Person"],
                        ["LOCATION", "Place"],
                      ] as const
                    ).map(([value, label]) => {
                      const active = entityType === value;
                      return (
                        <button
                          key={value}
                          type="button"
                          onClick={() => setEntityType(value)}
                          disabled={busy}
                          aria-pressed={active}
                          className={[
                            "rounded-lg px-4 py-2 text-sm font-medium transition min-h-[44px] disabled:cursor-not-allowed disabled:opacity-50",
                            active
                              ? "bg-accent text-white"
                              : "bg-transparent text-ink-muted hover:text-ink",
                          ].join(" ")}
                        >
                          {label}
                        </button>
                      );
                    })}
                  </div>
                </div>

                <label className="block">
                  <span className={labelClass}>
                    {entityType === "PERSON" ? "Name" : "Place name"} (required)
                  </span>
                  <input
                    ref={nameRef}
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    maxLength={256}
                    disabled={busy}
                    placeholder={entityType === "PERSON" ? "e.g. Sarah Chen" : "e.g. Cedar Street"}
                    className={inputClass}
                  />
                </label>

                <label className="block">
                  <span className={labelClass}>Relationship (optional)</span>
                  <input
                    type="text"
                    value={relationship}
                    onChange={(e) => setRelationship(e.target.value)}
                    disabled={busy}
                    placeholder={entityType === "PERSON" ? "e.g. daughter, coworker" : "e.g. home, office"}
                    className={inputClass}
                  />
                </label>

                <label className="block">
                  <span className={labelClass}>Notes (optional)</span>
                  <textarea
                    value={notes}
                    onChange={(e) => setNotes(e.target.value)}
                    disabled={busy}
                    rows={2}
                    placeholder="e.g. 4.5 years old, into Roblox"
                    className={`${inputClass} resize-none`}
                  />
                </label>

                {error && (
                  <div
                    role="alert"
                    className="rounded-xl border border-rose-border bg-rose-bg px-3 py-2 text-xs text-rose-text"
                  >
                    {error}
                  </div>
                )}

                <div className="flex items-center gap-3 pt-1">
                  <button
                    type="button"
                    onClick={onCancel}
                    disabled={busy}
                    className="flex-1 rounded-xl border border-border px-4 py-2.5 text-sm font-medium text-ink-muted transition hover:bg-surface-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={!canSubmit}
                    className="glow-purple flex-1 rounded-xl bg-accent px-4 py-2.5 text-sm font-semibold text-white transition hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
                  >
                    {busy ? "Adding…" : `Add ${kindNoun}`}
                  </button>
                </div>
              </form>
            </>
          ) : (
            <>
              <h3 id="add-person-title" className="text-base font-semibold text-ink">
                Hide &ldquo;{trimmedName}&rdquo; anyway?
              </h3>

              <div
                role="alert"
                className="mt-3 rounded-xl border border-amber-border bg-amber-bg px-3 py-2.5 text-sm text-amber-text leading-relaxed"
              >
                {warning}
              </div>

              <p className="mt-3 text-sm text-ink-muted leading-relaxed">
                Hiding a common word can redact ordinary text in your messages. You can
                hide it anyway, or go back and change the name.
              </p>

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
                  type="button"
                  onClick={() => {
                    setError(null);
                    setPhase("form");
                  }}
                  disabled={busy}
                  className="flex-1 rounded-xl border border-border px-4 py-2.5 text-sm font-medium text-ink-muted transition hover:bg-surface-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
                >
                  Back
                </button>
                <button
                  type="button"
                  onClick={() => void submit(true)}
                  disabled={busy}
                  className="flex-1 rounded-xl border border-amber-border bg-amber-bg px-4 py-2.5 text-sm font-medium text-amber-text transition hover:brightness-125 disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
                >
                  {busy ? "Hiding…" : "Hide it anyway"}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
