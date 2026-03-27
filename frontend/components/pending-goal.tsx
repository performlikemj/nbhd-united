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
      <article className="glass-card-horizons border-l-2 border-l-signal p-5 md:p-6">
        <p className="text-sm text-signal">
          {extraction.kind === "goal" ? "Added to your goals!" : "Added to your tasks!"}
        </p>
      </article>
    );
  }

  const busy = approveMutation.isPending || dismissMutation.isPending;
  const isGoal = extraction.kind === "goal";
  const borderColor = isGoal ? "border-l-accent" : "border-l-signal";
  const badgeLabel = isGoal ? "AI Extraction" : "Pattern Found";
  const badgeClasses = isGoal
    ? "text-accent bg-accent/10"
    : "text-signal bg-signal/10";

  return (
    <article className={`glass-card-horizons border-l-4 ${borderColor} p-5 flex flex-col justify-between md:p-6`}>
      <div>
        <div className="mb-3 flex items-start justify-between">
          <span className={`rounded px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-widest ${badgeClasses}`}>
            {badgeLabel}
          </span>
        </div>

        <p className="text-sm font-medium leading-snug text-ink mb-3">
          &ldquo;{extraction.text}&rdquo;
        </p>

        <div className="flex flex-wrap items-center gap-2 text-xs text-ink-faint">
          <span>{extraction.confidence} confidence</span>
          {extraction.source_date ? (
            <span className="font-mono">from {formatDate(extraction.source_date)}</span>
          ) : null}
        </div>
      </div>

      <div className="mt-4 flex items-center gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            approveMutation.mutate(extraction.id, {
              onSuccess: () => setResolved("approved"),
            });
          }}
          className={`w-full rounded-lg py-2 text-xs font-mono uppercase tracking-widest transition-all disabled:opacity-50 ${
            isGoal
              ? "bg-accent/10 text-accent hover:bg-accent hover:text-white"
              : "bg-signal/10 text-signal hover:bg-signal hover:text-[#0b0f13]"
          }`}
        >
          {approveMutation.isPending ? "Saving..." : `Accept ${extraction.kind === "goal" ? "Goal" : "Task"}`}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            dismissMutation.mutate(extraction.id, {
              onSuccess: () => setResolved("dismissed"),
            });
          }}
          className="rounded-lg border border-border px-4 py-2 text-xs font-mono uppercase tracking-widest text-ink-muted transition-colors hover:border-border-strong hover:text-ink disabled:opacity-50"
        >
          Dismiss
        </button>
      </div>
    </article>
  );
}
