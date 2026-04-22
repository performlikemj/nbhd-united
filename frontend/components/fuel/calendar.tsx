"use client";

import { useMemo, useState } from "react";

import { useFuelCalendarQuery } from "@/lib/queries";
import type { WorkoutCategory, WorkoutStub } from "@/lib/types";
import { CATEGORIES, CATEGORY_IDS } from "./category-meta";

function isoFromParts(y: number, m: number, d: number): string {
  return `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

const WEEKDAYS_SHORT = ["M", "T", "W", "T", "F", "S", "S"];
const WEEKDAYS_LONG = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

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
  const startWeekday = (monthStart.getDay() + 6) % 7;
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

  const weekTotal = useMemo(() => {
    const now = new Date();
    const ws: string[] = [];
    for (let i = 0; i < 7; i++) {
      const d = new Date(now);
      d.setDate(d.getDate() - i);
      ws.push(isoFromParts(d.getFullYear(), d.getMonth(), d.getDate()));
    }
    const seen = ws.flatMap((iso) => (byDate[iso] || []).filter((w) => w.status === "done"));
    const mins = seen.reduce((a, w) => a + (w.duration_minutes || 0), 0);
    return { count: seen.length, hours: Math.round((mins / 60) * 10) / 10 };
  }, [byDate]);

  const goMonth = (delta: number) => {
    setCursor((c) => {
      const d = new Date(c.y, c.m + delta, 1);
      return { y: d.getFullYear(), m: d.getMonth() };
    });
  };

  return (
    <div className="space-y-3 sm:space-y-4">
      {/* Header */}
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <div className="flex items-baseline gap-3">
            <h2 className="text-2xl sm:text-3xl font-semibold italic">{monthLabel}</h2>
            <button
              onClick={() => setCursor({ y: today.getFullYear(), m: today.getMonth() })}
              className="text-[10px] font-bold uppercase tracking-[0.2em] text-ink-muted hover:text-ink transition min-h-[44px] flex items-center"
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
            className="h-11 w-11 sm:h-10 sm:w-10 rounded-full border border-border hover:border-border-strong text-ink-muted hover:text-ink flex items-center justify-center transition"
            aria-label="Previous month"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="m15 18-6-6 6-6" /></svg>
          </button>
          <button
            onClick={() => goMonth(1)}
            className="h-11 w-11 sm:h-10 sm:w-10 rounded-full border border-border hover:border-border-strong text-ink-muted hover:text-ink flex items-center justify-center transition"
            aria-label="Next month"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="m9 18 6-6-6-6" /></svg>
          </button>
        </div>
      </div>

      {/* Weekday header */}
      <div className="grid grid-cols-7 gap-0.5 sm:gap-1.5">
        {WEEKDAYS_LONG.map((d, i) => (
          <div key={d} className="text-[9px] sm:text-[10px] font-bold uppercase tracking-[0.15em] text-ink-faint px-1 sm:px-2 py-1 text-center sm:text-left">
            <span className="sm:hidden">{WEEKDAYS_SHORT[i]}</span>
            <span className="hidden sm:inline">{d}</span>
          </div>
        ))}
      </div>

      {/* Grid */}
      <div className="grid grid-cols-7 gap-0.5 sm:gap-1.5">
        {cells.map((c, i) => {
          const iso = isoFromParts(c.y, c.m, c.d);
          const items = byDate[iso] || [];
          const isToday = iso === todayISO;
          return (
            <button
              key={i}
              onClick={() => onSelectDay(iso)}
              className={`group relative min-h-[44px] sm:aspect-[1.15/1] rounded-md sm:rounded-lg border text-left p-1 sm:p-2 transition ${
                c.out
                  ? "border-white/[0.02] bg-transparent opacity-40"
                  : isToday
                    ? "border-accent/50 bg-accent/[0.04]"
                    : "border-border bg-surface-elevated hover:border-border-strong hover:bg-surface-hover"
              }`}
            >
              <div className="flex items-start justify-between">
                <span className={`font-mono text-[10px] sm:text-[11px] ${isToday ? "text-accent" : c.out ? "text-ink-faint" : "text-ink-muted"}`}>
                  {c.d}
                </span>
                {items.length > 0 && !c.out && (
                  <span className="font-mono text-[8px] sm:text-[9px] text-ink-faint">{items.length}</span>
                )}
              </div>
              {/* Activity slivers — dots only on mobile, text on sm+ */}
              <div className="mt-0.5 sm:mt-1 space-y-0.5">
                {items.slice(0, 3).map((w) => {
                  const accent = CATEGORIES[w.category as WorkoutCategory]?.accent || "#94a3b8";
                  const planned = w.status === "planned";
                  return (
                    <div
                      key={w.id}
                      className={`flex items-center gap-1 ${planned ? "opacity-70" : ""}`}
                    >
                      <span
                        className="h-1.5 w-1.5 sm:h-1 sm:w-1 rounded-full shrink-0"
                        style={{ background: accent, opacity: planned ? 0.5 : 1 }}
                      />
                      <span className="hidden sm:block truncate text-[10px] leading-tight text-ink-muted">{w.activity}</span>
                    </div>
                  );
                })}
                {items.length > 3 && (
                  <div className="text-[8px] sm:text-[9px] font-mono text-ink-faint">+{items.length - 3}</div>
                )}
              </div>
              {isToday && (
                <span className="absolute bottom-0.5 right-0.5 sm:bottom-1.5 sm:right-1.5 text-[6px] sm:text-[7px] font-bold uppercase tracking-[0.15em] text-accent">
                  TODAY
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* Legend — horizontal scroll on mobile */}
      <div className="overflow-x-auto -mx-4 px-4 sm:mx-0 sm:px-0">
        <div className="flex items-center gap-3 text-[10px] font-mono text-ink-faint pt-2 min-w-max sm:min-w-0 sm:flex-wrap">
          <span className="hidden sm:inline">Click a day to open it.</span>
          <span className="hidden sm:inline text-border">&middot;</span>
          {CATEGORY_IDS.map((c) => (
            <span key={c} className="flex items-center gap-1">
              <span className="h-1.5 w-1.5 rounded-full shrink-0" style={{ background: CATEGORIES[c].accent }} />
              {CATEGORIES[c].label}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
