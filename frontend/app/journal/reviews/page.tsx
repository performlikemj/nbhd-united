"use client";

import { FormEvent, useState } from "react";

import { DynamicList } from "@/components/dynamic-list";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import {
  useCreateWeeklyReviewMutation,
  useDeleteWeeklyReviewMutation,
  useUpdateWeeklyReviewMutation,
  useWeeklyReviewsQuery,
} from "@/lib/queries";
import type { WeeklyReview, WeekRating } from "@/lib/types";

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return "Request failed.";
}

function mondayOfCurrentWeek(): string {
  const d = new Date();
  const day = d.getDay();
  const diff = d.getDate() - day + (day === 0 ? -6 : 1);
  const monday = new Date(d.setDate(diff));
  return monday.toISOString().slice(0, 10);
}

function addDays(date: string, days: number): string {
  const d = new Date(date + "T00:00:00");
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

function formatDateRange(start: string, end: string): string {
  const s = new Date(start + "T00:00:00");
  const e = new Date(end + "T00:00:00");
  return `${s.toLocaleDateString(undefined, { month: "short", day: "numeric" })} – ${e.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}`;
}

const ratingLabels: Record<WeekRating, string> = {
  "thumbs-up": "Good",
  "meh": "Meh",
  "thumbs-down": "Tough",
};

type FormState = {
  week_start: string;
  week_end: string;
  mood_summary: string;
  week_rating: WeekRating;
  top_wins: string[];
  top_challenges: string[];
  lessons: string[];
  intentions_next_week: string[];
};

function defaultFormState(): FormState {
  const start = mondayOfCurrentWeek();
  return {
    week_start: start,
    week_end: addDays(start, 6),
    mood_summary: "",
    week_rating: "meh",
    top_wins: [""],
    top_challenges: [""],
    lessons: [""],
    intentions_next_week: [""],
  };
}

function toEditFormState(review: WeeklyReview): FormState {
  return {
    week_start: review.week_start,
    week_end: review.week_end,
    mood_summary: review.mood_summary,
    week_rating: review.week_rating,
    top_wins: review.top_wins.length > 0 ? review.top_wins : [""],
    top_challenges: review.top_challenges.length > 0 ? review.top_challenges : [""],
    lessons: review.lessons.length > 0 ? review.lessons : [""],
    intentions_next_week: review.intentions_next_week.length > 0 ? review.intentions_next_week : [""],
  };
}

function normalizePayload(form: FormState) {
  return {
    week_start: form.week_start,
    week_end: form.week_end,
    mood_summary: form.mood_summary.trim(),
    week_rating: form.week_rating,
    top_wins: form.top_wins.map((s) => s.trim()).filter(Boolean),
    top_challenges: form.top_challenges.map((s) => s.trim()).filter(Boolean),
    lessons: form.lessons.map((s) => s.trim()).filter(Boolean),
    intentions_next_week: form.intentions_next_week.map((s) => s.trim()).filter(Boolean),
  };
}

function ReviewForm({
  form,
  setForm,
  showDates,
}: {
  form: FormState;
  setForm: React.Dispatch<React.SetStateAction<FormState>>;
  showDates: boolean;
}) {
  return (
    <>
      {showDates ? (
        <div className="grid gap-3 md:grid-cols-2">
          <label className="text-sm text-ink/70">
            Week start
            <input
              type="date"
              className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
              value={form.week_start}
              onChange={(e) =>
                setForm((prev) => ({
                  ...prev,
                  week_start: e.target.value,
                  week_end: addDays(e.target.value, 6),
                }))
              }
            />
          </label>
          <label className="text-sm text-ink/70">
            Week end
            <input
              type="date"
              className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
              value={form.week_end}
              onChange={(e) => setForm((prev) => ({ ...prev, week_end: e.target.value }))}
            />
          </label>
        </div>
      ) : null}

      <label className="block text-sm text-ink/70">
        Mood summary
        <input
          className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
          placeholder="How did this week feel overall?"
          value={form.mood_summary}
          onChange={(e) => setForm((prev) => ({ ...prev, mood_summary: e.target.value }))}
        />
      </label>

      <div>
        <p className="text-sm text-ink/70">Week rating</p>
        <div className="mt-1 flex gap-2">
          {(["thumbs-up", "meh", "thumbs-down"] as WeekRating[]).map((rating) => (
            <button
              key={rating}
              type="button"
              onClick={() => setForm((prev) => ({ ...prev, week_rating: rating }))}
              className={`rounded-full px-4 py-1.5 text-sm transition ${
                form.week_rating === rating
                  ? "bg-ink text-white"
                  : "border border-ink/20 text-ink/75 hover:border-ink/40"
              }`}
            >
              {ratingLabels[rating]}
            </button>
          ))}
        </div>
      </div>

      <DynamicList
        label="Top wins"
        items={form.top_wins}
        onChange={(top_wins) => setForm((prev) => ({ ...prev, top_wins }))}
      />
      <DynamicList
        label="Top challenges"
        items={form.top_challenges}
        onChange={(top_challenges) => setForm((prev) => ({ ...prev, top_challenges }))}
      />
      <DynamicList
        label="Lessons"
        items={form.lessons}
        onChange={(lessons) => setForm((prev) => ({ ...prev, lessons }))}
      />
      <DynamicList
        label="Intentions next week"
        items={form.intentions_next_week}
        onChange={(intentions_next_week) => setForm((prev) => ({ ...prev, intentions_next_week }))}
      />
    </>
  );
}

export default function WeeklyReviewsPage() {
  const { data: reviews, isLoading, error } = useWeeklyReviewsQuery();
  const createMutation = useCreateWeeklyReviewMutation();
  const updateMutation = useUpdateWeeklyReviewMutation();
  const deleteMutation = useDeleteWeeklyReviewMutation();

  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<FormState>(defaultFormState);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<FormState>(defaultFormState);

  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await createMutation.mutateAsync(normalizePayload(form));
    setForm(defaultFormState());
    setShowForm(false);
  };

  const handleStartEdit = (review: WeeklyReview) => {
    setEditingId(review.id);
    setEditForm(toEditFormState(review));
  };

  const handleUpdate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editingId) return;
    await updateMutation.mutateAsync({ id: editingId, data: normalizePayload(editForm) });
    setEditingId(null);
  };

  return (
    <div className="space-y-4">
      {/* Create form toggle */}
      {!showForm ? (
        <button
          type="button"
          onClick={() => setShowForm(true)}
          className="rounded-full bg-accent px-5 py-2 text-sm font-medium text-white transition hover:bg-accent/85"
        >
          New review
        </button>
      ) : (
        <SectionCard title="New Weekly Review" subtitle="Reflect on your week">
          <form className="space-y-4" onSubmit={handleCreate}>
            <ReviewForm form={form} setForm={setForm} showDates />

            <div className="flex gap-2">
              <button
                type="submit"
                disabled={createMutation.isPending || !form.mood_summary.trim()}
                className="rounded-full bg-accent px-5 py-2 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55"
              >
                {createMutation.isPending ? "Saving..." : "Save review"}
              </button>
              <button
                type="button"
                onClick={() => { setShowForm(false); setForm(defaultFormState()); }}
                className="rounded-full border border-ink/20 px-4 py-2 text-sm hover:border-ink/40"
              >
                Cancel
              </button>
            </div>
          </form>

          {createMutation.isError ? (
            <p className="mt-3 rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
              {getErrorMessage(createMutation.error)}
            </p>
          ) : null}
        </SectionCard>
      )}

      {/* Reviews list */}
      {isLoading ? (
        <SectionCardSkeleton lines={5} />
      ) : error ? (
        <SectionCard title="Past Reviews">
          <p className="rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
            Could not load reviews.
          </p>
        </SectionCard>
      ) : reviews && reviews.length > 0 ? (
        <SectionCard title="Past Reviews" subtitle={`${reviews.length} review${reviews.length === 1 ? "" : "s"}`}>
          <div className="space-y-3">
            {reviews.map((review) => (
              <article key={review.id} className="rounded-panel border border-ink/15 bg-white p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-base font-medium">
                    {formatDateRange(review.week_start, review.week_end)}
                  </p>
                  <StatusPill status={review.week_rating} />
                </div>
                <p className="mt-1 text-sm text-ink/70">{review.mood_summary}</p>

                {review.top_wins.length > 0 ? (
                  <div className="mt-2">
                    <p className="text-xs font-medium uppercase tracking-wide text-ink/50">Top wins</p>
                    <ul className="mt-1 list-disc pl-5 text-sm text-ink/80">
                      {review.top_wins.map((w, i) => <li key={i}>{w}</li>)}
                    </ul>
                  </div>
                ) : null}

                {review.top_challenges.length > 0 ? (
                  <div className="mt-2">
                    <p className="text-xs font-medium uppercase tracking-wide text-ink/50">Top challenges</p>
                    <ul className="mt-1 list-disc pl-5 text-sm text-ink/80">
                      {review.top_challenges.map((c, i) => <li key={i}>{c}</li>)}
                    </ul>
                  </div>
                ) : null}

                {review.lessons.length > 0 ? (
                  <div className="mt-2">
                    <p className="text-xs font-medium uppercase tracking-wide text-ink/50">Lessons</p>
                    <ul className="mt-1 list-disc pl-5 text-sm text-ink/80">
                      {review.lessons.map((l, i) => <li key={i}>{l}</li>)}
                    </ul>
                  </div>
                ) : null}

                {review.intentions_next_week.length > 0 ? (
                  <div className="mt-2">
                    <p className="text-xs font-medium uppercase tracking-wide text-ink/50">Intentions next week</p>
                    <ul className="mt-1 list-disc pl-5 text-sm text-ink/80">
                      {review.intentions_next_week.map((item, idx) => <li key={idx}>{item}</li>)}
                    </ul>
                  </div>
                ) : null}

                <div className="mt-4 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => handleStartEdit(review)}
                    className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40"
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    onClick={() => deleteMutation.mutate(review.id)}
                    disabled={deleteMutation.isPending}
                    className="rounded-full border border-rose-300 px-3 py-1.5 text-sm text-rose-700 hover:border-rose-500 disabled:cursor-not-allowed disabled:opacity-45"
                  >
                    Delete
                  </button>
                </div>

                {editingId === review.id ? (
                  <form
                    className="mt-4 space-y-4 rounded-panel border border-ink/10 p-4"
                    onSubmit={handleUpdate}
                  >
                    <ReviewForm form={editForm} setForm={setEditForm} showDates={false} />

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
                        onClick={() => setEditingId(null)}
                        className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40"
                      >
                        Cancel
                      </button>
                    </div>

                    {updateMutation.isError ? (
                      <p className="rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
                        {getErrorMessage(updateMutation.error)}
                      </p>
                    ) : null}
                  </form>
                ) : null}
              </article>
            ))}
          </div>
        </SectionCard>
      ) : (
        <SectionCard title="Past Reviews">
          <p className="text-sm text-ink/70">No weekly reviews yet.</p>
        </SectionCard>
      )}
    </div>
  );
}
