"use client";

import { useMemo, useRef, useState } from "react";

import {
  useBodyWeightQuery,
  useCreateBodyWeightMutation,
  useDeleteBodyWeightMutation,
} from "@/lib/queries";
import type { BodyWeightEntry } from "@/lib/types";
import { displayToKg, kgToDisplay, useWeightUnit } from "./use-weight-unit";

export function BodyWeight() {
  const { data: entries, isLoading } = useBodyWeightQuery();
  const createMutation = useCreateBodyWeightMutation();
  const deleteMutation = useDeleteBodyWeightMutation();
  const { unit, setUnit } = useWeightUnit();
  const todayISO = new Date().toISOString().slice(0, 10);
  const [date, setDate] = useState(todayISO);
  const [weight, setWeight] = useState("");
  const [pendingDelete, setPendingDelete] = useState<BodyWeightEntry | null>(null);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!weight) return;
    const kg = displayToKg(parseFloat(weight), unit);
    createMutation.mutate({ date, weight_kg: kg }, {
      onSuccess: () => setWeight(""),
    });
  };

  const sorted = [...(entries || [])].sort((a, b) => a.date.localeCompare(b.date));

  // Sparkline data — always in display unit
  const pts = sorted.map((e) => ({
    date: e.date,
    value: kgToDisplay(parseFloat(e.weight_kg), unit),
  }));
  const latest = sorted.at(-1);
  const first = sorted[0];
  const latestDisplay = latest ? kgToDisplay(parseFloat(latest.weight_kg), unit) : 0;
  const firstDisplay = first ? kgToDisplay(parseFloat(first.weight_kg), unit) : 0;
  const delta = latest && first ? +(latestDisplay - firstDisplay).toFixed(1) : 0;
  const minVal = pts.length ? Math.min(...pts.map((p) => p.value)) : 0;
  const maxVal = pts.length ? Math.max(...pts.map((p) => p.value)) : 0;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">BODY WEIGHT</div>
        <UnitToggle unit={unit} onChange={setUnit} />
      </div>

      {/* Trend card */}
      <div className="rounded-panel border border-border bg-surface-elevated p-4 sm:p-5">
        <div className="flex items-start justify-between">
          <div>
            <div className="text-3xl font-semibold italic">
              {latest ? latestDisplay.toFixed(1) : "\u2014"}
              <span className="text-xs text-ink-faint ml-1">{unit}</span>
            </div>
            {latest && (
              <div className="text-xs text-ink-faint font-mono mt-1">
                {new Date(latest.date + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" })}
              </div>
            )}
          </div>
          {pts.length >= 2 && (
            <div className={`font-mono text-[11px] px-2 py-1 rounded-full ${
              delta > 0 ? "text-rose-text bg-rose-bg" : delta < 0 ? "text-emerald-text bg-emerald-bg" : "text-ink-faint bg-surface-hover"
            }`}>
              {delta > 0 ? "\u2191" : delta < 0 ? "\u2193" : "\u00b7"} {Math.abs(delta)} {unit}
            </div>
          )}
        </div>

        {/* Sparkline */}
        {pts.length >= 2 ? (
          <>
            <WeightSparkline pts={pts} unit={unit} />
            <div className="mt-2 flex items-center justify-between font-mono text-[10px] text-ink-faint">
              <span>min {minVal.toFixed(1)} {unit}</span>
              <span>max {maxVal.toFixed(1)} {unit}</span>
            </div>
          </>
        ) : (
          <div className="mt-4 text-[11px] text-ink-faint">Log more entries to see the trend.</div>
        )}
      </div>

      {/* Quick add — stacks on mobile */}
      <form onSubmit={handleSubmit} className="flex flex-col sm:flex-row gap-2">
        <div className="flex gap-2">
          <input
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            className="flex-1 sm:flex-none rounded-lg border border-border bg-surface-elevated px-3 min-h-[44px] py-2 font-mono text-sm text-ink focus:outline-none focus:border-accent"
          />
          <input
            type="number"
            step="0.1"
            value={weight}
            onChange={(e) => setWeight(e.target.value)}
            placeholder={unit}
            className="w-20 sm:w-24 rounded-lg border border-border bg-surface-elevated px-3 min-h-[44px] py-2 font-mono text-sm text-ink focus:outline-none focus:border-accent placeholder:text-ink-faint"
          />
        </div>
        <button
          type="submit"
          disabled={!weight || createMutation.isPending}
          className="rounded-lg bg-accent text-white min-h-[44px] px-4 py-2 text-sm font-medium hover:opacity-90 transition disabled:opacity-50"
        >
          {createMutation.isPending ? "Logging\u2026" : "Log"}
        </button>
      </form>

      {/* Recent entries */}
      {isLoading ? (
        <div className="text-sm text-ink-faint">Loading...</div>
      ) : (entries || []).length > 0 ? (
        <div className="space-y-1">
          {(entries || []).slice(0, 14).map((e) => {
            const isPending = deleteMutation.isPending && deleteMutation.variables === e.id;
            return (
              <div key={e.id} className="flex items-center gap-3 text-xs">
                <span className="font-mono text-[10px] text-ink-faint w-20 shrink-0">
                  {new Date(e.date + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}
                </span>
                <span className="font-mono text-sm text-ink flex-1">
                  {kgToDisplay(parseFloat(e.weight_kg), unit).toFixed(1)} {unit}
                </span>
                <button
                  type="button"
                  onClick={() => setPendingDelete(e)}
                  disabled={isPending}
                  aria-label={`Delete weight entry from ${e.date}`}
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
        <DeleteConfirmDialog
          entry={pendingDelete}
          unit={unit}
          isPending={deleteMutation.isPending}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => {
            deleteMutation.mutate(pendingDelete.id, {
              onSuccess: () => setPendingDelete(null),
            });
          }}
        />
      )}
    </div>
  );
}

function DeleteConfirmDialog({
  entry,
  unit,
  isPending,
  onCancel,
  onConfirm,
}: {
  entry: BodyWeightEntry;
  unit: "kg" | "lbs";
  isPending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const display = kgToDisplay(parseFloat(entry.weight_kg), unit);
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
      aria-labelledby="delete-weight-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay px-4"
      onClick={onCancel}
    >
      <div
        className="rounded-panel border border-border bg-surface-elevated p-5 shadow-panel max-w-sm w-full backdrop-blur-md"
        onClick={(e) => e.stopPropagation()}
      >
        <div id="delete-weight-title" className="font-headline text-lg font-bold text-ink">Delete weight entry?</div>
        <div className="mt-2 text-sm text-ink-muted">
          {dateLabel} &middot;{" "}
          <span className="font-mono text-ink">{display.toFixed(1)} {unit}</span>
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={isPending}
            className="rounded-lg border border-border bg-transparent text-ink-muted min-h-[44px] px-4 py-2 text-sm font-medium hover:bg-surface-hover hover:text-ink transition disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={isPending}
            className="rounded-lg bg-rose-bg text-rose-text border border-rose-border min-h-[44px] px-4 py-2 text-sm font-medium hover:brightness-110 transition disabled:opacity-50"
          >
            {isPending ? "Deleting\u2026" : "Delete"}
          </button>
        </div>
      </div>
    </div>
  );
}

function UnitToggle({ unit, onChange }: { unit: "kg" | "lbs"; onChange: (u: "kg" | "lbs") => void }) {
  return (
    <div className="flex gap-0.5 rounded-full border border-border p-0.5">
      {(["kg", "lbs"] as const).map((u) => (
        <button
          key={u}
          onClick={() => onChange(u)}
          className={`rounded-full min-h-[36px] min-w-[44px] px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider transition ${
            unit === u ? "bg-surface-hover text-ink" : "text-ink-faint hover:text-ink"
          }`}
        >
          {u}
        </button>
      ))}
    </div>
  );
}

function WeightSparkline({ pts, unit }: { pts: { date: string; value: number }[]; unit: "kg" | "lbs" }) {
  const W = 280, H = 64, pad = 6;
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const { coords, d, area } = useMemo(() => {
    const vals = pts.map((p) => p.value);
    const min = Math.min(...vals), max = Math.max(...vals);
    const range = max - min || 1;
    const step = (W - pad * 2) / (pts.length - 1);
    const y = (v: number) => pad + (1 - (v - min) / range) * (H - pad * 2);
    const c = pts.map((p, i) => ({ x: pad + i * step, y: y(p.value) }));
    const path = c.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
    const a = `${path} L ${c.at(-1)!.x} ${H} L ${c[0].x} ${H} Z`;
    return { coords: c, d: path, area: a };
  }, [pts]);

  const handleMove = (clientX: number) => {
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const xViewbox = ((clientX - rect.left) / rect.width) * W;
    let nearest = 0;
    let bestDx = Infinity;
    coords.forEach((c, i) => {
      const dx = Math.abs(c.x - xViewbox);
      if (dx < bestDx) { bestDx = dx; nearest = i; }
    });
    setHoverIdx(nearest);
  };

  const active = hoverIdx != null ? coords[hoverIdx] : null;
  const activePt = hoverIdx != null ? pts[hoverIdx] : null;
  const tooltipLeftPct = active ? (active.x / W) * 100 : 0;
  const tooltipAbove = active ? active.y > H / 2 : true;

  return (
    <div className="relative mt-4">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        className="w-full h-auto touch-none"
        preserveAspectRatio="none"
        onMouseMove={(e) => handleMove(e.clientX)}
        onMouseLeave={() => setHoverIdx(null)}
        onTouchStart={(e) => handleMove(e.touches[0].clientX)}
        onTouchMove={(e) => handleMove(e.touches[0].clientX)}
        onTouchEnd={() => setHoverIdx(null)}
      >
        <defs>
          <linearGradient id="weight-grad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--color-accent)" stopOpacity="0.3" />
            <stop offset="100%" stopColor="var(--color-accent)" stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d={area} fill="url(#weight-grad)" />
        <path d={d} fill="none" stroke="var(--color-accent)" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
        {coords.map((p, i) => (
          <circle key={i} cx={p.x} cy={p.y} r={i === coords.length - 1 ? 2.8 : 1.6} fill="var(--color-accent)" />
        ))}
        {active && (
          <>
            <line x1={active.x} y1={pad} x2={active.x} y2={H - pad} stroke="var(--color-accent)" strokeWidth="0.8" strokeOpacity="0.5" strokeDasharray="2 2" />
            <circle cx={active.x} cy={active.y} r={3.5} fill="var(--color-accent)" stroke="var(--color-bg)" strokeWidth="1.5" />
          </>
        )}
      </svg>
      {active && activePt && (
        <div
          className={`pointer-events-none absolute -translate-x-1/2 rounded-md border border-border bg-surface-elevated px-2 py-1 font-mono text-[10px] text-ink shadow-panel whitespace-nowrap ${
            tooltipAbove ? "bottom-full mb-2" : "top-full mt-2"
          }`}
          style={{ left: `${tooltipLeftPct}%` }}
        >
          <span className="text-ink-faint">
            {new Date(activePt.date + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" })}
          </span>
          {" \u00b7 "}
          <span>{activePt.value.toFixed(1)} {unit}</span>
        </div>
      )}
    </div>
  );
}
