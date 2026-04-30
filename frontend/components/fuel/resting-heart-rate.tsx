"use client";

import { useState } from "react";

import { useCreateRestingHRMutation, useRestingHRQuery } from "@/lib/queries";

export function RestingHeartRate() {
  const { data: entries, isLoading } = useRestingHRQuery();
  const createMutation = useCreateRestingHRMutation();
  const todayISO = new Date().toISOString().slice(0, 10);
  const [date, setDate] = useState(todayISO);
  const [bpm, setBpm] = useState("");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!bpm) return;
    createMutation.mutate({ date, bpm: parseInt(bpm, 10) }, {
      onSuccess: () => setBpm(""),
    });
  };

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
              {latest ? latest.bpm : "\u2014"}
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
              {delta < 0 ? "\u2193" : delta > 0 ? "\u2191" : "\u00b7"} {Math.abs(delta)} bpm
            </div>
          )}
        </div>

        {pts.length >= 2 ? (
          <RHRSparkline pts={pts} />
        ) : (
          <div className="mt-4 text-xs text-ink-faint">Log more entries to see the trend.</div>
        )}
      </div>

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
            value={bpm}
            onChange={(e) => setBpm(e.target.value)}
            placeholder="bpm"
            className="w-20 sm:w-24 rounded-lg border border-border bg-surface-elevated px-3 min-h-[44px] py-2 font-mono text-sm text-ink focus:outline-none focus:border-accent placeholder:text-ink-faint"
          />
        </div>
        <button
          type="submit"
          disabled={!bpm || createMutation.isPending}
          className="glow-purple rounded-full bg-accent text-white min-h-[44px] px-4 py-2 text-sm font-semibold hover:brightness-110 active:scale-[0.98] transition disabled:opacity-50"
        >
          {createMutation.isPending ? "Logging\u2026" : "Log"}
        </button>
      </form>

      {isLoading ? (
        <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-ink-faint">loading…</div>
      ) : (entries || []).length > 0 ? (
        <div className="space-y-1">
          {(entries || []).slice(0, 14).map((e) => (
            <div key={e.id} className="flex items-center gap-3 text-xs">
              <span className="font-mono text-[10px] text-ink-faint w-20 shrink-0">
                {new Date(e.date + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}
              </span>
              <span className="font-mono text-sm text-ink">{e.bpm} bpm</span>
            </div>
          ))}
        </div>
      ) : null}
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
