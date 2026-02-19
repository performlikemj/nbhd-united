"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

import ScheduleBuilder, { cronToHuman } from "@/components/schedule-builder";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import TimezoneSelector from "@/components/timezone-selector";
import { CronJob } from "@/lib/types";
import {
  useCronJobsQuery,
  useCreateCronJobMutation,
  useDeleteCronJobMutation,
  useMeQuery,
  useToggleCronJobMutation,
  useUpdateCronJobMutation,
} from "@/lib/queries";

/* ------------------------------------------------------------------ */
/*  Task templates                                                     */
/* ------------------------------------------------------------------ */

interface TaskTemplate {
  icon: string;
  name: string;
  message: string;
  expr: string;
}

const TASK_TEMPLATES: TaskTemplate[] = [
  {
    icon: "☀️",
    name: "Morning Briefing",
    message:
      "Check my calendar, weather, and any important emails. Give me a quick summary to start my day.",
    expr: "0 7 * * *",
  },
  {
    icon: "📰",
    name: "News Digest",
    message:
      "Search for the latest news in tech, AI, and my areas of interest. Summarize the top 5 stories.",
    expr: "0 12 * * *",
  },
  {
    icon: "🌤️",
    name: "Weather Report",
    message:
      "Check the weather forecast for today and tomorrow. Let me know if I should bring an umbrella or dress warm.",
    expr: "0 6 * * *",
  },
  {
    icon: "📅",
    name: "Daily Schedule",
    message:
      "Review my calendar for today and remind me of upcoming events, deadlines, or meetings.",
    expr: "0 8 * * 1-5",
  },
  {
    icon: "🌙",
    name: "Evening Recap",
    message:
      "Summarize what happened today — any messages I missed, tasks completed, and what's coming up tomorrow.",
    expr: "0 21 * * *",
  },
  {
    icon: "💪",
    name: "Weekly Review",
    message:
      "Give me a weekly review: what got done this week, what's pending, and priorities for next week.",
    expr: "0 10 * * 1",
  },
];

/* ------------------------------------------------------------------ */
/*  Agent capabilities reference                                       */
/* ------------------------------------------------------------------ */

const AGENT_CAPABILITIES = [
  { icon: "🌐", name: "Web Search", desc: "Search the internet for current information" },
  { icon: "🌤️", name: "Weather", desc: "Check weather forecasts for any location" },
  { icon: "📅", name: "Calendar", desc: "Check your upcoming events and schedule" },
  { icon: "📧", name: "Email", desc: "Check for new or important emails" },
  { icon: "📰", name: "News", desc: "Find and summarize news articles" },
  { icon: "💬", name: "Message", desc: "Send you updates via Telegram" },
  { icon: "🧠", name: "Memory", desc: "Access your notes, preferences, and past conversations" },
  { icon: "📝", name: "Journal", desc: "Read and write to your journal entries" },
];

/* ------------------------------------------------------------------ */
/*  Form types                                                         */
/* ------------------------------------------------------------------ */

type CreateFormState = {
  name: string;
  expr: string;
  tz: string;
  message: string;
  deliveryMode: string;
  deliveryChannel: string;
};

function defaultCreateForm(tz?: string): CreateFormState {
  const profileTz =
    tz || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  return {
    name: "",
    expr: "0 9 * * *",
    tz: profileTz,
    message: "",
    deliveryMode: "announce",
    deliveryChannel: "telegram",
  };
}

type EditFormState = {
  expr: string;
  tz: string;
  message: string;
  deliveryMode: string;
  deliveryChannel: string;
};

function toEditForm(job: CronJob): EditFormState {
  return {
    expr: job.schedule.expr,
    tz: job.schedule.tz || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    message: job.payload.message,
    deliveryMode: job.delivery.mode,
    deliveryChannel: job.delivery.channel ?? "",
  };
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return "Request failed.";
}

type SaveButtonStatus = "idle" | "saving" | "success" | "error";

type ActionFeedback = {
  status: SaveButtonStatus;
  text: string;
};

const SAVE_CLEAR_DELAY_MS = 2000;
const ERROR_CLEAR_DELAY_MS = 4000;

function getSaveButtonClass(status: SaveButtonStatus): string {
  const base =
    "rounded-full border border-border-strong px-4 py-2 text-sm transition-all duration-200 disabled:cursor-not-allowed disabled:opacity-45";

  if (status === "success") {
    return `${base} border-emerald-500 bg-emerald-500 text-white hover:bg-emerald-600`;
  }

  if (status === "error") {
    return `${base} border-rose-500 bg-rose-500 text-white hover:bg-rose-600`;
  }

  return `${base} hover:border-border-strong`;
}

/* ------------------------------------------------------------------ */
/*  Page component                                                     */
/* ------------------------------------------------------------------ */

export default function SettingsCronJobsPage() {
  const { data: me } = useMeQuery();
  const { data: cronJobs, isLoading, error } = useCronJobsQuery();
  const createMutation = useCreateCronJobMutation();
  const deleteMutation = useDeleteCronJobMutation();
  const toggleMutation = useToggleCronJobMutation();
  const updateMutation = useUpdateCronJobMutation();

  const [showCreate, setShowCreate] = useState(false);
  const [createForm, setCreateForm] = useState<CreateFormState>(() => defaultCreateForm(me?.timezone));
  const [editingName, setEditingName] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<EditFormState>({
    expr: "",
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    message: "",
    deliveryMode: "announce",
    deliveryChannel: "",
  });
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [showCapabilities, setShowCapabilities] = useState(false);
  const [createFeedback, setCreateFeedback] = useState<ActionFeedback>({ status: "idle", text: "" });
  const [editFeedback, setEditFeedback] = useState<ActionFeedback>({ status: "idle", text: "" });
  const [deleteFeedback, setDeleteFeedback] = useState<ActionFeedback>({ status: "idle", text: "" });
  const createTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const editTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const deleteTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearCreateTimer = () => {
    if (createTimeoutRef.current) {
      clearTimeout(createTimeoutRef.current);
      createTimeoutRef.current = null;
    }
  };

  const clearEditTimer = () => {
    if (editTimeoutRef.current) {
      clearTimeout(editTimeoutRef.current);
      editTimeoutRef.current = null;
    }
  };

  const clearDeleteTimer = () => {
    if (deleteTimeoutRef.current) {
      clearTimeout(deleteTimeoutRef.current);
      deleteTimeoutRef.current = null;
    }
  };

  useEffect(() => {
    return () => {
      clearCreateTimer();
      clearEditTimer();
      clearDeleteTimer();
    };
  }, []);

  const setSuccess = (
    setState: (state: ActionFeedback) => void,
    timeoutRef: { current: ReturnType<typeof setTimeout> | null },
  ) => {
    setState({ status: "success", text: "" });
    timeoutRef.current = setTimeout(() => {
      setState({ status: "idle", text: "" });
    }, SAVE_CLEAR_DELAY_MS);
  };

  const setError = (
    setState: (state: ActionFeedback) => void,
    timeoutRef: { current: ReturnType<typeof setTimeout> | null },
    message: string,
  ) => {
    setState({ status: "error", text: message });
    timeoutRef.current = setTimeout(() => {
      setState({ status: "idle", text: "" });
    }, ERROR_CLEAR_DELAY_MS);
  };

  const handleApplyTemplate = (template: TaskTemplate) => {
    setCreateForm((prev) => ({
      ...prev,
      name: template.name,
      message: template.message,
      expr: template.expr,
    }));
  };

  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    clearCreateTimer();
    setCreateFeedback({ status: "saving", text: "" });

    try {
      await createMutation.mutateAsync({
        name: createForm.name.trim(),
        schedule: { kind: "cron", expr: createForm.expr.trim(), tz: createForm.tz.trim() },
        sessionTarget: "isolated",
        payload: { kind: "agentTurn", message: createForm.message.trim() },
        delivery: {
          mode: createForm.deliveryMode,
          ...(createForm.deliveryChannel ? { channel: createForm.deliveryChannel } : {}),
        },
        enabled: true,
      });

      setCreateForm(defaultCreateForm(me?.timezone));
      setShowCreate(false);
      setSuccess(setCreateFeedback, createTimeoutRef);
    } catch (err) {
      clearCreateTimer();
      setError(setCreateFeedback, createTimeoutRef, getErrorMessage(err));
    }
  };

  const handleStartEdit = (job: CronJob) => {
    clearEditTimer();
    setEditFeedback({ status: "idle", text: "" });
    setEditingName(job.jobId ?? job.name);
    setEditForm(toEditForm(job));
  };

  const handleUpdate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editingName) return;
    clearEditTimer();
    setEditFeedback({ status: "saving", text: "" });

    try {
      await updateMutation.mutateAsync({
        name: editingName,
        data: {
          schedule: { kind: "cron", expr: editForm.expr.trim(), tz: editForm.tz.trim() },
          payload: { kind: "agentTurn", message: editForm.message.trim() },
          delivery: {
            mode: editForm.deliveryMode,
            ...(editForm.deliveryChannel ? { channel: editForm.deliveryChannel } : {}),
          },
        },
      });
      setEditingName(null);
      setSuccess(setEditFeedback, editTimeoutRef);
    } catch (err) {
      clearEditTimer();
      setError(setEditFeedback, editTimeoutRef, getErrorMessage(err));
    }
  };

  const handleDelete = async (nameOrId: string) => {
    clearDeleteTimer();
    setDeleteFeedback({ status: "saving", text: "" });

    try {
      await deleteMutation.mutateAsync({ name: nameOrId });
      setConfirmDelete(null);
      setSuccess(setDeleteFeedback, deleteTimeoutRef);
    } catch (err) {
      clearDeleteTimer();
      setError(setDeleteFeedback, deleteTimeoutRef, getErrorMessage(err));
    }
  };

  return (
    <div className="space-y-4">
      <SectionCard
        title="Scheduled Tasks"
        subtitle="Set up recurring tasks for your AI assistant — morning briefings, news digests, reminders, and more"
      >
        {!showCreate ? (
          <button
            type="button"
            onClick={() => {
              clearCreateTimer();
              setCreateFeedback({ status: "idle", text: "" });
              setCreateForm(defaultCreateForm(me?.timezone));
              setShowCreate(true);
            }}
            className="rounded-full border border-border-strong px-4 py-2 text-sm hover:border-border-strong"
          >
            Add scheduled task
          </button>
        ) : (
          <div className="space-y-4">
            {/* Quick-start templates */}
            <div>
              <p className="mb-2 text-sm font-medium text-ink-muted">Quick start — pick a template</p>
              <div className="flex flex-wrap gap-2">
                {TASK_TEMPLATES.map((tpl) => (
                  <button
                    key={tpl.name}
                    type="button"
                    onClick={() => handleApplyTemplate(tpl)}
                    className={`rounded-full border px-3 py-1.5 text-sm transition ${
                      createForm.name === tpl.name
                        ? "border-accent bg-accent/10 text-accent"
                        : "border-border text-ink-muted hover:border-border-strong hover:text-ink"
                    }`}
                  >
                    {tpl.icon} {tpl.name}
                  </button>
                ))}
              </div>
            </div>

            {/* Create form */}
            <form className="grid gap-3 md:grid-cols-2" onSubmit={handleCreate}>
              <label className="text-sm text-ink-muted">
                Name
                <input
                  className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm"
                  placeholder="e.g. Morning Briefing"
                  value={createForm.name}
                  onChange={(e) => setCreateForm((prev) => ({ ...prev, name: e.target.value }))}
                  required
                />
              </label>

              <TimezoneSelector
                value={createForm.tz}
                onChange={(tz) => setCreateForm((prev) => ({ ...prev, tz }))}
                defaultTimezone={me?.timezone}
              />

              <ScheduleBuilder
                expr={createForm.expr}
                onChange={(expr) => setCreateForm((prev) => ({ ...prev, expr }))}
              />

              <label className="text-sm text-ink-muted">
                Delivery
                <select
                  className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm"
                  value={createForm.deliveryMode}
                  onChange={(e) => setCreateForm((prev) => ({ ...prev, deliveryMode: e.target.value }))}
                >
                  <option value="announce">Announce (send message)</option>
                  <option value="none">Silent (no message)</option>
                </select>
              </label>

              <label className="text-sm text-ink-muted md:col-span-2">
                What should your agent do?
                <textarea
                  className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm"
                  rows={3}
                  placeholder="Describe the task in plain language — e.g. 'Check my calendar and weather, then send me a morning summary'"
                  value={createForm.message}
                  onChange={(e) => setCreateForm((prev) => ({ ...prev, message: e.target.value }))}
                  required
                />
              </label>

              {/* Capabilities reference */}
              <div className="md:col-span-2">
                <button
                  type="button"
                  onClick={() => setShowCapabilities(!showCapabilities)}
                  className="text-xs text-accent hover:underline"
                >
                  {showCapabilities ? "Hide" : "Show"} what your agent can do ↗
                </button>
                {showCapabilities && (
                  <div className="mt-2 grid gap-1.5 rounded-panel border border-border bg-surface-hover p-3 sm:grid-cols-2">
                    {AGENT_CAPABILITIES.map((cap) => (
                      <div key={cap.name} className="flex items-start gap-2 text-sm">
                        <span className="text-base leading-5">{cap.icon}</span>
                        <div>
                          <span className="font-medium text-ink-muted">{cap.name}</span>
                          <span className="text-ink-faint"> — {cap.desc}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="flex flex-col gap-2 md:col-span-2">
                <button
                  type="submit"
                  disabled={createMutation.isPending}
                  className={getSaveButtonClass(createFeedback.status)}
                >
                  {createMutation.isPending
                    ? "Saving..."
                    : createFeedback.status === "success"
                      ? "✓ Saved"
                      : "Save"}
                </button>
                {createFeedback.status === "error" ? (
                  <p className="text-xs text-rose-500">{createFeedback.text}</p>
                ) : null}
                <button
                  type="button"
                  onClick={() => {
                    clearCreateTimer();
                    setCreateFeedback({ status: "idle", text: "" });
                    setShowCreate(false);
                  }}
                  className="rounded-full border border-border-strong px-4 py-2 text-sm hover:border-border-strong"
                >
                  Cancel
                </button>
              </div>
            </form>
          </div>
        )}
      </SectionCard>

      {isLoading ? (
        <SectionCardSkeleton lines={5} />
      ) : error ? (
        <SectionCard title="Your Tasks">
          <p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">
            Could not load scheduled tasks.
            {error instanceof Error ? ` ${error.message}` : ""}
          </p>
          <p className="mt-2 text-xs text-ink-faint">
            If this persists, run: python manage.py check_gateway_health
          </p>
        </SectionCard>
      ) : cronJobs && cronJobs.length > 0 ? (
        <SectionCard title="Your Tasks" subtitle="Toggle, edit, or remove scheduled tasks">
          <div className="space-y-3">
            {cronJobs.map((job) => {
              const jobIdentifier = job.jobId ?? job.name;
              return (
              <article
                key={job.name}
                className="rounded-panel border border-border bg-surface-elevated p-4"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p className="text-base font-medium break-words">{job.name}</p>
                    <p className="text-sm text-ink-muted break-words">
                      {cronToHuman(job.schedule?.expr ?? "", job.schedule?.tz ?? "UTC")}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <StatusPill status={job.enabled ? "active" : "paused"} />
                    {job.delivery?.mode !== "none" && job.delivery?.channel ? (
                      <span className="rounded-full bg-surface-hover px-2.5 py-0.5 text-xs text-ink-muted">
                        {job.delivery.channel}
                      </span>
                    ) : null}
                  </div>
                </div>

                <p className="mt-2 text-sm text-ink-muted line-clamp-2">
                  {(job.payload?.message ?? "").slice(0, 200)}
                </p>

                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() =>
                      toggleMutation.mutate({ name: jobIdentifier, enabled: !job.enabled })
                    }
                    disabled={toggleMutation.isPending}
                    className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-45"
                  >
                    {job.enabled ? "Disable" : "Enable"}
                  </button>

                  <button
                    type="button"
                    onClick={() => handleStartEdit(job)}
                    className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong"
                  >
                    Edit
                  </button>

                  {confirmDelete === jobIdentifier ? (
                    <div className="flex flex-col gap-1.5">
                      <div className="flex gap-2">
                        <button
                          type="button"
                          onClick={() => handleDelete(jobIdentifier)}
                          disabled={deleteMutation.isPending}
                          className={getSaveButtonClass(deleteFeedback.status)}
                        >
                          {deleteMutation.isPending
                            ? "Saving..."
                            : deleteFeedback.status === "success"
                              ? "✓ Saved"
                              : "Save"}
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            clearDeleteTimer();
                            setDeleteFeedback({ status: "idle", text: "" });
                            setConfirmDelete(null);
                          }}
                          className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong"
                        >
                          Cancel
                        </button>
                      </div>
                      {deleteFeedback.status === "error" && confirmDelete === jobIdentifier ? (
                        <p className="text-xs text-rose-500">{deleteFeedback.text}</p>
                      ) : null}
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={() => {
                        clearDeleteTimer();
                        setDeleteFeedback({ status: "idle", text: "" });
                        setConfirmDelete(jobIdentifier);
                      }}
                      className="rounded-full border border-rose-border px-3 py-1.5 text-sm text-rose-text hover:border-rose-border"
                    >
                      Delete
                    </button>
                  )}
                </div>

                {editingName === jobIdentifier ? (
                  <form
                    className="mt-4 grid gap-3 rounded-panel border border-border p-3 md:grid-cols-2"
                    onSubmit={handleUpdate}
                  >
                    <TimezoneSelector
                      value={editForm.tz}
                      onChange={(tz) => setEditForm((prev) => ({ ...prev, tz }))}
                      defaultTimezone={me?.timezone}
                    />

                    <ScheduleBuilder
                      expr={editForm.expr}
                      onChange={(expr) => setEditForm((prev) => ({ ...prev, expr }))}
                    />

                    <label className="text-sm text-ink-muted">
                      Delivery
                      <select
                        className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm"
                        value={editForm.deliveryMode}
                        onChange={(e) =>
                          setEditForm((prev) => ({
                            ...prev,
                            deliveryMode: e.target.value,
                          }))
                        }
                      >
                        <option value="announce">Announce (send message)</option>
                        <option value="none">Silent (no message)</option>
                      </select>
                    </label>

                    <label className="text-sm text-ink-muted md:col-span-2">
                      What should your agent do?
                      <textarea
                        className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm"
                        rows={3}
                        value={editForm.message}
                        onChange={(e) => setEditForm((prev) => ({ ...prev, message: e.target.value }))}
                      />
                    </label>

                    <div className="flex flex-col gap-2 md:col-span-2">
                      <div className="flex gap-2">
                        <button
                          type="submit"
                          disabled={updateMutation.isPending}
                          className={getSaveButtonClass(editFeedback.status)}
                        >
                          {updateMutation.isPending
                            ? "Saving..."
                            : editFeedback.status === "success"
                              ? "✓ Saved"
                              : "Save"}
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            clearEditTimer();
                            setEditFeedback({ status: "idle", text: "" });
                            setEditingName(null);
                          }}
                          className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong"
                        >
                          Cancel
                        </button>
                      </div>
                      {editFeedback.status === "error" ? (
                        <p className="text-xs text-rose-500">{editFeedback.text}</p>
                      ) : null}
                    </div>
                  </form>
                ) : null}
              </article>
              );
            })}
          </div>
        </SectionCard>
      ) : (
        <SectionCard title="Your Tasks">
          <p className="text-sm text-ink-muted">No scheduled tasks configured yet.</p>
        </SectionCard>
      )}
    </div>
  );
}
