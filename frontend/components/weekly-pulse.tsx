"use client";

import { useState } from "react";
import { MarkdownRenderer } from "@/components/markdown-renderer";
import { HorizonsWeeklyDocument, HorizonsWeeklyPulse, WeekRating } from "@/lib/types";

type PulseEntry = {
  key: string;
  weekStart: string;
  weekEnd: string;
  rating: WeekRating | null;
  summary: string;
  markdown: string | null;
};

const RATING_EMOJI: Record<WeekRating, string> = {
  "thumbs-up": "\uD83D\uDE80",
  meh: "\uD83E\uDDD8",
  "thumbs-down": "\uD83D\uDD25",
};

const SHORT_MONTH = { month: "short", day: "numeric" } as const;

function formatWeekRange(start: string, end: string): string {
  const s = new Date(start + "T00:00:00");
  const e = new Date(end + "T00:00:00");
  if (Number.isNaN(s.getTime()) || Number.isNaN(e.getTime())) return start;
  const fmt = (d: Date) => d.toLocaleDateString(undefined, SHORT_MONTH);
  return `${fmt(s)}\u2013${fmt(e)}`;
}

function isoWeekNumber(iso: string): number | null {
  const d = new Date(iso + "T00:00:00Z");
  if (Number.isNaN(d.getTime())) return null;
  const target = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  const day = target.getUTCDay() || 7;
  target.setUTCDate(target.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(target.getUTCFullYear(), 0, 1));
  return Math.ceil(((target.getTime() - yearStart.getTime()) / 86_400_000 + 1) / 7);
}

function entriesFromPulse(weeks: HorizonsWeeklyPulse[]): PulseEntry[] {
  return weeks.map((w) => ({
    key: `pulse:${w.week_start}`,
    weekStart: w.week_start,
    weekEnd: w.week_end,
    rating: w.week_rating,
    summary: w.top_win ?? "",
    markdown: null,
  }));
}

function entriesFromDocuments(documents: HorizonsWeeklyDocument[]): PulseEntry[] {
  return documents.map((d) => ({
    key: `doc:${d.id}`,
    weekStart: d.week_start,
    weekEnd: d.week_end,
    rating: null,
    summary: d.preview ?? "",
    markdown: d.markdown ?? null,
  }));
}

export function WeeklyPulse({
  weeks,
  documents = [],
}: {
  weeks: HorizonsWeeklyPulse[];
  documents?: HorizonsWeeklyDocument[];
}) {
  const entries = weeks.length > 0 ? entriesFromPulse(weeks) : entriesFromDocuments(documents);

  if (entries.length === 0) {
    return (
      <p className="py-6 text-center text-sm text-ink-muted">
        Your weekly reflections will appear here. The first one arrives this Monday.
      </p>
    );
  }

  return (
    <ul className="divide-y divide-border/60">
      {entries.map((entry, index) => (
        <li key={entry.key}>
          <WeekRow entry={entry} index={index} />
        </li>
      ))}
    </ul>
  );
}

function WeekRow({ entry, index }: { entry: PulseEntry; index: number }) {
  const [expanded, setExpanded] = useState(false);
  const canExpand = Boolean(entry.markdown && entry.markdown.trim().length > 0);
  const weekNum = isoWeekNumber(entry.weekStart);
  const range = formatWeekRange(entry.weekStart, entry.weekEnd);
  const emoji = entry.rating ? RATING_EMOJI[entry.rating] : null;
  const opacity = expanded ? 1 : Math.max(0.55, 1 - index * 0.12);

  return (
    <div className="py-3 transition-opacity" style={{ opacity }}>
      <button
        type="button"
        onClick={() => canExpand && setExpanded((v) => !v)}
        aria-expanded={canExpand ? expanded : undefined}
        disabled={!canExpand}
        className={`group flex w-full items-start gap-3 text-left ${
          canExpand ? "cursor-pointer" : "cursor-default"
        }`}
      >
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-widest text-ink-faint">
            {weekNum ? <span>W{String(weekNum).padStart(2, "0")}</span> : null}
            {weekNum ? <span aria-hidden="true">&middot;</span> : null}
            <span>{range}</span>
          </div>
          {entry.summary ? (
            <p
              className={`mt-1 text-sm leading-relaxed text-ink-muted group-hover:text-ink transition-colors ${
                expanded ? "" : "line-clamp-2"
              }`}
            >
              {entry.summary}
            </p>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-2 pt-0.5">
          {emoji ? (
            <span className="text-lg leading-none" aria-hidden="true">
              {emoji}
            </span>
          ) : null}
          {canExpand ? (
            <span
              aria-hidden="true"
              className={`text-ink-faint transition-transform ${expanded ? "rotate-180" : ""}`}
            >
              {"\u25be"}
            </span>
          ) : null}
        </div>
      </button>
      {canExpand && expanded ? (
        <div className="mt-3 rounded-md bg-surface-elevated/50 px-3 py-2">
          <MarkdownRenderer content={entry.markdown ?? ""} />
        </div>
      ) : null}
    </div>
  );
}
