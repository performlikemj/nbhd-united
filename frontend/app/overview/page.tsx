"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo } from "react";

import { SleepBars, WeightLine, type SleepNight } from "@/components/open-sky/charts";
import { OpenSkyPageHeader, OpenSkySection } from "@/components/open-sky/primitives";
import type { AssistantCardRow, AssistantPanelRef } from "@/lib/api";
import {
  useAssistantCardsQuery,
  useBodyWeightQuery,
  useMeQuery,
  useSleepQuery,
  useTenantQuery,
  useWorkoutsQuery,
} from "@/lib/queries";

/** YYYY-MM-DD for `date` in the user's timezone. */
function dayKey(date: Date, timeZone: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
}

function isoWeek(d: Date): number {
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const day = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  return Math.ceil(((t.getTime() - yearStart.getTime()) / 86_400_000 + 1) / 7);
}

function hoursLabel(h: number): string {
  const whole = Math.floor(h);
  const mins = Math.round((h - whole) * 60);
  return `${whole}h ${String(mins).padStart(2, "0")}m`;
}

const PANEL_COPY: Record<string, { line: string; href: string }> = {
  sleep: { line: "Sleep from Apple Health and your log", href: "/log" },
  workout: { line: "Your planned session", href: "/fuel" },
  schedule: { line: "From your calendar", href: "/overview#this-week" },
  log_table: { line: "Your logged numbers", href: "/log" },
  focus: { line: "A focus block", href: "/overview" },
  timer: { line: "A timer", href: "/overview" },
};

function PanelIcon({ kind }: { kind: string }) {
  const common = { fill: "none", stroke: "currentColor", strokeWidth: 1.5, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  switch (kind) {
    case "sleep":
      return (
        <svg viewBox="0 0 24 24" className="h-5 w-5" aria-hidden="true" {...common}>
          <path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z" />
        </svg>
      );
    case "workout":
      return (
        <svg viewBox="0 0 24 24" className="h-5 w-5" aria-hidden="true" {...common}>
          <path d="M3 10v4M6 8v8M18 8v8M21 10v4M6 12h12" />
        </svg>
      );
    case "schedule":
      return (
        <svg viewBox="0 0 24 24" className="h-5 w-5" aria-hidden="true" {...common}>
          <rect x="3.5" y="5" width="17" height="15" rx="2.5" />
          <path d="M3.5 10h17M8 3v4M16 3v4" />
        </svg>
      );
    default:
      return (
        <svg viewBox="0 0 24 24" className="h-5 w-5" aria-hidden="true" {...common}>
          <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
          <path d="M3.5 9.5h17M9.5 9.5v10" />
        </svg>
      );
  }
}

function panelTitle(p: AssistantPanelRef): string {
  if (p.title) return p.title;
  return p.kind.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

function firstLine(text: string): string {
  const line = text.split("\n").find((l) => l.trim()) ?? "";
  return line.replace(/[*_`#>]/g, "").trim();
}

function AssistantCards({ cards, timeZone }: { cards: AssistantCardRow[]; timeZone: string }) {
  if (cards.length === 0) return null;
  const [latest, ...earlier] = cards;
  const when = (iso: string) =>
    new Intl.DateTimeFormat("en-GB", { timeZone, weekday: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(iso));
  return (
    <OpenSkySection label="From your assistant" trailing={<span className="text-[0.8125rem] text-os-faint">{when(latest.created_at)}</span>}>
      {latest.text ? <p className="mb-3 max-w-[62ch] text-[1.0625rem] leading-relaxed text-os-ink">{firstLine(latest.text)}</p> : null}
      <ul className="grid gap-x-8 sm:grid-cols-2">
        {latest.panels.map((p, i) => {
          const copy = PANEL_COPY[p.kind] ?? { line: "", href: "/overview" };
          return (
            <li key={`${latest.id}-${i}`}>
              <Link href={copy.href} className="os-focus group flex min-h-[60px] items-center gap-3 os-hairline-top py-2">
                <span className="text-os-accent">
                  <PanelIcon kind={p.kind} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[0.9375rem] text-os-ink group-hover:text-white">{panelTitle(p)}</span>
                  {copy.line ? <span className="block truncate text-[0.8125rem] text-os-muted">{copy.line}</span> : null}
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
      {earlier.length > 0 ? (
        <details className="mt-3">
          <summary className="os-focus min-h-[44px] cursor-pointer list-none py-2 text-[0.9375rem] text-os-accent">
            Earlier cards ({earlier.length})
          </summary>
          <ul>
            {earlier.map((c) => (
              <li key={c.id} className="flex min-h-[48px] items-center justify-between gap-4 os-hairline-top">
                <span className="min-w-0 truncate text-[0.9375rem] text-os-ink">{c.panels.map(panelTitle).join(" · ")}</span>
                <span className="shrink-0 text-[0.8125rem] text-os-muted">{when(c.created_at)}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </OpenSkySection>
  );
}

export default function OverviewPage() {
  const router = useRouter();
  const { data: tenant, isLoading: tenantLoading } = useTenantQuery();
  const { data: me } = useMeQuery();
  const enabled = !!tenant?.web_redesign;

  // Tenants without the redesign keep Journal as their home.
  useEffect(() => {
    if (!tenantLoading && tenant && !tenant.web_redesign) router.replace("/journal");
  }, [tenant, tenantLoading, router]);

  const timeZone = me?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone;
  const now = useMemo(() => new Date(), []);
  const today = dayKey(now, timeZone);

  const sleepQuery = useSleepQuery();
  const weightQuery = useBodyWeightQuery();
  const cardsQuery = useAssistantCardsQuery(enabled);
  const weekStart = useMemo(() => {
    const d = new Date(now);
    const dow = (d.getDay() + 6) % 7; // Monday = 0
    d.setDate(d.getDate() - dow);
    return dayKey(d, timeZone);
  }, [now, timeZone]);
  const weekEnd = useMemo(() => {
    const d = new Date(now);
    const dow = (d.getDay() + 6) % 7;
    d.setDate(d.getDate() - dow + 6);
    return dayKey(d, timeZone);
  }, [now, timeZone]);
  const workoutsQuery = useWorkoutsQuery({ date_from: weekStart, date_to: weekEnd });

  const nights: SleepNight[] = useMemo(() => {
    const byDate = new Map((sleepQuery.data ?? []).map((s) => [s.date, Number(s.duration_hours)]));
    const labels = ["S", "M", "T", "W", "T", "F", "S"];
    return Array.from({ length: 7 }, (_, i) => {
      const d = new Date(now);
      d.setDate(d.getDate() - (6 - i));
      const key = dayKey(d, timeZone);
      return { date: key, label: labels[d.getDay()], hours: byDate.get(key) ?? null };
    });
  }, [sleepQuery.data, now, timeZone]);
  const logged = nights.filter((n) => n.hours != null) as { hours: number }[];
  const avgSleep = logged.length ? logged.reduce((a, n) => a + n.hours, 0) / logged.length : null;

  const weights = useMemo(
    () =>
      (weightQuery.data ?? [])
        .filter((w) => {
          const cutoff = new Date(now.getTime() - 30 * 86_400_000);
          return w.date >= dayKey(cutoff, timeZone);
        })
        .map((w) => ({ date: w.date, kg: Number(w.weight_kg) })),
    [weightQuery.data, now, timeZone],
  );
  const latestWeight = [...weights].sort((a, b) => b.date.localeCompare(a.date))[0];
  const firstWeight = [...weights].sort((a, b) => a.date.localeCompare(b.date))[0];
  const weightDelta = latestWeight && firstWeight ? latestWeight.kg - firstWeight.kg : null;

  const workouts = (workoutsQuery.data ?? []).filter((w) => w.status !== "rest").sort((a, b) => a.date.localeCompare(b.date));
  const doneCount = workouts.filter((w) => w.status === "done").length;

  if (!enabled) return null;

  const dateTitle = new Intl.DateTimeFormat("en-GB", { timeZone, weekday: "long", day: "numeric", month: "long" }).format(now);
  const city = me?.location_city?.trim();

  return (
    <div className="space-y-10">
      <OpenSkyPageHeader
        eyebrow={`Week ${isoWeek(now)}${city ? ` · ${city}` : ""}`}
        title={dateTitle}
      />

      <AssistantCards cards={cardsQuery.data ?? []} timeZone={timeZone} />

      <div className="grid gap-x-12 gap-y-10 md:grid-cols-2">
        <OpenSkySection
          label="Sleep"
          trailing={
            <Link href="/log" className="os-focus text-[0.875rem] text-os-accent">
              Open log
            </Link>
          }
        >
          {avgSleep == null ? (
            <p className="py-2 text-[0.9375rem] text-os-muted">No sleep logged this week yet. It fills in from Apple Health on your iPhone.</p>
          ) : (
            <>
              <p className="os-num mb-3 text-[2rem] font-light leading-none text-white">
                {hoursLabel(avgSleep)} <span className="text-[0.875rem] font-normal text-os-muted">average</span>
              </p>
              <SleepBars nights={nights} />
            </>
          )}
        </OpenSkySection>

        <OpenSkySection
          label="Weight · 30 days"
          trailing={
            <Link href="/log" className="os-focus text-[0.875rem] text-os-accent">
              Edit log
            </Link>
          }
        >
          {latestWeight ? (
            <>
              <p className="os-num mb-1 text-[2rem] font-light leading-none text-white">
                {latestWeight.kg.toFixed(1)} <span className="text-[0.875rem] font-normal text-os-muted">kg</span>
              </p>
              {weightDelta != null ? (
                <p className="os-num mb-3 text-[0.875rem] text-os-muted">
                  {weightDelta > 0 ? "+" : weightDelta < 0 ? "−" : ""}
                  {Math.abs(weightDelta).toFixed(1)} kg over 30 days
                </p>
              ) : null}
              <WeightLine points={weights} />
            </>
          ) : (
            <p className="py-2 text-[0.9375rem] text-os-muted">No weight logged in the last 30 days.</p>
          )}
        </OpenSkySection>

        <OpenSkySection id="this-week" label="This week">
          <p className="py-2 text-[0.9375rem] text-os-muted">
            Your calendar lands here soon. It will show what your iPhone has synced, and say when it last did.
          </p>
        </OpenSkySection>

        <OpenSkySection
          label={`Training · ${doneCount} of ${workouts.length} done`}
          trailing={
            <Link href="/fuel" className="os-focus text-[0.875rem] text-os-accent">
              Open Fuel
            </Link>
          }
        >
          {workouts.length === 0 ? (
            <p className="py-2 text-[0.9375rem] text-os-muted">No sessions planned this week.</p>
          ) : (
            <ul>
              {workouts.map((w) => {
                const done = w.status === "done";
                const isToday = w.date === today;
                const day = new Intl.DateTimeFormat("en-GB", { timeZone, weekday: "short" }).format(new Date(`${w.date}T12:00:00`));
                return (
                  <li key={w.id} className="flex min-h-[52px] items-center gap-3 os-hairline-top">
                    <span
                      className={
                        done
                          ? "flex h-6 w-6 items-center justify-center rounded-full border border-os-done text-os-done"
                          : isToday
                            ? "h-6 w-6 rounded-full border border-white"
                            : "h-6 w-6 rounded-full border border-os-ring"
                      }
                      aria-hidden="true"
                    >
                      {done ? (
                        <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={2.4} strokeLinecap="round">
                          <path d="M5 12l5 5 9-10" />
                        </svg>
                      ) : null}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-[1rem] text-os-ink">{w.activity}</span>
                    <span className="shrink-0 text-[0.875rem] text-os-muted">
                      {isToday ? "Today" : day}
                      <span className="sr-only">{done ? ", done" : ", planned"}</span>
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </OpenSkySection>
      </div>
    </div>
  );
}
