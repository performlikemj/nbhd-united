"use client";

import { useState } from "react";

import { useCoreProfileQuery, useUpdateCoreProfileMutation } from "@/lib/queries";

/**
 * The consent channel for Core: a gentle, optional space where the user can tell
 * their assistant what they'd like their meditations to hold. Writes to
 * CoreProfile.additional_context. Nothing here is required — the copy makes that
 * explicit ("offered, never required").
 */
export function CoreContextPanel() {
  const { data: profile } = useCoreProfileQuery();
  const mutation = useUpdateCoreProfileMutation();

  const [value, setValue] = useState("");
  const [saved, setSaved] = useState("");
  const [seededFrom, setSeededFrom] = useState<string | null>(null);
  const [error, setError] = useState(false);
  const [justSaved, setJustSaved] = useState(false);

  // Seed the textarea from the profile the moment it arrives (and re-sync if it
  // changes upstream) — done during render via the documented "adjust state when
  // a prop changes" pattern (mirrors audio-player.tsx), not in an effect. Typing
  // is never clobbered: while the user edits, the profile is unchanged, so the
  // guard below stays false.
  const incoming = profile ? (profile.additional_context ?? "") : null;
  if (incoming !== null && incoming !== seededFrom) {
    setSeededFrom(incoming);
    setValue(incoming);
    setSaved(incoming);
  }

  const dirty = value.trim() !== saved.trim();

  const save = async () => {
    setError(false);
    try {
      const updated = await mutation.mutateAsync({ additional_context: value.trim() });
      setSaved(updated.additional_context ?? "");
      setValue(updated.additional_context ?? "");
      setJustSaved(true);
      window.setTimeout(() => setJustSaved(false), 2600);
    } catch {
      setError(true);
    }
  };

  return (
    <section className="mb-12 rounded-panel border border-border bg-surface/40 p-6 sm:p-8">
      <h3 className="font-display text-lg italic text-ink sm:text-xl">What would you like your meditations to hold?</h3>
      <p className="mt-1.5 max-w-[520px] text-sm leading-relaxed text-ink-muted">
        Anything you&rsquo;d like your meditations to hold &mdash; a season you&rsquo;re moving through, a word to
        return to, something you&rsquo;re setting down. Offered, never required.
      </p>
      <label htmlFor="core-additional-context" className="sr-only">
        Context for your meditations
      </label>
      <textarea
        id="core-additional-context"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        rows={4}
        maxLength={2000}
        placeholder="e.g. I&rsquo;m in a stretch of hard weeks and want space to breathe; keep it gentle."
        className="mt-4 w-full resize-none rounded-2xl border border-border bg-surface/60 px-4 py-3 text-sm leading-relaxed text-ink placeholder:text-ink-faint focus:border-signal/40 focus:outline-none focus:ring-1 focus:ring-signal/30"
      />
      <div className="mt-3 flex items-center justify-between gap-3">
        <span className="text-[11px] text-ink-faint" role="status" aria-live="polite">
          {error
            ? ""
            : justSaved
              ? "Saved — your assistant will keep this in mind."
              : "Only your assistant sees this."}
        </span>
        <div className="flex items-center gap-3">
          {error && (
            <span className="text-[11px] text-rose-text" role="alert">
              Couldn&rsquo;t save — try again.
            </span>
          )}
          <button
            type="button"
            onClick={() => void save()}
            disabled={!dirty || mutation.isPending}
            className="rounded-full bg-signal px-4 py-1.5 text-xs font-semibold text-[#0b0f13] transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {mutation.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </section>
  );
}
