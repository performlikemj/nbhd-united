"use client";

import { useState } from "react";

import { SectionCardSkeleton } from "@/components/skeleton";
import { DailyNoteSection } from "@/components/journal/daily-note-section";
import { QuickLogInput } from "@/components/journal/quick-log-input";
import {
  useDailyNoteQuery,
  useCreateDailyNoteEntryMutation,
  useUpdateDailyNoteSectionMutation,
} from "@/lib/queries";

function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

function shiftDate(date: string, days: number): string {
  const d = new Date(date + "T00:00:00");
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

function formatDate(date: string): string {
  return new Date(date + "T00:00:00").toLocaleDateString(undefined, {
    weekday: "short",
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export default function TodayPage() {
  const [selectedDate, setSelectedDate] = useState(todayISO);
  const { data, isLoading, error } = useDailyNoteQuery(selectedDate);
  const createEntryMutation = useCreateDailyNoteEntryMutation(selectedDate);
  const sectionMutation = useUpdateDailyNoteSectionMutation(selectedDate);

  const sections = data?.sections ?? [];

  const handleSaveSection = async (slug: string, content: string) => {
    await sectionMutation.mutateAsync({ slug, content });
  };

  const handleQuickLog = async (content: string) => {
    await createEntryMutation.mutateAsync({ content });
  };

  return (
    <div className="space-y-4">
      {/* Date navigation */}
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => setSelectedDate((d) => shiftDate(d, -1))}
          className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40"
        >
          &larr;
        </button>
        <label className="relative cursor-pointer">
          <span className="text-base font-medium text-ink">{formatDate(selectedDate)}</span>
          <input
            type="date"
            className="absolute inset-0 cursor-pointer opacity-0"
            value={selectedDate}
            onChange={(e) => e.target.value && setSelectedDate(e.target.value)}
          />
        </label>
        <button
          type="button"
          onClick={() => setSelectedDate((d) => shiftDate(d, 1))}
          disabled={selectedDate >= todayISO()}
          className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-45"
        >
          &rarr;
        </button>
        {selectedDate !== todayISO() ? (
          <button
            type="button"
            onClick={() => setSelectedDate(todayISO())}
            className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40"
          >
            Today
          </button>
        ) : null}
      </div>

      {/* Template name */}
      {data?.template_name ? (
        <p className="text-sm text-ink/50">Template: {data.template_name}</p>
      ) : null}

      {/* Sections */}
      {isLoading ? (
        <div className="space-y-4">
          <SectionCardSkeleton lines={6} />
          <SectionCardSkeleton lines={4} />
        </div>
      ) : error ? (
        <p className="rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
          Could not load today&apos;s note.
        </p>
      ) : sections.length === 0 ? (
        <p className="text-sm text-ink/70">No sections configured for this day.</p>
      ) : (
        <div className="space-y-4">
          {sections.map((section) => (
            <DailyNoteSection
              key={section.slug}
              section={section}
              onSave={handleSaveSection}
            />
          ))}
        </div>
      )}

      {/* Quick log input */}
      {!isLoading && !error ? (
        <div className="rounded-panel border border-ink/10 bg-white p-4">
          <p className="mb-2 text-sm font-medium text-ink/60">Quick log</p>
          <QuickLogInput
            onSubmit={handleQuickLog}
            isPending={createEntryMutation.isPending}
          />
          {createEntryMutation.isError ? (
            <p className="mt-2 text-sm text-rose-700">
              {createEntryMutation.error instanceof Error
                ? createEntryMutation.error.message
                : "Failed to add log entry."}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
