"use client";

import { useState } from "react";

import { HorizonsPendingExtraction } from "@/lib/types";
import { useApproveExtractionMutation, useDismissExtractionMutation } from "@/lib/queries";

function formatDate(dateStr: string): string {
  const d = new Date(dateStr + "T00:00:00");
  if (Number.isNaN(d.getTime())) return dateStr;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function PendingGoal({
  extraction,
}: {
  extraction: HorizonsPendingExtraction;
}) {
  const approveMutation = useApproveExtractionMutation();
  const dismissMutation = useDismissExtractionMutation();
  const [resolved, setResolved] = useState<"approved" | "dismissed" | null>(null);

  if (resolved === "dismissed") return null;

  if (resolved === "approved") {
    return (
      <article className="rounded-panel border border-emerald-200 bg-emerald-50 p-4 dark:border-emerald-800/30 dark:bg-emerald-900/10 md:p-5">
        <p className="text-sm text-emerald-700 dark:text-emerald-300">
          {extraction.kind === "goal" ? "Added to your goals!" : "Added to your tasks!"}
        </p>
      </article>
    );
  }

  const busy = approveMutation.isPending || dismissMutation.isPending;

  return (
    <article className="rounded-panel border border-dashed border-border bg-surface/60 p-4 md:p-5">
      <p className="mb-2 text-xs italic text-ink-faint">
        Your assistant noticed&hellip;
      </p>

      <p className="text-sm leading-relaxed text-ink">{extraction.text}</p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span
          className={`rounded-full px-2 py-0.5 font-mono text-[11px] ${
            extraction.kind === "goal"
              ? "bg-sky-50 text-sky-800 dark:bg-sky-900/20 dark:text-sky-300"
              : "bg-slate-100 text-slate-600 dark:bg-slate-800/30 dark:text-slate-400"
          }`}
        >
          {extraction.kind}
        </span>

        <span className="text-xs text-ink-faint">
          {extraction.confidence} confidence
        </span>

        {extraction.source_date ? (
          <span className="font-mono text-xs text-ink-faint">
            from {formatDate(extraction.source_date)}
          </span>
        ) : null}
      </div>

      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            approveMutation.mutate(extraction.id, {
              onSuccess: () => setResolved("approved"),
            });
          }}
          className="rounded-full bg-accent px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-50"
        >
          {approveMutation.isPending ? "Saving..." : "Approve"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            dismissMutation.mutate(extraction.id, {
              onSuccess: () => setResolved("dismissed"),
            });
          }}
          className="rounded-full border border-border px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-border-strong hover:text-ink disabled:opacity-50"
        >
          Dismiss
        </button>
      </div>
    </article>
  );
}
