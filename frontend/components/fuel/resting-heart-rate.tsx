"use client";

import { useState } from "react";

import {
  useDeleteRestingHRMutation,
  useRestingHRQuery,
  useUpdateRestingHRMutation,
} from "@/lib/queries";
import type { RestingHeartRateEntry } from "@/lib/types";
import { SkelBar } from "@/components/ui/skeleton";

export function RestingHeartRate() {
  const { data: entries, isPending } = useRestingHRQuery();
  const updateMutation = useUpdateRestingHRMutation();
  const deleteMutation = useDeleteRestingHRMutation();
  const [pendingEdit, setPendingEdit] = useState<RestingHeartRateEntry | null>(null);
  const [pendingDelete, setPendingDelete] = useState<RestingHeartRateEntry | null>(null);

  const sorted = [...(entries || [])].sort((a, b) => a.date.localeCompare(b.date));
  const pts = sorted.map((e) => ({ value: e.bpm }));
  const latest = sorted.at(-1);
  const first = sorted[0];
  const delta = latest && first ? latest.bpm - first.bpm : 0;

  return (
    <div className="space-y-4">
      <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">RESTING HEART RATE</div>

      <div className="rounded-panel border border-border bg-surface-elevated p-4 sm:p-5">
        <div className="flex items-start justify-between">
          <div>
            <div className="text-2xl sm:text-3xl font-semibold italic">
              {latest ? latest.bpm : "—"}
              <span className="text-xs text-ink-faint ml-1">bpm</span>
            </div>
            {latest && (
              <div className="text-xs text-ink-faint font-mono mt-1">
                {new Date(latest.date + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" })}
              </div>
            )}
          </div>
          {pts.length >= 2 && (
            <div className={`font-mono text-[11px] px-2 py-1 rounded-full ${
              delta < 0 ? "text-emerald-text bg-emerald-bg" : delta > 0 ? "text-rose-text bg-rose-bg" : "text-ink-faint bg-surface-hover"
            }`}>
              {delta < 0 ? "↓" : delta > 0 ? "↑" : "·"} {Math.abs(delta)} bpm
            </div>
          )}
        </div>

        {pts.length >= 2 ? (
          <RHRSparkline pts={pts} />
        ) : (
          <div className="mt-4 text-xs text-ink-faint">Log more entries to see the trend.</div>
        )}
      </div>

      {isPending ? (
        <div className="space-y-1" role="status" aria-busy="true" aria-label="Loading resting heart rate history">
          {[0, 1, 2, 3, 4].map((i) => (
            <div key={i} className="flex items-center gap-3">
              <SkelBar className="h-3 w-20 shrink-0" />
              <SkelBar className="h-4 w-16 flex-1" />
              <SkelBar className="h-9 w-9" />
              <SkelBar className="h-9 w-9" />
            </div>
          ))}
        </div>
      ) : (entries || []).length > 0 ? (
        <div className="space-y-1">
          {(entries || []).slice(0, 14).map((e) => {
            const isDeletePending = deleteMutation.isPending && deleteMutation.variables === e.id;
            return (
              <div key={e.id} className="flex items-center gap-3 text-xs">
                <span className="font-mono text-[10px] text-ink-faint w-20 shrink-0">
                  {new Date(e.date + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}
                </span>
                <span className="font-mono text-sm text-ink flex-1">{e.bpm} bpm</span>
                <button
                  type="button"
                  onClick={() => setPendingEdit(e)}
                  aria-label={`Edit heart rate entry from ${e.date}`}
                  className="rounded-md min-h-[36px] min-w-[36px] px-2 text-ink-faint hover:text-ink hover:bg-surface-hover transition"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <path d="M12 20h9" />
                    <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4Z" />
                  </svg>
                </button>
                <button
                  type="button"
                  onClick={() => setPendingDelete(e)}
                  disabled={isDeletePending}
                  aria-label={`Delete heart rate entry from ${e.date}`}
                  className="rounded-md min-h-[36px] min-w-[36px] px-2 text-ink-faint hover:text-rose-text hover:bg-rose-bg/40 transition disabled:opacity-50"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <path d="M3 6h18" />
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
                    <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                  </svg>
                </button>
              </div>
            );
          })}
        </div>
      ) : null}

      {pendingDelete && (
        <RHRDeleteDialog
          entry={pendingDelete}
          isPending={deleteMutation.isPending}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => {
            deleteMutation.mutate(pendingDelete.id, {
              onSuccess: () => setPendingDelete(null),
            });
          }}
        />
      )}

      {pendingEdit && (
        <RHREditDialog
          entry={pendingEdit}
          isPending={updateMutation.isPending}
          error={updateMutation.error instanceof Error ? updateMutation.error.message : null}
          onCancel={() => {
            updateMutation.reset();
            setPendingEdit(null);
          }}
          onSave={(patch) => {
            updateMutation.mutate(
              { id: pendingEdit.id, data: patch },
              {
                onSuccess: () => {
                  updateMutation.reset();
                  setPendingEdit(null);
                },
              },
            );
          }}
        />
      )}
    </div>
  );
}

function RHREditDialog({
  entry,
  isPending,
  error,
  onCancel,
  onSave,
}: {
  entry: RestingHeartRateEntry;
  isPending: boolean;
  error: string | null;
  onCancel: () => void;
  onSave: (patch: { date?: string; bpm?: number }) => void;
}) {
  const [date, setDate] = useState(entry.date);
  const [bpm, setBpm] = useState(String(entry.bpm));

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const patch: { date?: string; bpm?: number } = {};
    if (date !== entry.date) patch.date = date;
    const parsed = parseInt(bpm, 10);
    if (Number.isFinite(parsed) && parsed !== entry.bpm) {
      patch.bpm = parsed;
    }
    if (Object.keys(patch).length === 0) {
      onCancel();
      return;
    }
    onSave(patch);
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="edit-hr-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay px-4"
      onClick={onCancel}
    >
      <form
        onSubmit={handleSubmit}
        className="rounded-panel border border-border bg-surface-elevated p-5 shadow-panel max-w-sm w-full backdrop-blur-md"
        onClick={(e) => e.stopPropagation()}
      >
        <div id="edit-hr-title" className="font-headline text-lg font-bold text-ink">Edit heart rate entry</div>
        <div className="mt-4 space-y-3">
          <label className="block">
            <span className="block font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint mb-1">Date</span>
            <input
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              className="w-full rounded-lg border border-border bg-surface px-3 min-h-[44px] py-2 font-mono text-sm text-ink focus:outline-none focus:border-accent"
            />
          </label>
          <label className="block">
            <span className="block font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint mb-1">Resting HR (bpm)</span>
            <input
              type="text"
              inputMode="numeric"
              value={bpm}
              onChange={(e) => {
                if (/^\d*$/.test(e.target.value)) setBpm(e.target.value);
              }}
              className="w-full rounded-lg border border-border bg-surface px-3 min-h-[44px] py-2 font-mono text-sm text-ink focus:outline-none focus:border-accent"
            />
          </label>
        </div>

        {error && (
          <div className="mt-3 rounded-xl border border-rose-border bg-rose-bg px-4 py-2.5 text-sm text-rose-text">
            {error.includes("409") || error.toLowerCase().includes("already")
              ? `An entry already exists for ${date}. Delete that one first, or pick a different date.`
              : error}
          </div>
        )}

        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-border bg-transparent text-ink-muted min-h-[44px] px-4 py-2 text-sm font-medium hover:bg-surface-hover hover:text-ink transition"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={isPending || !bpm}
            className="glow-purple rounded-lg bg-accent text-white border border-accent min-h-[44px] px-4 py-2 text-sm font-semibold hover:brightness-110 active:scale-[0.98] transition disabled:opacity-50"
          >
            {isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </div>
  );
}

function RHRDeleteDialog({
  entry,
  isPending,
  onCancel,
  onConfirm,
}: {
  entry: RestingHeartRateEntry;
  isPending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const dateLabel = new Date(entry.date + "T00:00:00").toLocaleDateString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="delete-hr-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay px-4"
      onClick={onCancel}
    >
      <div
        className="rounded-panel border border-border bg-surface-elevated p-5 shadow-panel max-w-sm w-full backdrop-blur-md"
        onClick={(e) => e.stopPropagation()}
      >
        <div id="delete-hr-title" className="font-headline text-lg font-bold text-ink">Delete heart rate entry?</div>
        <div className="mt-2 text-sm text-ink-muted">
          {dateLabel} &middot;{" "}
          <span className="font-mono text-ink">{entry.bpm} bpm</span>
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-border bg-transparent text-ink-muted min-h-[44px] px-4 py-2 text-sm font-medium hover:bg-surface-hover hover:text-ink transition"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={isPending}
            className="rounded-lg bg-rose-bg text-rose-text border border-rose-border min-h-[44px] px-4 py-2 text-sm font-medium hover:brightness-110 transition disabled:opacity-50"
          >
            {isPending ? "Deleting…" : "Delete"}
          </button>
        </div>
      </div>
    </div>
  );
}

function RHRSparkline({ pts }: { pts: { value: number }[] }) {
  const W = 280, H = 64, pad = 6;
  const vals = pts.map((p) => p.value);
  const min = Math.min(...vals), max = Math.max(...vals);
  const range = max - min || 1;
  const step = (W - pad * 2) / (pts.length - 1);
  const y = (v: number) => pad + (1 - (v - min) / range) * (H - pad * 2);
  const coords = pts.map((p, i) => ({ x: pad + i * step, y: y(p.value) }));
  const d = coords.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
  const area = `${d} L ${coords.at(-1)!.x} ${H} L ${coords[0].x} ${H} Z`;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="mt-4 w-full h-auto" preserveAspectRatio="none">
      <defs>
        <linearGradient id="rhr-grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--color-rose-text, #f472b6)" stopOpacity="0.3" />
          <stop offset="100%" stopColor="var(--color-rose-text, #f472b6)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#rhr-grad)" />
      <path d={d} fill="none" stroke="var(--color-rose-text, #f472b6)" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
      {coords.map((p, i) => (
        <circle key={i} cx={p.x} cy={p.y} r={i === coords.length - 1 ? 2.8 : 1.6} fill="var(--color-rose-text, #f472b6)" />
      ))}
    </svg>
  );
}
