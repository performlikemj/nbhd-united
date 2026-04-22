"use client";

import { useState } from "react";

import { useCreateSleepMutation, useSleepQuery } from "@/lib/queries";

export function Sleep() {
  const { data: entries, isLoading } = useSleepQuery();
  const createMutation = useCreateSleepMutation();
  const todayISO = new Date().toISOString().slice(0, 10);
  const [date, setDate] = useState(todayISO);
  const [hours, setHours] = useState("");
  const [quality, setQuality] = useState("");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!hours) return;
    createMutation.mutate(
      {
        date,
        duration_hours: parseFloat(hours),
        ...(quality ? { quality: parseInt(quality, 10) } : {}),
      },
      { onSuccess: () => { setHours(""); setQuality(""); } },
    );
  };

  const sorted = [...(entries || [])].sort((a, b) => a.date.localeCompare(b.date));
  const pts = sorted.map((e) => ({ value: parseFloat(e.duration_hours) }));
  const latest = sorted.at(-1);
  const avg = pts.length > 0 ? Math.round((pts.reduce((a, p) => a + p.value, 0) / pts.length) * 10) / 10 : 0;

  return (
    <div className="space-y-4">
      <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">SLEEP</div>

      <div className="rounded-panel border border-border bg-surface-elevated p-4 sm:p-5">
        <div className="flex items-start justify-between">
          <div>
            <div className="text-2xl sm:text-3xl font-semibold italic">
              {latest ? parseFloat(latest.duration_hours).toFixed(1) : "\u2014"}
              <span className="text-xs text-ink-faint ml-1">hrs</span>
            </div>
            {latest && (
              <div className="text-xs text-ink-faint font-mono mt-1">
                {new Date(latest.date + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" })}
                {latest.quality != null && <span> &middot; quality {latest.quality}/5</span>}
              </div>
            )}
          </div>
          {pts.length >= 2 && (
            <div className="font-mono text-[11px] px-2 py-1 rounded-full text-ink-faint bg-surface-hover">
              avg {avg}h
            </div>
          )}
        </div>

        {pts.length >= 2 ? (
          <SleepSparkline pts={pts} />
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
            step="0.5"
            min="0"
            max="24"
            value={hours}
            onChange={(e) => setHours(e.target.value)}
            placeholder="hrs"
            className="w-16 sm:w-20 rounded-lg border border-border bg-surface-elevated px-3 min-h-[44px] py-2 font-mono text-sm text-ink focus:outline-none focus:border-accent placeholder:text-ink-faint"
          />
          <select
            value={quality}
            onChange={(e) => setQuality(e.target.value)}
            className="rounded-lg border border-border bg-surface-elevated px-2 min-h-[44px] py-2 text-sm text-ink focus:outline-none focus:border-accent"
          >
            <option value="">Quality</option>
            <option value="1">1 - Poor</option>
            <option value="2">2</option>
            <option value="3">3 - OK</option>
            <option value="4">4</option>
            <option value="5">5 - Great</option>
          </select>
        </div>
        <button
          type="submit"
          disabled={!hours || createMutation.isPending}
          className="rounded-lg bg-accent text-white min-h-[44px] px-4 py-2 text-sm font-medium hover:opacity-90 transition disabled:opacity-50"
        >
          {createMutation.isPending ? "Logging\u2026" : "Log"}
        </button>
      </form>

      {isLoading ? (
        <div className="text-sm text-ink-faint">Loading...</div>
      ) : (entries || []).length > 0 ? (
        <div className="space-y-1">
          {(entries || []).slice(0, 14).map((e) => (
            <div key={e.id} className="flex items-center gap-2 sm:gap-3 text-xs">
              <span className="font-mono text-[10px] text-ink-faint w-20 shrink-0">
                {new Date(e.date + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}
              </span>
              <span className="font-mono text-sm text-ink">{parseFloat(e.duration_hours).toFixed(1)}h</span>
              {e.quality != null && <span className="text-ink-faint">Q{e.quality}</span>}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function SleepSparkline({ pts }: { pts: { value: number }[] }) {
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
        <linearGradient id="sleep-grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--color-blue-400, #60a5fa)" stopOpacity="0.3" />
          <stop offset="100%" stopColor="var(--color-blue-400, #60a5fa)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#sleep-grad)" />
      <path d={d} fill="none" stroke="var(--color-blue-400, #60a5fa)" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
      {coords.map((p, i) => (
        <circle key={i} cx={p.x} cy={p.y} r={i === coords.length - 1 ? 2.8 : 1.6} fill="var(--color-blue-400, #60a5fa)" />
      ))}
    </svg>
  );
}
