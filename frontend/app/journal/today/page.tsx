"use client";

import { FormEvent, useEffect, useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { useDailyNoteQuery, useUpdateDailyNoteTemplateMutation, useCreateDailyNoteEntryMutation, useDeleteDailyNoteEntryMutation, useUpdateDailyNoteEntryMutation } from "@/lib/queries";
import type { DailyNoteResponse, NoteTemplateSection } from "@/lib/types";

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

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return "Request failed.";
}

function cloneSections(sections: DailyNoteResponse["sections"]): NoteTemplateSection[] {
  return (sections ?? []).map((section) => ({
    slug: section.slug,
    title: section.title,
    content: section.content,
    source: section.source,
  }));
}

function coerceNumber(value: string): number | null {
  if (!value) return null;
  const parsed = Number.parseInt(value, 10);
  if (Number.isNaN(parsed)) return null;
  return parsed;
}

export default function TodayPage() {
  const [selectedDate, setSelectedDate] = useState(todayISO);
  const { data, isLoading, error } = useDailyNoteQuery(selectedDate);
  const createMutation = useCreateDailyNoteEntryMutation(selectedDate);
  const updateMutation = useUpdateDailyNoteEntryMutation(selectedDate);
  const deleteMutation = useDeleteDailyNoteEntryMutation(selectedDate);
  const updateTemplateMutation = useUpdateDailyNoteTemplateMutation(selectedDate);

  const [content, setContent] = useState("");
  const [mood, setMood] = useState("");
  const [energy, setEnergy] = useState("");
  const [showOptional, setShowOptional] = useState(false);

  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editContent, setEditContent] = useState("");
  const [editMood, setEditMood] = useState("");
  const [editEnergy, setEditEnergy] = useState("");
  const [sectionDrafts, setSectionDrafts] = useState<NoteTemplateSection[]>([]);

  useEffect(() => {
    setSectionDrafts(cloneSections(data?.sections));
  }, [data?.sections, data?.date]);

  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmed = content.trim();
    if (!trimmed) return;
    await createMutation.mutateAsync({
      content: trimmed,
      mood: mood.trim() || undefined,
      energy: coerceNumber(energy) ?? undefined,
    });
    setContent("");
    setMood("");
    setEnergy("");
    setShowOptional(false);
  };

  const handleUpdate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (editingIndex === null) return;
    await updateMutation.mutateAsync({
      index: editingIndex,
      data: {
        content: editContent.trim(),
        mood: editMood.trim() || undefined,
        energy: coerceNumber(editEnergy) ?? undefined,
      },
    });
    setEditingIndex(null);
  };

  const handleSaveSections = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!data) return;
    await updateTemplateMutation.mutateAsync({
      markdown: data.markdown,
      template_id: data.template_id ?? null,
      sections: sectionDrafts,
    });
  };

  const handleSectionChange = (index: number, value: string) => {
    setSectionDrafts((prev) =>
      prev.map((section, i) => (i === index ? { ...section, content: value } : section)),
    );
  };

  const handleSectionTitleChange = (index: number, value: string) => {
    setSectionDrafts((prev) =>
      prev.map((section, i) => (i === index ? { ...section, title: value } : section)),
    );
  };

  const entries = data?.entries ?? [];

  return (
    <div className="space-y-4">
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

      <SectionCard
        title="Template sections"
        subtitle={data?.template_name ? `Template: ${data.template_name}` : "Template content"}
      >
        {isLoading ? (
          <SectionCardSkeleton lines={6} />
        ) : error ? (
          <p className="rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
            Could not load today&apos;s sections.
          </p>
        ) : (
          <form className="space-y-4" onSubmit={handleSaveSections}>
            {sectionDrafts.length === 0 ? (
              <p className="text-sm text-ink/70">No template sections configured for this day.</p>
            ) : (
              sectionDrafts.map((section, index) => (
                <div key={section.slug} className="space-y-2 rounded-panel border border-ink/12 bg-white p-4">
                  <label className="text-sm text-ink/70">
                    Section title
                    <input
                      className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                      value={section.title}
                      onChange={(event) => handleSectionTitleChange(index, event.target.value)}
                    />
                  </label>
                  <textarea
                    className="w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                    rows={4}
                    placeholder="Section content in markdown..."
                    value={section.content}
                    onChange={(event) => handleSectionChange(index, event.target.value)}
                  />
                  <p className="text-xs text-ink/50">Slug: {section.slug}</p>
                </div>
              ))
            )}
            <div>
              <button
                type="submit"
                disabled={updateTemplateMutation.isPending || sectionDrafts.length === 0}
                className="rounded-full bg-accent px-5 py-2 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55"
              >
                {updateTemplateMutation.isPending ? "Saving..." : "Save template sections"}
              </button>
            </div>
            {updateTemplateMutation.isError ? (
              <p className="rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
                {getErrorMessage(updateTemplateMutation.error)}
              </p>
            ) : null}
          </form>
        )}
      </SectionCard>

      <SectionCard title="Add entry" subtitle="Append a new log entry">
        <form className="space-y-3" onSubmit={handleCreate}>
          <textarea
            className="w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
            rows={3}
            placeholder="Write something..."
            value={content}
            onChange={(event) => setContent(event.target.value)}
          />

          {showOptional ? (
            <div className="grid gap-3 md:grid-cols-2">
              <label className="text-sm text-ink/70">
                Mood
                <input
                  className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                  placeholder="e.g. focused, calm, anxious"
                  value={mood}
                  onChange={(event) => setMood(event.target.value)}
                />
              </label>
              <label className="text-sm text-ink/70">
                Energy (1-10)
                <input
                  type="number"
                  min={1}
                  max={10}
                  className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                  placeholder="e.g. 7"
                  value={energy}
                  onChange={(event) => setEnergy(event.target.value)}
                />
              </label>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setShowOptional(true)}
              className="text-sm text-ink/50 hover:text-ink/70"
            >
              + Add mood &amp; energy
            </button>
          )}

          <div>
            <button
              type="submit"
              disabled={createMutation.isPending || !content.trim()}
              className="rounded-full bg-accent px-5 py-2 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55"
            >
              {createMutation.isPending ? "Saving..." : "Add entry"}
            </button>
          </div>
        </form>

        {createMutation.isError ? (
          <p className="mt-3 rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
            {getErrorMessage(createMutation.error)}
          </p>
        ) : null}
      </SectionCard>

      {isLoading ? (
        <SectionCardSkeleton lines={5} />
      ) : error ? (
        <SectionCard title="Entries">
          <p className="rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
            Could not load daily notes.
          </p>
        </SectionCard>
      ) : (
        <SectionCard title="Entries" subtitle={`${entries.length} entr${entries.length === 1 ? "y" : "ies"} for this day`}>
          <div className="space-y-3">
            {entries.map((entry, index) => (
              <article key={`${entry.time}-${index}`} className="rounded-panel border border-ink/15 bg-white p-4">
                <div className="flex flex-wrap items-center gap-2">
                  {entry.time ? (
                    <span className="font-mono text-xs text-ink/50">{entry.time}</span>
                  ) : null}
                  <span
                    className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${
                      entry.author === "human"
                        ? "bg-accent/10 text-accent"
                        : "bg-signal/15 text-signal"
                    }`}
                  >
                    {entry.author === "human" ? "You" : "Agent"}
                  </span>
                  {entry.mood ? (
                    <span className="text-xs text-ink/50">Mood: {entry.mood}</span>
                  ) : null}
                  {entry.energy !== null ? (
                    <span className="text-xs text-ink/50">Energy: {entry.energy}</span>
                  ) : null}
                </div>
                {entry.content ? <p className="mt-2 whitespace-pre-wrap text-sm text-ink/80">{entry.content}</p> : null}

                {entry.subsections ? (
                  <div className="mt-2 space-y-2">
                    {Object.entries(entry.subsections).map(([slug, body]) => (
                      <div key={slug}>
                        <p className="text-xs font-medium uppercase tracking-wide text-ink/50">
                          {slug.replace(/-/g, " ")}
                        </p>
                        <p className="mt-0.5 whitespace-pre-wrap text-sm text-ink/80">{body}</p>
                      </div>
                    ))}
                  </div>
                ) : null}

                {entry.author === "human" ? (
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => {
                        setEditingIndex(index);
                        setEditContent(entry.content);
                        setEditMood(entry.mood ?? "");
                        setEditEnergy(entry.energy !== null ? String(entry.energy) : "");
                      }}
                      className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40"
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() => deleteMutation.mutate(index)}
                      disabled={deleteMutation.isPending}
                      className="rounded-full border border-rose-300 px-3 py-1.5 text-sm text-rose-700 hover:border-rose-500 disabled:cursor-not-allowed disabled:opacity-45"
                    >
                      Delete
                    </button>
                  </div>
                ) : null}

                {editingIndex === index ? (
                  <form className="mt-4 space-y-3 rounded-panel border border-ink/10 p-3" onSubmit={handleUpdate}>
                    <textarea
                      className="w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                      rows={3}
                      value={editContent}
                      onChange={(event) => setEditContent(event.target.value)}
                    />
                    <div className="grid gap-3 md:grid-cols-2">
                      <label className="text-sm text-ink/70">
                        Mood
                        <input
                          className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                          value={editMood}
                          onChange={(event) => setEditMood(event.target.value)}
                        />
                      </label>
                      <label className="text-sm text-ink/70">
                        Energy (1-10)
                        <input
                          type="number"
                          min={1}
                          max={10}
                          className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                          value={editEnergy}
                          onChange={(event) => setEditEnergy(event.target.value)}
                        />
                      </label>
                    </div>
                    <div className="flex gap-2">
                      <button
                        type="submit"
                        disabled={updateMutation.isPending}
                        className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-45"
                      >
                        {updateMutation.isPending ? "Saving..." : "Save"}
                      </button>
                      <button
                        type="button"
                        onClick={() => setEditingIndex(null)}
                        className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40"
                      >
                        Cancel
                      </button>
                    </div>
                  </form>
                ) : null}
              </article>
            ))}
          </div>
        </SectionCard>
      )}
    </div>
  );
}
