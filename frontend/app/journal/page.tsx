"use client";

import { FormEvent, useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import { JournalEntry, JournalEntryEnergy } from "@/lib/types";
import {
  useCreateJournalEntryMutation,
  useDeleteJournalEntryMutation,
  useJournalEntriesQuery,
  useUpdateJournalEntryMutation,
} from "@/lib/queries";

type FormState = {
  date: string;
  mood: string;
  energy: JournalEntryEnergy;
  wins: string[];
  challenges: string[];
  reflection: string;
};

function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

function defaultFormState(): FormState {
  return {
    date: todayISO(),
    mood: "",
    energy: "medium",
    wins: [""],
    challenges: [""],
    reflection: "",
  };
}

function toFormState(entry: JournalEntry): FormState {
  return {
    date: entry.date,
    mood: entry.mood,
    energy: entry.energy,
    wins: entry.wins.length > 0 ? entry.wins : [""],
    challenges: entry.challenges.length > 0 ? entry.challenges : [""],
    reflection: entry.reflection,
  };
}

function normalizePayload(form: FormState) {
  return {
    date: form.date,
    mood: form.mood.trim(),
    energy: form.energy,
    wins: form.wins.map((w) => w.trim()).filter(Boolean),
    challenges: form.challenges.map((c) => c.trim()).filter(Boolean),
    reflection: form.reflection.trim(),
  };
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return "Request failed.";
}

const MAX_LIST_ITEMS = 10;

function DynamicList({
  label,
  items,
  onChange,
}: {
  label: string;
  items: string[];
  onChange: (items: string[]) => void;
}) {
  return (
    <div>
      <p className="text-sm text-ink/70">{label}</p>
      <div className="mt-1 space-y-2">
        {items.map((item, index) => (
          <div key={index} className="flex gap-2">
            <input
              className="flex-1 rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
              value={item}
              placeholder={`${label.slice(0, -1)}...`}
              onChange={(e) => {
                const next = [...items];
                next[index] = e.target.value;
                onChange(next);
              }}
            />
            {items.length > 1 ? (
              <button
                type="button"
                onClick={() => onChange(items.filter((_, i) => i !== index))}
                className="rounded-full border border-rose-300 px-3 py-1.5 text-sm text-rose-700 hover:border-rose-500"
              >
                Remove
              </button>
            ) : null}
          </div>
        ))}
      </div>
      {items.length < MAX_LIST_ITEMS ? (
        <button
          type="button"
          onClick={() => onChange([...items, ""])}
          className="mt-2 rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40"
        >
          Add {label.slice(0, -1).toLowerCase()}
        </button>
      ) : null}
    </div>
  );
}

export default function JournalPage() {
  const { data: entries, isLoading, error } = useJournalEntriesQuery();
  const createMutation = useCreateJournalEntryMutation();
  const updateMutation = useUpdateJournalEntryMutation();
  const deleteMutation = useDeleteJournalEntryMutation();

  const [createForm, setCreateForm] = useState<FormState>(defaultFormState());
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<FormState>(defaultFormState());

  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await createMutation.mutateAsync(normalizePayload(createForm));
    setCreateForm(defaultFormState());
  };

  const handleStartEdit = (entry: JournalEntry) => {
    setEditingId(entry.id);
    setEditForm(toFormState(entry));
  };

  const handleUpdate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editingId) return;
    await updateMutation.mutateAsync({
      id: editingId,
      data: normalizePayload(editForm),
    });
    setEditingId(null);
  };

  return (
    <div className="space-y-4">
      <SectionCard title="New Journal Entry" subtitle="Record your day">
        <form className="grid gap-3 md:grid-cols-2" onSubmit={handleCreate}>
          <label className="text-sm text-ink/70">
            Date
            <input
              type="date"
              className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
              value={createForm.date}
              onChange={(e) => setCreateForm((prev) => ({ ...prev, date: e.target.value }))}
            />
          </label>

          <label className="text-sm text-ink/70">
            Mood
            <input
              className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
              placeholder="e.g. focused, calm, anxious"
              value={createForm.mood}
              onChange={(e) => setCreateForm((prev) => ({ ...prev, mood: e.target.value }))}
            />
          </label>

          <label className="text-sm text-ink/70">
            Energy
            <select
              className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
              value={createForm.energy}
              onChange={(e) =>
                setCreateForm((prev) => ({
                  ...prev,
                  energy: e.target.value as JournalEntryEnergy,
                }))
              }
            >
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
          </label>

          <div className="md:col-span-2">
            <DynamicList
              label="Wins"
              items={createForm.wins}
              onChange={(wins) => setCreateForm((prev) => ({ ...prev, wins }))}
            />
          </div>

          <div className="md:col-span-2">
            <DynamicList
              label="Challenges"
              items={createForm.challenges}
              onChange={(challenges) => setCreateForm((prev) => ({ ...prev, challenges }))}
            />
          </div>

          <label className="text-sm text-ink/70 md:col-span-2">
            Reflection
            <textarea
              className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
              rows={3}
              placeholder="How was your day? What stands out?"
              value={createForm.reflection}
              onChange={(e) => setCreateForm((prev) => ({ ...prev, reflection: e.target.value }))}
            />
          </label>

          <div className="md:col-span-2">
            <button
              type="submit"
              disabled={createMutation.isPending}
              className="rounded-full border border-ink/20 px-4 py-2 text-sm hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-45"
            >
              {createMutation.isPending ? "Saving..." : "Save entry"}
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
      ) : (
        <SectionCard title="Journal Entries" subtitle="Your reflections, newest first">
          {error ? (
            <p className="rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
              Could not load journal entries.
            </p>
          ) : entries && entries.length > 0 ? (
            <div className="space-y-3">
              {entries.map((entry) => (
                <article key={entry.id} className="rounded-panel border border-ink/15 bg-white p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <p className="text-base font-medium">
                        {new Date(entry.date + "T00:00:00").toLocaleDateString(undefined, {
                          weekday: "short",
                          year: "numeric",
                          month: "short",
                          day: "numeric",
                        })}
                      </p>
                      <p className="text-sm text-ink/70 capitalize">{entry.mood}</p>
                    </div>
                    <StatusPill status={entry.energy} />
                  </div>

                  {entry.wins.length > 0 ? (
                    <div className="mt-2">
                      <p className="text-xs font-medium uppercase tracking-wide text-ink/50">Wins</p>
                      <ul className="mt-1 list-disc pl-5 text-sm text-ink/80">
                        {entry.wins.map((win, i) => (
                          <li key={i}>{win}</li>
                        ))}
                      </ul>
                    </div>
                  ) : null}

                  {entry.challenges.length > 0 ? (
                    <div className="mt-2">
                      <p className="text-xs font-medium uppercase tracking-wide text-ink/50">Challenges</p>
                      <ul className="mt-1 list-disc pl-5 text-sm text-ink/80">
                        {entry.challenges.map((challenge, i) => (
                          <li key={i}>{challenge}</li>
                        ))}
                      </ul>
                    </div>
                  ) : null}

                  {entry.reflection ? (
                    <div className="mt-2">
                      <p className="text-xs font-medium uppercase tracking-wide text-ink/50">Reflection</p>
                      <p className="mt-1 text-sm text-ink/80">{entry.reflection}</p>
                    </div>
                  ) : null}

                  <div className="mt-4 flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => handleStartEdit(entry)}
                      className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40"
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() => deleteMutation.mutate(entry.id)}
                      disabled={deleteMutation.isPending}
                      className="rounded-full border border-rose-300 px-3 py-1.5 text-sm text-rose-700 hover:border-rose-500 disabled:cursor-not-allowed disabled:opacity-45"
                    >
                      Delete
                    </button>
                  </div>

                  {editingId === entry.id ? (
                    <form
                      className="mt-4 grid gap-3 rounded-panel border border-ink/10 p-3 md:grid-cols-2"
                      onSubmit={handleUpdate}
                    >
                      <label className="text-sm text-ink/70">
                        Date
                        <input
                          type="date"
                          className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                          value={editForm.date}
                          onChange={(e) => setEditForm((prev) => ({ ...prev, date: e.target.value }))}
                        />
                      </label>

                      <label className="text-sm text-ink/70">
                        Mood
                        <input
                          className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                          value={editForm.mood}
                          onChange={(e) => setEditForm((prev) => ({ ...prev, mood: e.target.value }))}
                        />
                      </label>

                      <label className="text-sm text-ink/70">
                        Energy
                        <select
                          className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                          value={editForm.energy}
                          onChange={(e) =>
                            setEditForm((prev) => ({
                              ...prev,
                              energy: e.target.value as JournalEntryEnergy,
                            }))
                          }
                        >
                          <option value="low">Low</option>
                          <option value="medium">Medium</option>
                          <option value="high">High</option>
                        </select>
                      </label>

                      <div className="md:col-span-2">
                        <DynamicList
                          label="Wins"
                          items={editForm.wins}
                          onChange={(wins) => setEditForm((prev) => ({ ...prev, wins }))}
                        />
                      </div>

                      <div className="md:col-span-2">
                        <DynamicList
                          label="Challenges"
                          items={editForm.challenges}
                          onChange={(challenges) => setEditForm((prev) => ({ ...prev, challenges }))}
                        />
                      </div>

                      <label className="text-sm text-ink/70 md:col-span-2">
                        Reflection
                        <textarea
                          className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                          rows={3}
                          value={editForm.reflection}
                          onChange={(e) =>
                            setEditForm((prev) => ({ ...prev, reflection: e.target.value }))
                          }
                        />
                      </label>

                      <div className="flex gap-2 md:col-span-2">
                        <button
                          type="submit"
                          disabled={updateMutation.isPending}
                          className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-45"
                        >
                          {updateMutation.isPending ? "Saving..." : "Save"}
                        </button>
                        <button
                          type="button"
                          onClick={() => setEditingId(null)}
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
          ) : (
            <p className="text-sm text-ink/70">No journal entries yet. Create your first entry above.</p>
          )}
        </SectionCard>
      )}
    </div>
  );
}
