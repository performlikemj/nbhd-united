"use client";

import { useState } from "react";

import { useConfirmInsightMutation, useRefuteInsightMutation } from "@/lib/queries";
import type { HorizonsAssistantInsight } from "@/lib/types";

function formatDate(dateStr: string): string {
  const d = new Date(dateStr);
  if (Number.isNaN(d.getTime())) return dateStr;
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: new Date().getFullYear() === d.getFullYear() ? undefined : "numeric",
  });
}

function statusTone(status: HorizonsAssistantInsight["status"]): {
  badgeClass: string;
  label: string;
  accentClass: string;
} {
  switch (status) {
    case "confirmed":
      return {
        badgeClass: "bg-status-emerald text-status-emerald-text",
        label: "Confirmed",
        accentClass: "border-l-status-emerald",
      };
    case "refuted":
      return {
        badgeClass: "bg-status-rose text-status-rose-text",
        label: "Refuted",
        accentClass: "border-l-status-rose",
      };
    case "expired":
      return {
        badgeClass: "bg-status-slate text-status-slate-text",
        label: "Expired",
        accentClass: "border-l-status-slate",
      };
    case "open":
    default:
      return {
        badgeClass: "bg-status-sky text-status-sky-text",
        label: "New observation",
        accentClass: "border-l-accent",
      };
  }
}

export function InsightCard({ insight }: { insight: HorizonsAssistantInsight }) {
  const confirm = useConfirmInsightMutation();
  const refute = useRefuteInsightMutation();
  const [optimisticStatus, setOptimisticStatus] = useState<HorizonsAssistantInsight["status"] | null>(null);

  const status = optimisticStatus ?? insight.status;
  const tone = statusTone(status);
  const isResolved = status !== "open";
  const isPending = confirm.isPending || refute.isPending;

  return (
    <article
      className={`group glass-card-horizons border-l-2 ${tone.accentClass} p-5 transition-all md:p-6`}
      aria-label={`Assistant observation: ${insight.statement}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          {insight.topic_display_name ? (
            <p className="font-mono text-[10px] uppercase tracking-wider text-ink-faint">
              {insight.pillar} · {insight.topic_display_name}
            </p>
          ) : (
            <p className="font-mono text-[10px] uppercase tracking-wider text-ink-faint">{insight.pillar}</p>
          )}
          <p className="mt-1 text-sm leading-relaxed text-ink">{insight.statement}</p>
        </div>
        <span className={`inline-flex shrink-0 rounded-full px-2.5 py-1 text-[10px] font-medium ${tone.badgeClass}`}>
          {tone.label}
        </span>
      </div>

      <div className="mt-4 flex items-center justify-between gap-3">
        <span className="font-mono text-[10px] uppercase tracking-wider text-ink-faint">
          {formatDate(insight.created_at)}
        </span>
        {!isResolved ? (
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={isPending}
              onClick={() => {
                setOptimisticStatus("refuted");
                refute.mutate(
                  { id: insight.id },
                  { onError: () => setOptimisticStatus(null) },
                );
              }}
              className="min-h-[36px] rounded-full border border-border bg-transparent px-3 py-1 text-xs font-medium text-ink-muted transition hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-50"
            >
              Not quite
            </button>
            <button
              type="button"
              disabled={isPending}
              onClick={() => {
                setOptimisticStatus("confirmed");
                confirm.mutate(
                  { id: insight.id },
                  { onError: () => setOptimisticStatus(null) },
                );
              }}
              className="min-h-[36px] rounded-full bg-accent px-3 py-1 text-xs font-semibold text-white transition hover:brightness-110 active:scale-95 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Looks right
            </button>
          </div>
        ) : (
          <span className="font-mono text-[10px] uppercase tracking-wider text-ink-faint">
            {status === "confirmed" && insight.last_confirmed_at
              ? `Confirmed ${formatDate(insight.last_confirmed_at)}`
              : null}
          </span>
        )}
      </div>
    </article>
  );
}
