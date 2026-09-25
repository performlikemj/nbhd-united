"use client";

import { useMemo } from "react";

import { OpenSkySection } from "@/components/open-sky/primitives";
import { useDatebookAgendaQuery } from "@/lib/queries";
import type { AgendaItem, DatebookAgenda } from "@/lib/types";

type OkAgenda = Extract<DatebookAgenda, { state: "ok" }>;

function addDays(isoDay: string, n: number): string {
  const d = new Date(`${isoDay}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

function dayLabel(isoDay: string, today: string): string {
  if (isoDay === today) return "Today";
  if (isoDay === addDays(today, 1)) return "Tomorrow";
  const d = new Date(`${isoDay}T12:00:00Z`);
  return `${d.toLocaleDateString(undefined, { weekday: "short", timeZone: "UTC" })} ${d.getUTCDate()}`;
}

function clock(iso: string, timeZone: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", timeZone });
}

function localClock(local: string): string {
  // "YYYY-MM-DDTHH:MM[:SS]" with no zone: show the wall-clock time as written.
  const [h, m] = (local.split("T")[1] ?? "").split(":").map(Number);
  if (Number.isNaN(h)) return "";
  const d = new Date(Date.UTC(2000, 0, 1, h, m || 0));
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", timeZone: "UTC" });
}

/** Sort key + display time for one item on one day. */
function when(item: AgendaItem, day: string, timeZone: string): { sort: string; text: string } {
  if (item.entity === "event") {
    const t = item.time;
    if (t.kind === "all_day") return { sort: "0", text: "All day" };
    if (t.kind === "zoned") {
      const startsToday = new Intl.DateTimeFormat("en-CA", { timeZone }).format(new Date(t.start_at)) === day;
      return startsToday ? { sort: `1${t.start_at}`, text: clock(t.start_at, timeZone) } : { sort: "0", text: "Continues" };
    }
    return t.start_local.slice(0, 10) === day ? { sort: `1${t.start_local}`, text: localClock(t.start_local) } : { sort: "0", text: "Continues" };
  }
  const due = item.due;
  if (due.kind === "all_day") return { sort: "2", text: "Reminder" };
  if (due.kind === "zoned") return { sort: `1${due.due_at}`, text: `Due ${clock(due.due_at, timeZone)}` };
  return { sort: `1${due.due_local}`, text: `Due ${localClock(due.due_local)}` };
}

/** All-day events show on every day they cover inside the window; others on their own day. */
function itemsByDay(agenda: OkAgenda, days: string[]): Map<string, AgendaItem[]> {
  const map = new Map<string, AgendaItem[]>(days.map((d) => [d, []]));
  for (const item of agenda.items) {
    if (item.entity === "event" && item.time.kind === "all_day") {
      for (const d of days) {
        if (d >= item.time.start_date && d < item.time.end_date_exclusive) map.get(d)?.push(item);
      }
      continue;
    }
    map.get(item.day)?.push(item);
  }
  return map;
}

function ago(iso: string, nowIso: string): string {
  const mins = Math.max(0, Math.round((new Date(nowIso).getTime() - new Date(iso).getTime()) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} ${hours === 1 ? "hour" : "hours"} ago`;
  const daysAgo = Math.round(hours / 24);
  return `${daysAgo} ${daysAgo === 1 ? "day" : "days"} ago`;
}

function Week({ agenda }: { agenda: OkAgenda }) {
  const covered = useMemo(() => new Set(agenda.covered_days), [agenda.covered_days]);
  const days = useMemo(() => {
    const out: string[] = [];
    for (let d = agenda.requested.start_day; d < agenda.requested.end_day_exclusive; d = addDays(d, 1)) out.push(d);
    return out;
  }, [agenda.requested.start_day, agenda.requested.end_day_exclusive]);
  const byDay = useMemo(() => itemsByDay(agenda, days), [agenda, days]);
  const today = agenda.requested.start_day;

  return (
    <ul>
      {days.map((day) => {
        const items = (byDay.get(day) ?? [])
          .map((item) => ({ item, ...when(item, day, agenda.timezone) }))
          .sort((a, b) => a.sort.localeCompare(b.sort));
        const isCovered = covered.has(day);
        return (
          <li key={day} className="os-hairline-top grid grid-cols-[5.5rem_1fr] gap-x-4 py-3 first:border-t-0 first:pt-1">
            <span className={`text-[0.875rem] ${day === today ? "text-os-ink" : "text-os-muted"}`}>{dayLabel(day, today)}</span>
            {items.length ? (
              <ul className="min-w-0 space-y-1.5">
                {items.map(({ item, text }) => (
                  <li key={`${item.entity}-${item.id}-${day}`} className="flex min-w-0 items-baseline gap-3">
                    <span className="w-[4.75rem] shrink-0 text-[0.8125rem] tabular-nums text-os-muted">{text}</span>
                    <span className="min-w-0 truncate text-[0.9375rem] text-os-ink">{item.title}</span>
                  </li>
                ))}
                {!isCovered ? <li className="text-[0.75rem] text-os-faint">May be missing events — not fully synced</li> : null}
              </ul>
            ) : (
              <span className={`text-[0.9375rem] ${isCovered ? "text-os-ink" : "text-os-faint"}`}>{isCovered ? "Free" : "Not synced"}</span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/**
 * Overview "This week": the iPhone-synced calendar for the next 7 days. A day
 * says "Free" only when the last complete sync covered all of it; otherwise it
 * says "Not synced" rather than guessing.
 */
export function ThisWeekCard({ enabled }: { enabled: boolean }) {
  const { data, isLoading, error } = useDatebookAgendaQuery(enabled, 7);

  let freshness: string | null = null;
  if (data?.state === "ok") {
    const at = data.freshness.events_last_complete_sync_at;
    freshness = at ? `Synced from your iPhone ${ago(at, data.server_now)}` : "Not synced from your iPhone yet";
  }

  return (
    <OpenSkySection
      id="this-week"
      label="This week"
      trailing={freshness ? <span className="text-[0.8125rem] text-os-muted">{freshness}</span> : undefined}
    >
      {isLoading ? (
        <p className="py-2 text-[0.9375rem] text-os-muted">Loading your week…</p>
      ) : error ? (
        <p className="py-2 text-[0.9375rem] text-os-muted">Couldn&rsquo;t load your calendar right now.</p>
      ) : !data || data.state === "datebook_disabled" ? (
        <p className="py-2 text-[0.9375rem] text-os-muted">
          Your calendar isn&rsquo;t connected. Turn on Calendar in the NBHD iPhone app to see your week here.
        </p>
      ) : data.state === "consent_required" ? (
        <p className="py-2 text-[0.9375rem] text-os-muted">
          Your calendar needs your OK first. Open Calendar in the NBHD iPhone app to allow it.
        </p>
      ) : (
        <>
          <Week agenda={data} />
          {data.truncated ? <p className="mt-2 text-[0.8125rem] text-os-muted">More in your iPhone calendar.</p> : null}
        </>
      )}
    </OpenSkySection>
  );
}
