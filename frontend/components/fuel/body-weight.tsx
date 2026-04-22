"use client";

import { useState } from "react";

import { useBodyWeightQuery, useCreateBodyWeightMutation } from "@/lib/queries";
import { displayToKg, kgToDisplay, useWeightUnit } from "./use-weight-unit";

export function BodyWeight() {
  const { data: entries, isLoading } = useBodyWeightQuery();
  const createMutation = useCreateBodyWeightMutation();
  const { unit, setUnit } = useWeightUnit();
  const todayISO = new Date().toISOString().slice(0, 10);
  const [date, setDate] = useState(todayISO);
  const [weight, setWeight] = useState("");

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
  const pts = sorted.map((e) => ({ value: kgToDisplay(parseFloat(e.weight_kg), unit) }));
  const latest = sorted.at(-1);
  const first = sorted[0];
  const latestDisplay = latest ? kgToDisplay(parseFloat(latest.weight_kg), unit) : 0;
  const firstDisplay = first ? kgToDisplay(parseFloat(first.weight_kg), unit) : 0;
  const delta = latest && first ? +(latestDisplay - firstDisplay).toFixed(1) : 0;

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
          <WeightSparkline pts={pts} />
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
          {(entries || []).slice(0, 14).map((e) => (
            <div key={e.id} className="flex items-center gap-3 text-xs">
              <span className="font-mono text-[10px] text-ink-faint w-20 shrink-0">
                {new Date(e.date + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}
              </span>
              <span className="font-mono text-sm text-ink">{kgToDisplay(parseFloat(e.weight_kg), unit).toFixed(1)} {unit}</span>
            </div>
          ))}
        </div>
      ) : null}
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

function WeightSparkline({ pts }: { pts: { value: number }[] }) {
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
    </svg>
  );
}
