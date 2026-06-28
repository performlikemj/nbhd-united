// Shared ISO calendar-date helpers for daily-note slugs.
//
// Daily notes are keyed by a YYYY-MM-DD slug. These helpers centralise the
// construction so no caller can re-introduce the "NaN-NaN-NaN" slug bug: when
// a date string is fed to `new Date(...)` and turns out invalid, every getter
// returns NaN, and `${getFullYear()}-...` stringifies to the literal
// "NaN-NaN-NaN" — which then gets persisted as a garbage Document. `shiftISODate`
// and the hash parser route through `isISODate` so an invalid input always
// falls back to today instead of minting NaN.
//
// Local-timezone semantics (browser-local calendar day), matching the historical
// per-file `todayISO()`/`shiftDate()` behaviour this replaces.

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function format(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

/**
 * True when `s` is a real YYYY-MM-DD calendar date.
 *
 * Rejects shape mismatches ("daily", "", "NaN-NaN-NaN") and impossible dates
 * ("2026-13-40", "2026-02-30") by round-tripping through the Date constructor.
 */
export function isISODate(s: string | null | undefined): s is string {
  if (!s || !ISO_DATE_RE.test(s)) return false;
  const [y, m, d] = s.split("-").map(Number);
  const date = new Date(y, m - 1, d);
  return date.getFullYear() === y && date.getMonth() === m - 1 && date.getDate() === d;
}

/** Today as YYYY-MM-DD in the browser's local timezone. */
export function todayISO(): string {
  return format(new Date());
}

/**
 * Shift an ISO date by `days` (can be negative). If `dateStr` is not a valid
 * ISO date, falls back to today — so navigation can never produce "NaN-NaN-NaN".
 */
export function shiftISODate(dateStr: string, days: number): string {
  const base = isISODate(dateStr) ? dateStr : todayISO();
  const [y, m, d] = base.split("-").map(Number);
  const date = new Date(y, m - 1, d);
  date.setDate(date.getDate() + days);
  return format(date);
}
