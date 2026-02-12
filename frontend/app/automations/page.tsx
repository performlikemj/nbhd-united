"use client";

import { FormEvent, useMemo, useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import { Automation } from "@/lib/types";
import {
  useAutomationRunsQuery,
  useAutomationsQuery,
  useCreateAutomationMutation,
  useDeleteAutomationMutation,
  usePauseAutomationMutation,
  useResumeAutomationMutation,
  useRunAutomationMutation,
  useUpdateAutomationMutation,
} from "@/lib/queries";

type FormState = {
  kind: "daily_brief" | "weekly_review";
  status: "active" | "paused";
  timezone: string;
  schedule_type: "daily" | "weekly";
  schedule_time: string;
  weekly_day: number;
};

const weekdayOptions = [
  { value: 0, label: "Mon" },
  { value: 1, label: "Tue" },
  { value: 2, label: "Wed" },
  { value: 3, label: "Thu" },
  { value: 4, label: "Fri" },
  { value: 5, label: "Sat" },
  { value: 6, label: "Sun" },
];

function defaultFormState(): FormState {
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  return {
    kind: "daily_brief",
    status: "active",
    timezone: tz,
    schedule_type: "daily",
    schedule_time: "09:00",
    weekly_day: 0,
  };
}

function toFormState(automation: Automation): FormState {
  return {
    kind: automation.kind,
    status: automation.status,
    timezone: automation.timezone,
    schedule_type: automation.schedule_type,
    schedule_time: automation.schedule_time.slice(0, 5),
    weekly_day: automation.schedule_days[0] ?? 0,
  };
}

function normalizePayload(form: FormState) {
  return {
    kind: form.kind,
    status: form.status,
    timezone: form.timezone.trim(),
    schedule_type: form.schedule_type,
    schedule_time: `${form.schedule_time}:00`,
    schedule_days: form.schedule_type === "weekly" ? [form.weekly_day] : [],
  };
}

function formatSchedule(automation: Automation): string {
  const timeValue = automation.schedule_time.slice(0, 5);
  if (automation.schedule_type === "daily") {
    return `Daily at ${timeValue}`;
  }
  const day = weekdayOptions.find((item) => item.value === automation.schedule_days[0])?.label ?? "Day";
  return `Weekly on ${day} at ${timeValue}`;
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return "Request failed.";
}

export default function AutomationsPage() {
  const { data: automations, isLoading, error } = useAutomationsQuery();
  const { data: runsData, isLoading: runsLoading } = useAutomationRunsQuery();
  const createMutation = useCreateAutomationMutation();
  const updateMutation = useUpdateAutomationMutation();
  const deleteMutation = useDeleteAutomationMutation();
  const pauseMutation = usePauseAutomationMutation();
  const resumeMutation = useResumeAutomationMutation();
  const runMutation = useRunAutomationMutation();

  const [createForm, setCreateForm] = useState<FormState>(defaultFormState());
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<FormState>(defaultFormState());

  const recentRuns = useMemo(() => (runsData?.results ?? []).slice(0, 10), [runsData?.results]);

  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await createMutation.mutateAsync(normalizePayload(createForm));
    setCreateForm(defaultFormState());
  };

  const handleStartEdit = (automation: Automation) => {
    setEditingId(automation.id);
    setEditForm(toFormState(automation));
  };

  const handleUpdate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editingId) {
      return;
    }
    await updateMutation.mutateAsync({
      id: editingId,
      data: normalizePayload(editForm),
    });
    setEditingId(null);
  };

  return (
    <div className="space-y-4">
      <SectionCard title="Automations" subtitle="Set proactive Daily Brief and Weekly Review jobs">
        <form className="grid gap-3 md:grid-cols-2" onSubmit={handleCreate}>
          <label className="text-sm text-ink/70">
            Kind
            <select
              className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
              value={createForm.kind}
              onChange={(event) =>
                setCreateForm((prev) => ({
                  ...prev,
                  kind: event.target.value as FormState["kind"],
                }))
              }
            >
              <option value="daily_brief">Daily Brief</option>
              <option value="weekly_review">Weekly Review</option>
            </select>
          </label>

          <label className="text-sm text-ink/70">
            Timezone
            <input
              className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
              value={createForm.timezone}
              onChange={(event) => setCreateForm((prev) => ({ ...prev, timezone: event.target.value }))}
            />
          </label>

          <label className="text-sm text-ink/70">
            Schedule Type
            <select
              className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
              value={createForm.schedule_type}
              onChange={(event) =>
                setCreateForm((prev) => ({
                  ...prev,
                  schedule_type: event.target.value as FormState["schedule_type"],
                }))
              }
            >
              <option value="daily">Daily</option>
              <option value="weekly">Weekly</option>
            </select>
          </label>

          <label className="text-sm text-ink/70">
            Time
            <input
              type="time"
              className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
              value={createForm.schedule_time}
              onChange={(event) => setCreateForm((prev) => ({ ...prev, schedule_time: event.target.value }))}
            />
          </label>

          {createForm.schedule_type === "weekly" ? (
            <label className="text-sm text-ink/70">
              Day
              <select
                className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                value={createForm.weekly_day}
                onChange={(event) =>
                  setCreateForm((prev) => ({
                    ...prev,
                    weekly_day: Number(event.target.value),
                  }))
                }
              >
                {weekdayOptions.map((day) => (
                  <option key={day.value} value={day.value}>
                    {day.label}
                  </option>
                ))}
              </select>
            </label>
          ) : null}

          <label className="text-sm text-ink/70">
            Status
            <select
              className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
              value={createForm.status}
              onChange={(event) =>
                setCreateForm((prev) => ({
                  ...prev,
                  status: event.target.value as FormState["status"],
                }))
              }
            >
              <option value="active">Active</option>
              <option value="paused">Paused</option>
            </select>
          </label>

          <div className="md:col-span-2">
            <button
              type="submit"
              disabled={createMutation.isPending}
              className="rounded-full border border-ink/20 px-4 py-2 text-sm hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-45"
            >
              {createMutation.isPending ? "Creating..." : "Create automation"}
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
        <SectionCard title="Configured Automations" subtitle="Manage schedules, status, and on-demand runs">
          {error ? (
            <p className="rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
              Could not load automations.
            </p>
          ) : automations && automations.length > 0 ? (
            <div className="space-y-3">
              {automations.map((automation) => (
                <article key={automation.id} className="rounded-panel border border-ink/15 bg-white p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <p className="text-base font-medium capitalize">
                        {automation.kind.replace("_", " ")}
                      </p>
                      <p className="text-sm text-ink/70">{formatSchedule(automation)}</p>
                    </div>
                    <StatusPill status={automation.status} />
                  </div>

                  <div className="mt-2 text-sm text-ink/70">
                    <p>Timezone: {automation.timezone}</p>
                    <p>Next run: {new Date(automation.next_run_at).toLocaleString()}</p>
                    <p>Last run: {automation.last_run_at ? new Date(automation.last_run_at).toLocaleString() : "Never"}</p>
                  </div>

                  <div className="mt-4 flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => runMutation.mutate(automation.id)}
                      disabled={runMutation.isPending}
                      className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-45"
                    >
                      Run now
                    </button>

                    {automation.status === "active" ? (
                      <button
                        type="button"
                        onClick={() => pauseMutation.mutate(automation.id)}
                        disabled={pauseMutation.isPending}
                        className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-45"
                      >
                        Pause
                      </button>
                    ) : (
                      <button
                        type="button"
                        onClick={() => resumeMutation.mutate(automation.id)}
                        disabled={resumeMutation.isPending}
                        className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-45"
                      >
                        Resume
                      </button>
                    )}

                    <button
                      type="button"
                      onClick={() => handleStartEdit(automation)}
                      className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40"
                    >
                      Edit
                    </button>

                    <button
                      type="button"
                      onClick={() => deleteMutation.mutate(automation.id)}
                      disabled={deleteMutation.isPending}
                      className="rounded-full border border-rose-300 px-3 py-1.5 text-sm text-rose-700 hover:border-rose-500 disabled:cursor-not-allowed disabled:opacity-45"
                    >
                      Delete
                    </button>
                  </div>

                  {editingId === automation.id ? (
                    <form className="mt-4 grid gap-3 rounded-panel border border-ink/10 p-3 md:grid-cols-2" onSubmit={handleUpdate}>
                      <label className="text-sm text-ink/70">
                        Kind
                        <select
                          className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                          value={editForm.kind}
                          onChange={(event) =>
                            setEditForm((prev) => ({
                              ...prev,
                              kind: event.target.value as FormState["kind"],
                            }))
                          }
                        >
                          <option value="daily_brief">Daily Brief</option>
                          <option value="weekly_review">Weekly Review</option>
                        </select>
                      </label>

                      <label className="text-sm text-ink/70">
                        Timezone
                        <input
                          className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                          value={editForm.timezone}
                          onChange={(event) => setEditForm((prev) => ({ ...prev, timezone: event.target.value }))}
                        />
                      </label>

                      <label className="text-sm text-ink/70">
                        Schedule Type
                        <select
                          className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                          value={editForm.schedule_type}
                          onChange={(event) =>
                            setEditForm((prev) => ({
                              ...prev,
                              schedule_type: event.target.value as FormState["schedule_type"],
                            }))
                          }
                        >
                          <option value="daily">Daily</option>
                          <option value="weekly">Weekly</option>
                        </select>
                      </label>

                      <label className="text-sm text-ink/70">
                        Time
                        <input
                          type="time"
                          className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                          value={editForm.schedule_time}
                          onChange={(event) =>
                            setEditForm((prev) => ({ ...prev, schedule_time: event.target.value }))
                          }
                        />
                      </label>

                      {editForm.schedule_type === "weekly" ? (
                        <label className="text-sm text-ink/70">
                          Day
                          <select
                            className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                            value={editForm.weekly_day}
                            onChange={(event) =>
                              setEditForm((prev) => ({
                                ...prev,
                                weekly_day: Number(event.target.value),
                              }))
                            }
                          >
                            {weekdayOptions.map((day) => (
                              <option key={day.value} value={day.value}>
                                {day.label}
                              </option>
                            ))}
                          </select>
                        </label>
                      ) : null}

                      <label className="text-sm text-ink/70">
                        Status
                        <select
                          className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                          value={editForm.status}
                          onChange={(event) =>
                            setEditForm((prev) => ({
                              ...prev,
                              status: event.target.value as FormState["status"],
                            }))
                          }
                        >
                          <option value="active">Active</option>
                          <option value="paused">Paused</option>
                        </select>
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
            <p className="text-sm text-ink/70">No automations configured yet.</p>
          )}
        </SectionCard>
      )}

      <SectionCard title="Recent Runs" subtitle="Latest execution outcomes">
        {runsLoading ? (
          <SectionCardSkeleton lines={3} />
        ) : recentRuns.length === 0 ? (
          <p className="text-sm text-ink/70">No runs yet.</p>
        ) : (
          <div className="space-y-2">
            {recentRuns.map((run) => (
              <div key={run.id} className="flex flex-wrap items-center justify-between gap-2 rounded-panel border border-ink/10 bg-white p-3">
                <div className="text-sm text-ink/70">
                  <p>Automation: {run.automation.slice(0, 8)}</p>
                  <p>Scheduled: {new Date(run.scheduled_for).toLocaleString()}</p>
                </div>
                <div className="flex items-center gap-2">
                  <StatusPill status={run.trigger_source} />
                  <StatusPill status={run.status} />
                </div>
              </div>
            ))}
          </div>
        )}
      </SectionCard>
    </div>
  );
}
