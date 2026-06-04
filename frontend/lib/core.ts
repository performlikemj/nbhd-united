// Core (mindfulness) pillar — shared UI types, the fixed meditation arc, and
// helpers that map API `MeditationSession` rows into the shape the tab renders.
// (Replaces the earlier `core-mock.ts`: the arc + types are real domain data;
// the rendered sessions now come from `/api/v1/core/sessions/`.)

import type { MeditationSession } from "@/lib/types";

export interface MeditationPhase {
  name: string;
  label: string;
  /** relative weight of the phase's time budget (drives timeline segment width) */
  weight: number;
}

export interface Meditation {
  id: string;
  date: string; // ISO (YYYY-MM-DD)
  dateLabel: string; // "Today", "Yesterday", "Jun 1"
  title: string;
  theme: string;
  durationMin: number;
  audioUrl?: string;
}

export interface CoreStats {
  sessionsThisWeek: number;
  totalMinutes: number;
  streakDays: number;
  lastSatLabel: string;
}

// The fixed meditation arc — matches the render-manifest scaffolding the backend
// enforces (apps/core/compose.py). The session API doesn't echo phases back
// (they're invariant), so the timeline reads from this constant.
export const PHASES: MeditationPhase[] = [
  { name: "arrival", label: "Arrival", weight: 60 },
  { name: "breath_anchor", label: "Breath", weight: 75 },
  { name: "body_scan", label: "Body scan", weight: 150 },
  { name: "core_practice", label: "Core practice", weight: 210 },
  { name: "integration", label: "Integration", weight: 60 },
  { name: "closing", label: "Closing", weight: 45 },
];

const DAY_MS = 86_400_000;

/** Parse a "YYYY-MM-DD" string as a *local* midnight (avoids UTC day-shift in JST). */
function parseLocalDate(s: string): Date {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, (m || 1) - 1, d || 1);
}

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function relativeDateLabel(dateStr: string): string {
  const date = startOfDay(parseLocalDate(dateStr));
  const today = startOfDay(new Date());
  const diffDays = Math.round((today.getTime() - date.getTime()) / DAY_MS);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** API session row → the UI's Meditation shape. */
export function toMeditation(s: MeditationSession): Meditation {
  return {
    id: s.id,
    date: s.date,
    dateLabel: relativeDateLabel(s.date),
    title: s.title || "Your meditation",
    theme: s.theme || "",
    durationMin: s.duration_ms ? Math.max(1, Math.round(s.duration_ms / 60_000)) : 10,
    audioUrl: s.audio_url || undefined,
  };
}

/**
 * Derive the four stat cards from the ready library (no backend formula — the
 * raw sessions are the evidence; this is plain aggregation for display).
 * `meds` is expected newest-first (the API orders by -date, -created_at).
 */
export function computeCoreStats(meds: Meditation[]): CoreStats {
  const today = startOfDay(new Date()).getTime();
  const weekFloor = today - 6 * DAY_MS; // last 7 calendar days, inclusive
  let sessionsThisWeek = 0;
  let totalMinutes = 0;
  const days = new Set<number>();
  for (const m of meds) {
    totalMinutes += m.durationMin;
    const day = startOfDay(parseLocalDate(m.date)).getTime();
    if (day >= weekFloor) sessionsThisWeek += 1;
    days.add(day);
  }
  // Streak: consecutive days with ≥1 sit, anchored at today (or yesterday if
  // nothing yet today, so an evening sit doesn't read as a broken streak).
  let streakDays = 0;
  let cursor = today;
  if (!days.has(cursor)) cursor -= DAY_MS;
  while (days.has(cursor)) {
    streakDays += 1;
    cursor -= DAY_MS;
  }
  return {
    sessionsThisWeek,
    totalMinutes,
    streakDays,
    lastSatLabel: meds.length ? meds[0].dateLabel : "—",
  };
}
