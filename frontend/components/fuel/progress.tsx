"use client";

import { useMemo, useRef, useState } from "react";

import { useFuelProgressQuery } from "@/lib/queries";
import type { WorkoutCategory } from "@/lib/types";
import { CATEGORIES, CATEGORY_IDS } from "./category-meta";
import { kgToDisplay, useWeightUnit } from "./use-weight-unit";

export function Progress() {
  const [cat, setCat] = useState<WorkoutCategory>("strength");
  const { data, isLoading } = useFuelProgressQuery(cat);

  const progress = data?.progress as Record<string, unknown> | undefined;

  return (
    <div className="space-y-5">
      {/* Category chips — wrap on mobile (all 7 always visible), inline row on sm+ */}
      <div className="flex flex-col sm:flex-row sm:items-center gap-2">
        <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-ink-faint sm:mr-1">CATEGORY</span>
        <div className="flex flex-wrap gap-2">
          {CATEGORY_IDS.map((c) => {
            const on = cat === c;
            return (
              <button
                key={c}
                onClick={() => setCat(c)}
                className={`rounded-full min-h-[44px] px-3 py-2 text-[11px] font-bold uppercase tracking-wider transition border flex items-center gap-1.5 whitespace-nowrap ${
                  on ? "text-ink" : "text-ink-muted"
                }`}
                style={on ? { background: `color-mix(in srgb, ${CATEGORIES[c].accent} 20%, transparent)`, borderColor: CATEGORIES[c].accent } : { borderColor: "var(--color-border)" }}
              >
                {CATEGORIES[c].label}
              </button>
            );
          })}
        </div>
      </div>

      {isLoading && <div className="text-sm text-ink-faint">Loading progress...</div>}

      {!isLoading && progress && cat === "strength" && <StrengthProgress data={progress} />}
      {!isLoading && progress && cat === "cardio" && <CardioProgress data={progress} />}
      {!isLoading && progress && cat === "hiit" && <HiitProgress data={progress} />}
      {!isLoading && progress && cat === "calisthenics" && <CalisProgress data={progress} />}
      {!isLoading && progress && (cat === "mobility" || cat === "sport" || cat === "other") && (
        <CountProgress data={progress} accent={CATEGORIES[cat].accent} />
      )}
    </div>
  );
}

/* ---- Sparkline SVG ---- */
type SparkPt = { date?: string; value: number };

function Sparkline({
  pts,
  color,
  invert,
  format,
}: {
  pts: SparkPt[];
  color: string;
  invert?: boolean;
  format?: (v: number) => string;
}) {
  const W = 280, H = 64, pad = 6;
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const geometry = useMemo(() => {
    if (!pts || pts.length < 2) return null;
    const vals = pts.map((p) => p.value);
    const min = Math.min(...vals), max = Math.max(...vals);
    const range = max - min || 1;
    const step = (W - pad * 2) / (pts.length - 1);
    const y = (v: number) => {
      const n = (v - min) / range;
      return pad + (invert ? n : 1 - n) * (H - pad * 2);
    };
    const coords = pts.map((p, i) => ({ x: pad + i * step, y: y(p.value) }));
    const d = coords.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
    const area = `${d} L ${coords.at(-1)!.x} ${H} L ${coords[0].x} ${H} Z`;
    return { coords, d, area, min, max };
  }, [pts, invert]);

  if (!geometry) {
    return <div className="mt-4 text-xs text-ink-faint">Log more sessions to see the trend.</div>;
  }

  const { coords, d, area, min, max } = geometry;
  const gid = `lg-${color.replace(/[^a-zA-Z0-9]/g, "")}-${pts.length}`;
  const fmt = format ?? ((v: number) => v.toFixed(1));

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
    <div className="mt-4">
      <div className="relative">
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
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity="0.3" />
              <stop offset="100%" stopColor={color} stopOpacity="0" />
            </linearGradient>
          </defs>
          <path d={area} fill={`url(#${gid})`} />
          <path d={d} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
          {coords.map((p, i) => (
            <circle key={i} cx={p.x} cy={p.y} r={i === coords.length - 1 ? 2.8 : 1.6} fill={color} />
          ))}
          {active && (
            <>
              <line x1={active.x} y1={pad} x2={active.x} y2={H - pad} stroke={color} strokeWidth="0.8" strokeOpacity="0.5" strokeDasharray="2 2" />
              <circle cx={active.x} cy={active.y} r={3.5} fill={color} stroke="var(--color-bg)" strokeWidth="1.5" />
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
            {activePt.date && (
              <span className="text-ink-faint">
                {new Date(activePt.date + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" })}
                {" \u00b7 "}
              </span>
            )}
            <span>{fmt(activePt.value)}</span>
          </div>
        )}
      </div>
      <div className="mt-1.5 flex items-center justify-between font-mono text-[10px] text-ink-faint">
        <span>min {fmt(min)}</span>
        <span>max {fmt(max)}</span>
      </div>
    </div>
  );
}

/* ---- Strength Progress ---- */
function StrengthProgress({ data }: { data: Record<string, unknown> }) {
  const lifts = Object.entries(data as Record<string, { date: string; value: number }[]>).sort(
    (a, b) => b[1].length - a[1].length,
  );
  const accent = CATEGORIES.strength.accent;
  const { unit } = useWeightUnit();
  const dw = (kg: number) => kgToDisplay(kg, unit);

  if (lifts.length === 0) {
    return <div className="rounded-panel border border-border p-4 sm:p-8 text-center text-sm text-ink-faint">No strength sessions logged yet.</div>;
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 sm:gap-4">
      {lifts.map(([lift, pts]) => {
        const displayPts = pts.map((p) => ({ ...p, value: dw(p.value) }));
        const latest = displayPts.at(-1)!.value;
        const first = displayPts[0].value;
        const delta = +(latest - first).toFixed(1);
        const hasTrend = displayPts.length >= 2;
        return (
          <div key={lift} className="rounded-panel border border-border bg-surface-elevated p-4 sm:p-5">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint truncate">{lift.toUpperCase()}</div>
                <div className="mt-1.5 flex items-baseline gap-2">
                  <span className="text-2xl sm:text-3xl font-semibold italic" style={{ color: accent }}>{latest}</span>
                  <span className="text-[11px] text-ink-faint">{unit} &middot; est 1RM</span>
                </div>
              </div>
              {hasTrend ? (
                <div className={`shrink-0 text-right font-mono text-[11px] px-2 py-1 rounded-full ${
                  delta > 0 ? "text-emerald-text bg-emerald-bg" : delta < 0 ? "text-rose-text bg-rose-bg" : "text-ink-faint bg-surface-hover"
                }`}>
                  {delta > 0 ? "\u2191" : delta < 0 ? "\u2193" : "\u00b7"} {Math.abs(delta)} {unit}
                </div>
              ) : (
                <div className="shrink-0 text-[9px] font-bold uppercase tracking-wider text-ink-faint px-2 py-1 rounded-full bg-surface-hover">1 SESSION</div>
              )}
            </div>
            {hasTrend ? (
              <Sparkline pts={displayPts} color={accent} format={(v) => `${v.toFixed(1)} ${unit}`} />
            ) : (
              <div className="mt-4 rounded-lg border border-dashed border-border px-3 py-4 text-center text-xs text-ink-faint">
                Log another session to see the trend
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ---- Cardio Progress ---- */
function CardioProgress({ data }: { data: Record<string, unknown> }) {
  const accent = CATEGORIES.cardio.accent;
  const pace = (data.pace as { date: string; value: number }[]) || [];
  const dist = (data.distance as { date: string; value: number }[]) || [];
  const totalKm = data.total_km as number || 0;

  const fmtPace = (s: number) => {
    const m = Math.floor(s / 60);
    const ss = Math.round(s - m * 60);
    return `${m}:${String(ss).padStart(2, "0")}`;
  };

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 sm:gap-4">
      <div className="rounded-panel border border-border bg-surface-elevated p-4 sm:p-5">
        <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">PACE TREND</div>
        <div className="mt-1.5 text-2xl sm:text-3xl font-semibold italic" style={{ color: accent }}>
          {pace.length > 0 ? fmtPace(Math.min(...pace.map((p) => p.value))) : "\u2014"}
          <span className="text-xs text-ink-faint ml-1">/km best</span>
        </div>
        <Sparkline pts={pace} color={accent} invert format={(v) => `${fmtPace(v)} /km`} />
      </div>
      <div className="rounded-panel border border-border bg-surface-elevated p-4 sm:p-5">
        <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">DISTANCE PER SESSION</div>
        <div className="mt-1.5 text-2xl sm:text-3xl font-semibold italic" style={{ color: accent }}>
          {totalKm.toFixed(1)}<span className="text-xs text-ink-faint ml-1">km total</span>
        </div>
        <Sparkline pts={dist} color={accent} format={(v) => `${v.toFixed(1)} km`} />
      </div>
    </div>
  );
}

/* ---- HIIT Progress ---- */
function HiitProgress({ data }: { data: Record<string, unknown> }) {
  const accent = CATEGORIES.hiit.accent;
  const hrPts = (data.peak_hr as { date: string; value: number }[]) || [];
  const count = (data.session_count as number) || 0;
  const totalMin = (data.total_minutes as number) || 0;

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 sm:gap-4">
      <div className="rounded-panel border border-border bg-surface-elevated p-4 sm:p-5">
        <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">PEAK HR</div>
        <div className="mt-1.5 text-2xl sm:text-3xl font-semibold italic" style={{ color: accent }}>
          {hrPts.length ? Math.max(...hrPts.map((p) => p.value)) : "\u2014"}<span className="text-xs text-ink-faint ml-1">bpm</span>
        </div>
        <Sparkline pts={hrPts} color={accent} format={(v) => `${Math.round(v)} bpm`} />
      </div>
      <div className="rounded-panel border border-border bg-surface-elevated p-4 sm:p-5">
        <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">TOTALS</div>
        <div className="mt-1.5 grid grid-cols-2 gap-4">
          <div>
            <div className="text-2xl sm:text-3xl font-semibold italic">{count}</div>
            <div className="font-mono text-[10px] text-ink-faint">sessions</div>
          </div>
          <div>
            <div className="text-2xl sm:text-3xl font-semibold italic">{totalMin}</div>
            <div className="font-mono text-[10px] text-ink-faint">total min</div>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---- Calisthenics Progress ---- */
function CalisProgress({ data }: { data: Record<string, unknown> }) {
  const accent = CATEGORIES.calisthenics.accent;
  const skills = Object.entries(data as Record<string, { points: { date: string; value: number }[]; is_hold: boolean }>);

  if (skills.length === 0) {
    return <div className="rounded-panel border border-border p-4 sm:p-8 text-center text-sm text-ink-faint">No calisthenics sessions logged yet.</div>;
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 sm:gap-4">
      {skills.map(([name, { points, is_hold }]) => (
        <div key={name} className="rounded-panel border border-border bg-surface-elevated p-4 sm:p-5">
          <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">{name.toUpperCase()}</div>
          <div className="mt-1.5 text-2xl sm:text-3xl font-semibold italic" style={{ color: accent }}>
            {points.at(-1)?.value ?? 0}
            <span className="text-[11px] text-ink-faint ml-1">{is_hold ? "sec best" : "reps best"}</span>
          </div>
          <Sparkline pts={points} color={accent} format={(v) => `${v} ${is_hold ? "sec" : "reps"}`} />
        </div>
      ))}
    </div>
  );
}

/* ---- Count Progress (mobility, sport, other) ---- */
function CountProgress({ data, accent }: { data: Record<string, unknown>; accent: string }) {
  const count = (data.session_count as number) || 0;
  const sessions = (data.sessions as { date: string; activity: string; duration_minutes: number | null }[]) || [];

  return (
    <div className="rounded-panel border border-border bg-surface-elevated p-4 sm:p-5">
      <div className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint">SESSIONS</div>
      <div className="mt-1.5 text-2xl sm:text-3xl font-semibold italic" style={{ color: accent }}>{count}</div>
      <div className="mt-4 space-y-1.5">
        {sessions.map((s, i) => (
          <div key={i} className="flex items-center gap-2 sm:gap-3 text-xs">
            <span className="font-mono text-[10px] text-ink-faint w-14 sm:w-16 shrink-0">
              {new Date(s.date + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" })}
            </span>
            <span className="flex-1 text-ink truncate">{s.activity}</span>
            {s.duration_minutes != null && <span className="font-mono text-[11px] text-ink-muted shrink-0">{s.duration_minutes} min</span>}
          </div>
        ))}
      </div>
    </div>
  );
}
