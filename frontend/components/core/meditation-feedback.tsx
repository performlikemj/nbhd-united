"use client";

import { useState } from "react";

import { submitMeditationFeedback } from "@/lib/api";
import type { MeditationSession } from "@/lib/types";

/**
 * A quiet feedback control for a rendered sit: a thumb (liked / disliked) plus
 * an optional short note. Both are best-effort — the assistant uses them to
 * shape the next meditation. Kept low-pressure, in keeping with the pillar's
 * tone: "offered, never required".
 */
export function MeditationFeedback({
  sessionId,
  initialFeedback,
  initialNote,
  onSaved,
}: {
  sessionId: string;
  initialFeedback: string;
  initialNote: string;
  onSaved?: (session: MeditationSession) => void;
}) {
  const [feedback, setFeedback] = useState(initialFeedback);
  const [note, setNote] = useState(initialNote);
  const [savedNote, setSavedNote] = useState(initialNote);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);

  const save = async (patch: { user_feedback?: string; feedback_note?: string }) => {
    setBusy(true);
    setError(false);
    try {
      const updated = await submitMeditationFeedback(sessionId, patch);
      setFeedback(updated.user_feedback);
      setSavedNote(updated.feedback_note);
      setNote(updated.feedback_note);
      onSaved?.(updated);
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  };

  // Tapping a thumb toggles it (a second tap clears the signal).
  const onThumb = (value: "liked" | "disliked") => {
    if (busy) return;
    void save({ user_feedback: feedback === value ? "" : value });
  };

  const noteDirty = note.trim() !== savedNote.trim();

  return (
    <div className="mt-8 w-full max-w-[420px]">
      <p className="text-[10px] uppercase tracking-[0.22em] text-ink-faint">How did that land?</p>
      <div className="mt-3 flex items-center justify-center gap-3">
        <button
          type="button"
          onClick={() => onThumb("liked")}
          disabled={busy}
          aria-pressed={feedback === "liked"}
          aria-label="This one landed"
          className={`grid h-11 w-11 place-items-center rounded-full border transition disabled:opacity-50 ${
            feedback === "liked"
              ? "border-signal/40 bg-signal/15 text-signal"
              : "border-border bg-surface/50 text-ink-muted hover:border-border-strong hover:text-ink"
          }`}
        >
          <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
            <path d="M7 10v10H4V10h3Zm3 0 3.5-6.5c.9.2 1.5 1 1.5 2V8h4.5a1.5 1.5 0 0 1 1.5 1.8l-1.4 7A2 2 0 0 1 20 18.5h-8.2A1.8 1.8 0 0 1 10 16.7V10Z" />
          </svg>
        </button>
        <button
          type="button"
          onClick={() => onThumb("disliked")}
          disabled={busy}
          aria-pressed={feedback === "disliked"}
          aria-label="This one didn't land"
          className={`grid h-11 w-11 place-items-center rounded-full border transition disabled:opacity-50 ${
            feedback === "disliked"
              ? "border-rose-border bg-rose-bg text-rose-text"
              : "border-border bg-surface/50 text-ink-muted hover:border-border-strong hover:text-ink"
          }`}
        >
          <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
            <path d="M17 14V4h3v10h-3Zm-3 0-3.5 6.5c-.9-.2-1.5-1-1.5-2V16H4.5A1.5 1.5 0 0 1 3 14.2l1.4-7A2 2 0 0 1 4 5.5h8.2A1.8 1.8 0 0 1 14 7.3V14Z" />
          </svg>
        </button>
      </div>

      <div className="mt-4">
        <label htmlFor={`fb-note-${sessionId}`} className="sr-only">
          A note about this meditation
        </label>
        <textarea
          id={`fb-note-${sessionId}`}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          rows={2}
          maxLength={2000}
          placeholder="Anything you'd want held differently next time? (optional)"
          className="w-full resize-none rounded-2xl border border-border bg-surface/50 px-3.5 py-2.5 text-sm text-ink placeholder:text-ink-faint focus:border-signal/40 focus:outline-none focus:ring-1 focus:ring-signal/30"
        />
        {noteDirty && (
          <div className="mt-2 flex justify-end">
            <button
              type="button"
              onClick={() => void save({ feedback_note: note.trim() })}
              disabled={busy}
              className="rounded-full bg-signal px-3.5 py-1.5 text-xs font-semibold text-[#0b0f13] transition hover:brightness-110 disabled:opacity-50"
            >
              {busy ? "Saving…" : "Save note"}
            </button>
          </div>
        )}
      </div>

      {error ? (
        <p className="mt-2 text-center text-[11px] text-rose-text" role="alert">
          Couldn&rsquo;t save that just now — please try again.
        </p>
      ) : feedback || savedNote ? (
        <p className="mt-2 text-center text-[11px] text-ink-faint" role="status">
          Thank you — this helps shape your next sit.
        </p>
      ) : null}
    </div>
  );
}
