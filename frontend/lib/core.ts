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

// ── Day math (timezone-aware) ────────────────────────────────────────────────
// The backend stamps `MeditationSession.date` in the TENANT's local timezone
// (`tenant_today()`), so every "what day is it" question here — the streak
// anchor, the week window, the Today/Yesterday labels — must also resolve in the
// tenant's tz, NOT the viewing device's clock. We work in "YYYY-MM-DD" day-keys:
// session dates already arrive as keys, lexical compare is chronological, and
// stepping by whole calendar days sidesteps the 24h/DST assumption the old
// `getTime() - DAY_MS` math made.

/** The calendar day of `date` as seen in IANA zone `tz`, as "YYYY-MM-DD". */
export function dayKeyInTz(date: Date, tz: string): string {
  // en-CA renders ISO-style YYYY-MM-DD.
  return new Intl.DateTimeFormat("en-CA", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone: tz,
  }).format(date);
}

/** Shift a "YYYY-MM-DD" key by whole calendar days (anchored at UTC noon so a
 *  DST transition can't bump the result across a day boundary). */
function shiftDayKey(key: string, deltaDays: number): string {
  const [y, m, d] = key.split("-").map(Number);
  const dt = new Date(Date.UTC(y, (m || 1) - 1, d || 1, 12));
  dt.setUTCDate(dt.getUTCDate() + deltaDays);
  return dt.toISOString().slice(0, 10);
}

function relativeDateLabel(dateKey: string, tz: string): string {
  const today = dayKeyInTz(new Date(), tz);
  if (dateKey === today) return "Today";
  if (dateKey === shiftDayKey(today, -1)) return "Yesterday";
  // Format the month/day straight from the key parts — no tz reinterpretation.
  const [y, m, d] = dateKey.split("-").map(Number);
  return new Date(y, (m || 1) - 1, d || 1).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** API session row → the UI's Meditation shape. `tz` is the tenant's IANA zone. */
export function toMeditation(s: MeditationSession, tz: string): Meditation {
  return {
    id: s.id,
    date: s.date,
    dateLabel: relativeDateLabel(s.date, tz),
    title: s.title || "Your meditation",
    theme: s.theme || "",
    durationMin: s.duration_ms ? Math.max(1, Math.round(s.duration_ms / 60_000)) : 10,
    audioUrl: s.audio_url || undefined,
  };
}

/**
 * Derive the four stat cards from the ready library (no backend formula — the
 * raw sessions are the evidence; this is plain aggregation for display).
 * `meds` is expected newest-first (the API orders by -date, -created_at). `tz`
 * is the tenant's IANA zone, so "today" matches the day the dates were stamped.
 */
export function computeCoreStats(meds: Meditation[], tz: string): CoreStats {
  const today = dayKeyInTz(new Date(), tz); // tenant-local day, not the device's
  const weekFloor = shiftDayKey(today, -6); // last 7 calendar days, inclusive
  let sessionsThisWeek = 0;
  let totalMinutes = 0;
  const days = new Set<string>();
  for (const m of meds) {
    totalMinutes += m.durationMin;
    if (m.date >= weekFloor && m.date <= today) sessionsThisWeek += 1; // YYYY-MM-DD compares chronologically
    days.add(m.date);
  }
  // Streak: consecutive days with ≥1 sit, anchored at today (or yesterday if
  // nothing yet today, so an evening sit doesn't read as a broken streak).
  let streakDays = 0;
  let cursor = today;
  if (!days.has(cursor)) cursor = shiftDayKey(cursor, -1);
  while (days.has(cursor)) {
    streakDays += 1;
    cursor = shiftDayKey(cursor, -1);
  }
  return {
    sessionsThisWeek,
    totalMinutes,
    streakDays,
    lastSatLabel: meds.length ? meds[0].dateLabel : "—",
  };
}
