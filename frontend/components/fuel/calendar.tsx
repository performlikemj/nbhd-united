"use client";

import { useMemo, useState } from "react";

import { useFuelCalendarQuery } from "@/lib/queries";
import type { WorkoutCategory, WorkoutStub } from "@/lib/types";
import { CATEGORIES, CATEGORY_IDS } from "./category-meta";

function isoFromParts(y: number, m: number, d: number): string {
  return `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

interface CalendarProps {
  onSelectDay: (iso: string) => void;
}

export function Calendar({ onSelectDay }: CalendarProps) {
  const today = new Date();
  const todayISO = isoFromParts(today.getFullYear(), today.getMonth(), today.getDate());
  const [cursor, setCursor] = useState({ y: today.getFullYear(), m: today.getMonth() });

  const { data: calendarData } = useFuelCalendarQuery(cursor.y, cursor.m + 1);

  const byDate = useMemo(() => {
    const m: Record<string, WorkoutStub[]> = {};
    for (const entry of calendarData || []) {
      m[entry.date] = entry.workouts;
    }
    return m;
  }, [calendarData]);

  const monthStart = new Date(cursor.y, cursor.m, 1);
  const startWeekday = (monthStart.getDay() + 6) % 7; // Monday-first
  const daysInMonth = new Date(cursor.y, cursor.m + 1, 0).getDate();
  const prevMonthDays = new Date(cursor.y, cursor.m, 0).getDate();

  const cells: { y: number; m: number; d: number; out: boolean }[] = [];
  for (let i = startWeekday - 1; i >= 0; i--) {
    cells.push({
      y: cursor.m === 0 ? cursor.y - 1 : cursor.y,
      m: (cursor.m + 11) % 12,
      d: prevMonthDays - i,
      out: true,
    });
  }
  for (let d = 1; d <= daysInMonth; d++) {
    cells.push({ y: cursor.y, m: cursor.m, d, out: false });
  }
  while (cells.length % 7 !== 0 || cells.length < 35) {
    cells.push({
      y: cursor.m === 11 ? cursor.y + 1 : cursor.y,
      m: (cursor.m + 1) % 12,
      d: cells.length - (startWeekday + daysInMonth) + 1,
      out: true,
    });
  }

  const monthLabel = monthStart.toLocaleDateString("en-US", { month: "long", year: "numeric" });

  // Weekly summary
  const weekTotal = useMemo(() => {
    const ws: string[] = [];
    for (let i = 0; i < 7; i++) {
      const d = new Date(today);
      d.setDate(d.getDate() - i);
      ws.push(isoFromParts(d.getFullYear(), d.getMonth(), d.getDate()));
    }
    const seen = ws.flatMap((iso) => (byDate[iso] || []).filter((w) => w.status === "done"));
    const mins = seen.reduce((a, w) => a + (w.duration_minutes || 0), 0);
    return { count: seen.length, hours: Math.round((mins / 60) * 10) / 10 };
  }, [byDate, todayISO]);

  const goMonth = (delta: number) => {
    setCursor((c) => {
      const d = new Date(c.y, c.m + delta, 1);
      return { y: d.getFullYear(), m: d.getMonth() };
    });
  };

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <div className="flex items-baseline gap-3">
            <h2 className="text-3xl font-semibold italic">{monthLabel}</h2>
            <button
              onClick={() => setCursor({ y: today.getFullYear(), m: today.getMonth() })}
              className="text-[10px] font-bold uppercase tracking-[0.2em] text-ink-muted hover:text-ink transition"
            >
              TODAY
            </button>
          </div>
          <div className="mt-1 text-xs text-ink-faint font-mono">
            This week &middot; {weekTotal.count} sessions &middot; {weekTotal.hours}h
          </div>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => goMonth(-1)}
            className="h-8 w-8 rounded-full border border-border hover:border-border-strong text-ink-muted hover:text-ink flex items-center justify-center transition"
            aria-label="Previous month"
          >
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="m15 18-6-6 6-6" /></svg>
          </button>
          <button
            onClick={() => goMonth(1)}
            className="h-8 w-8 rounded-full border border-border hover:border-border-strong text-ink-muted hover:text-ink flex items-center justify-center transition"
            aria-label="Next month"
          >
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="m9 18 6-6-6-6" /></svg>
          </button>
        </div>
      </div>

      {/* Weekday header */}
      <div className="grid grid-cols-7 gap-1.5">
        {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => (
          <div key={d} className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint px-2 py-1">
            {d}
          </div>
        ))}
      </div>

      {/* Grid */}
      <div className="grid grid-cols-7 gap-1.5">
        {cells.map((c, i) => {
          const iso = isoFromParts(c.y, c.m, c.d);
          const items = byDate[iso] || [];
          const isToday = iso === todayISO;
          return (
            <button
              key={i}
              onClick={() => onSelectDay(iso)}
              className={`group relative aspect-[1/1] sm:aspect-[1.15/1] rounded-lg border text-left p-2 transition ${
                c.out
                  ? "border-white/[0.02] bg-transparent opacity-40"
                  : isToday
                    ? "border-accent/50 bg-accent/[0.04]"
                    : "border-border bg-surface-elevated hover:border-border-strong hover:bg-surface-hover"
              }`}
            >
              <div className="flex items-start justify-between">
                <span className={`font-mono text-[11px] ${isToday ? "text-accent" : c.out ? "text-ink-faint" : "text-ink-muted"}`}>
                  {c.d}
                </span>
                {items.length > 0 && !c.out && (
                  <span className="font-mono text-[9px] text-ink-faint">{items.length}</span>
                )}
              </div>
              <div className="mt-1 space-y-0.5">
                {items.slice(0, 3).map((w) => {
                  const accent = CATEGORIES[w.category as WorkoutCategory]?.accent || "#94a3b8";
                  const planned = w.status === "planned";
                  return (
                    <div
                      key={w.id}
                      className={`truncate text-[10px] leading-tight flex items-center gap-1 ${planned ? "opacity-70" : ""}`}
                    >
                      <span
                        className="h-1 w-1 rounded-full shrink-0"
                        style={{ background: accent, opacity: planned ? 0.5 : 1 }}
                      />
                      <span className="truncate text-ink-muted">{w.activity}</span>
                    </div>
                  );
                })}
                {items.length > 3 && (
                  <div className="text-[9px] font-mono text-ink-faint">+{items.length - 3} more</div>
                )}
              </div>
              {isToday && (
                <span className="absolute bottom-1.5 right-1.5 text-[7px] font-bold uppercase tracking-[0.2em] text-accent">
                  TODAY
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* Legend */}
      <div className="flex flex-wrap items-center gap-3 text-[10px] font-mono text-ink-faint pt-2">
        <span>Click a day to open it.</span>
        <span className="text-border">&middot;</span>
        <div className="flex items-center gap-3">
          {CATEGORY_IDS.map((c) => (
            <span key={c} className="flex items-center gap-1.5">
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: CATEGORIES[c].accent }} />
              {CATEGORIES[c].label}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
