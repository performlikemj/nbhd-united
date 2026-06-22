"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { Lesson } from "@/lib/types";
import { useApproveLessonMutation, useDismissLessonMutation, usePendingLessonsQuery } from "@/lib/queries";

function formatDate(dateString: string): string {
  const date = new Date(dateString);
  if (Number.isNaN(date.getTime())) return dateString;
  return date.toLocaleString();
}

export default function PendingLessonQueuePage() {
  const { data: items = [], isLoading, error: queryError } = usePendingLessonsQuery();
  const approveMutation = useApproveLessonMutation();
  const dismissMutation = useDismissLessonMutation();
  const [error, setError] = useState("");
  const [processing, setProcessing] = useState<number | null>(null);
  const [removingIds, setRemovingIds] = useState<number[]>([]);

  const loading = isLoading;
  const totalCount = items.length;
  const liveError = error || (queryError instanceof Error ? queryError.message : queryError ? "Failed to load pending lessons." : "");

  const sortedItems = useMemo(
    () => [...items].sort((a, b) => new Date(b.suggested_at).getTime() - new Date(a.suggested_at).getTime()),
    [items],
  );

  // 220ms removal animation: tag the id so the card fades out, then let
  // the mutation's onSuccess invalidate ["pending-lessons"] which refetches
  // the list without the approved/dismissed entry.
  const animateRemoval = (id: number) => {
    setRemovingIds((current) => [...current, id]);
    window.setTimeout(() => {
      setRemovingIds((current) => current.filter((itemId) => itemId !== id));
    }, 220);
  };

  const handleApprove = async (lesson: Lesson) => {
    setError("");
    try {
      setProcessing(lesson.id);
      await approveMutation.mutateAsync(lesson.id);
      animateRemoval(lesson.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to approve lesson.");
    } finally {
      setProcessing(null);
    }
  };

  const handleDismiss = async (lesson: Lesson) => {
    setError("");
    try {
      setProcessing(lesson.id);
      await dismissMutation.mutateAsync(lesson.id);
      animateRemoval(lesson.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to dismiss lesson.");
    } finally {
      setProcessing(null);
    }
  };

  if (loading) {
    return <div className="rounded-panel border border-border bg-surface p-4 text-ink-muted">Loading pending lessons...</div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">Pending Lesson Queue</h1>
          <p className="text-sm text-ink-muted">Approve and curate suggestions before they enter the constellation.</p>
        </div>
        <Link
          href="/constellation"
          className="rounded-full border border-border px-3.5 py-2 text-sm text-ink-muted transition hover:border-border-strong hover:text-ink"
        >
          ← Back to constellation
        </Link>
      </div>

      {liveError ? <div className="rounded-panel border border-rose-border bg-rose-bg px-3 py-2 text-sm text-rose-text">{liveError}</div> : null}

      {totalCount === 0 ? (
        <div className="rounded-panel border border-border bg-surface p-4 text-sm text-ink-muted">No pending lessons right now.</div>
      ) : (
        <div className="space-y-3">
          {sortedItems.map((lesson) => (
            <article
              key={lesson.id}
              className={`w-full rounded-panel border border-border bg-surface p-4 transition-all duration-200 ${
                removingIds.includes(lesson.id) ? "scale-[0.99] opacity-0" : "opacity-100"
              }`}
            >
              <h2 className="text-sm font-semibold text-ink">{lesson.text}</h2>
              <p className="mt-1 text-sm text-ink-muted">{lesson.context || "No context provided."}</p>
              <p className="mt-2 text-xs text-ink-faint">Suggested: {formatDate(lesson.suggested_at)}</p>
              {lesson.tags.length ? (
                <div className="mt-2 flex flex-wrap gap-1">
                  {lesson.tags.map((tag) => (
                    <span
                      key={`${lesson.id}-${tag}`}
                      className="rounded-full border border-border bg-surface px-2.5 py-1 text-xs text-ink-muted"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              ) : null}

              <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:justify-end">
                <button
                  type="button"
                  disabled={processing === lesson.id}
                  onClick={() => handleApprove(lesson)}
                  className="h-11 rounded-full bg-accent px-4 py-2 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55"
                >
                  {processing === lesson.id ? "Working..." : "Approve"}
                </button>
                <button
                  type="button"
                  disabled={processing === lesson.id}
                  onClick={() => handleDismiss(lesson)}
                  className="h-11 rounded-full border border-rose-border bg-rose-bg px-4 py-2 text-sm text-rose-text transition hover:bg-rose-bg/80 disabled:opacity-55"
                >
                  {processing === lesson.id ? "Working..." : "Dismiss"}
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
