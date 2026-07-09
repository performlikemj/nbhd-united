"use client";

import { SectionCard } from "@/components/section-card";
import type { PIIReviewQueueEntry } from "@/lib/api";

interface PIIReviewCardProps {
  entries: PIIReviewQueueEntry[];
  /** Full unreviewed backlog (may exceed entries.length when server-capped). */
  total: number;
  onKeep: (placeholder: string) => void;
  onStopHiding: (placeholder: string) => void;
  onKeepAll: () => void;
  onStopHidingAll: () => void;
  /** Placeholders with a keep request in flight (per-row spinner). */
  pendingKeep: Set<string>;
  /** Placeholders with a stop-hiding request in flight. */
  pendingStop: Set<string>;
  /** Any bulk mutation in flight — disables the header actions. */
  bulkBusy: boolean;
}

/**
 * Tier-2 review surface: "here is what your assistant is hiding; keep or clean."
 *
 * The PII detector mints placeholders aggressively, so most bindings on a busy
 * tenant are junk (markdown fragments, tool-response noise, mislabeled numbers).
 * This card lists the PERSON/LOCATION spans the user hasn't judged yet and lets
 * them keep the real ones (stamps reviewed_at) or stop hiding the junk (deletes
 * the binding AND denylists the value so it isn't re-minted). It only renders
 * when the queue is non-empty — a clean queue shows nothing.
 */
export function PIIReviewCard({
  entries,
  total,
  onKeep,
  onStopHiding,
  onKeepAll,
  onStopHidingAll,
  pendingKeep,
  pendingStop,
  bulkBusy,
}: PIIReviewCardProps) {
  if (entries.length === 0) return null;

  const capped = total > entries.length;
  const title = `Your assistant is hiding ${total} ${total === 1 ? "value" : "values"}`;

  return (
    <SectionCard
      title={title}
      subtitle="These are names and places your assistant swapped for placeholders but you haven’t reviewed. Keep the real ones so it can still tell them apart; stop hiding the ones that aren’t actually personal so they stop being masked."
    >
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-ink-faint">
          {capped
            ? `Showing the ${entries.length} newest of ${total}.`
            : `${entries.length} to review.`}
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={onStopHidingAll}
            disabled={bulkBusy}
            className="rounded-lg border border-rose-border bg-transparent px-4 py-2 text-xs font-medium text-rose-text transition hover:bg-rose-bg disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
          >
            {capped ? `Stop hiding these ${entries.length}` : "Stop hiding all"}
          </button>
          <button
            type="button"
            onClick={onKeepAll}
            disabled={bulkBusy}
            className="glow-purple rounded-lg bg-accent px-4 py-2 text-xs font-semibold text-white transition hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
          >
            {capped ? `Keep these ${entries.length}` : "Keep all"}
          </button>
        </div>
      </div>

      <ul className="space-y-2">
        {entries.map((entry) => {
          const keeping = pendingKeep.has(entry.placeholder);
          const stopping = pendingStop.has(entry.placeholder);
          const rowBusy = keeping || stopping;
          const label = entry.name?.trim() || entry.placeholder;
          const detail = [entry.relationship?.trim(), entry.notes?.trim()]
            .filter(Boolean)
            .join(" · ");

          return (
            <li
              key={entry.placeholder}
              className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-surface/60 px-4 py-3 backdrop-blur-sm"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm text-ink">{label}</p>
                <div className="mt-0.5 flex flex-wrap items-center gap-2">
                  <code className="font-mono text-[10px] uppercase tracking-[0.12em] text-ink-faint">
                    {entry.placeholder}
                  </code>
                  {detail && <span className="truncate text-xs text-ink-muted">{detail}</span>}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <button
                  type="button"
                  onClick={() => onStopHiding(entry.placeholder)}
                  disabled={rowBusy || bulkBusy}
                  title="This isn’t personal — remove the binding and stop masking this value in future messages."
                  className="rounded-lg border border-rose-border bg-transparent px-3 py-2 text-xs font-medium text-rose-text transition hover:bg-rose-bg disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
                >
                  {stopping ? "Stopping…" : "Stop hiding"}
                </button>
                <button
                  type="button"
                  onClick={() => onKeep(entry.placeholder)}
                  disabled={rowBusy || bulkBusy}
                  title="This is a real person or place — keep masking it and mark it reviewed."
                  className="rounded-lg border border-border bg-transparent px-3 py-2 text-xs text-ink-muted transition hover:bg-surface-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
                >
                  {keeping ? "Keeping…" : "Keep"}
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </SectionCard>
  );
}
