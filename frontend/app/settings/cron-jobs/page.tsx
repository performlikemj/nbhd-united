"use client";

import { FormEvent, useState } from "react";

import ScheduleBuilder, { cronToHuman } from "@/components/schedule-builder";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import { CronJob } from "@/lib/types";
import {
  useCronJobsQuery,
  useCreateCronJobMutation,
  useDeleteCronJobMutation,
  useMeQuery,
  useToggleCronJobMutation,
  useUpdateCronJobMutation,
} from "@/lib/queries";

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
    tz: job.schedule.tz,
    message: job.payload.message,
    deliveryMode: job.delivery.mode,
    deliveryChannel: job.delivery.channel ?? "",
  };
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return "Request failed.";
}

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


  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
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
  };

  const handleStartEdit = (job: CronJob) => {
    setEditingName(job.name);
    setEditForm(toEditForm(job));
  };

  const handleUpdate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editingName) return;
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
  };

  const handleDelete = async (name: string) => {
    await deleteMutation.mutateAsync(name);
    setConfirmDelete(null);
  };

  return (
    <div className="space-y-4">
      <SectionCard
        title="Scheduled Tasks"
        subtitle="Manage cron jobs that run on your OpenClaw agent — morning briefings, evening check-ins, and more"
      >
        {!showCreate ? (
          <button
            type="button"
            onClick={() => {
              setCreateForm(defaultCreateForm(me?.timezone));
              setShowCreate(true);
            }}
            className="rounded-full border border-ink/20 px-4 py-2 text-sm hover:border-ink/40"
          >
            Add scheduled task
          </button>
        ) : (
          <form className="grid gap-3 md:grid-cols-2" onSubmit={handleCreate}>
            <label className="text-sm text-ink/70">
              Name
              <input
                className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                placeholder="e.g. Morning Briefing"
                value={createForm.name}
                onChange={(e) => setCreateForm((prev) => ({ ...prev, name: e.target.value }))}
                required
              />
            </label>

            <label className="text-sm text-ink/70">
              Timezone
              <input
                className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                value={createForm.tz}
                onChange={(e) => setCreateForm((prev) => ({ ...prev, tz: e.target.value }))}
              />
            </label>

            <ScheduleBuilder
              expr={createForm.expr}
              onChange={(expr) => setCreateForm((prev) => ({ ...prev, expr }))}
            />

            <label className="text-sm text-ink/70">
              Delivery
              <select
                className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                value={createForm.deliveryMode}
                onChange={(e) => setCreateForm((prev) => ({ ...prev, deliveryMode: e.target.value }))}
              >
                <option value="announce">Announce (send message)</option>
                <option value="none">Silent (no message)</option>
              </select>
            </label>

            <label className="text-sm text-ink/70 md:col-span-2">
              Prompt message
              <textarea
                className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                rows={3}
                placeholder="What should the agent do when this task runs?"
                value={createForm.message}
                onChange={(e) => setCreateForm((prev) => ({ ...prev, message: e.target.value }))}
                required
              />
            </label>

            <div className="flex gap-2 md:col-span-2">
              <button
                type="submit"
                disabled={createMutation.isPending}
                className="rounded-full border border-ink/20 px-4 py-2 text-sm hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-45"
              >
                {createMutation.isPending ? "Creating..." : "Create"}
              </button>
              <button
                type="button"
                onClick={() => setShowCreate(false)}
                className="rounded-full border border-ink/20 px-4 py-2 text-sm hover:border-ink/40"
              >
                Cancel
              </button>
            </div>
          </form>
        )}

        {createMutation.isError ? (
          <p className="mt-3 rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
            {getErrorMessage(createMutation.error)}
          </p>
        ) : null}
      </SectionCard>

      {isLoading ? (
        <SectionCardSkeleton lines={5} />
      ) : error ? (
        <SectionCard title="Your Tasks">
          <p className="rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
            Could not load scheduled tasks.
            {error instanceof Error ? ` ${error.message}` : ""}
          </p>
          <p className="mt-2 text-xs text-ink/50">
            If this persists, run: python manage.py check_gateway_health
          </p>
        </SectionCard>
      ) : cronJobs && cronJobs.length > 0 ? (
        <SectionCard title="Your Tasks" subtitle="Toggle, edit, or remove scheduled tasks">
          <div className="space-y-3">
            {cronJobs.map((job) => (
              <article
                key={job.name}
                className="rounded-panel border border-ink/15 bg-white p-4"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p className="text-base font-medium">{job.name}</p>
                    <p className="text-sm text-ink/70">
                      {cronToHuman(job.schedule?.expr ?? "", job.schedule?.tz ?? "UTC")}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <StatusPill status={job.enabled ? "active" : "paused"} />
                    {job.delivery?.mode !== "none" && job.delivery?.channel ? (
                      <span className="rounded-full bg-ink/5 px-2.5 py-0.5 text-xs text-ink/60">
                        {job.delivery.channel}
                      </span>
                    ) : null}
                  </div>
                </div>

                <p className="mt-2 text-sm text-ink/60 line-clamp-2">
                  {(job.payload?.message ?? "").slice(0, 200)}
                </p>

                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() =>
                      toggleMutation.mutate({ name: job.name, enabled: !job.enabled })
                    }
                    disabled={toggleMutation.isPending}
                    className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-45"
                  >
                    {job.enabled ? "Disable" : "Enable"}
                  </button>

                  <button
                    type="button"
                    onClick={() => handleStartEdit(job)}
                    className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40"
                  >
                    Edit
                  </button>

                  {confirmDelete === job.name ? (
                    <>
                      <button
                        type="button"
                        onClick={() => handleDelete(job.name)}
                        disabled={deleteMutation.isPending}
                        className="rounded-full border border-rose-300 px-3 py-1.5 text-sm text-rose-700 hover:border-rose-500 disabled:cursor-not-allowed disabled:opacity-45"
                      >
                        {deleteMutation.isPending ? "Deleting..." : "Confirm delete"}
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirmDelete(null)}
                        className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40"
                      >
                        Cancel
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setConfirmDelete(job.name)}
                      className="rounded-full border border-rose-300 px-3 py-1.5 text-sm text-rose-700 hover:border-rose-500"
                    >
                      Delete
                    </button>
                  )}
                </div>

                {editingName === job.name ? (
                  <form
                    className="mt-4 grid gap-3 rounded-panel border border-ink/10 p-3 md:grid-cols-2"
                    onSubmit={handleUpdate}
                  >
                    <label className="text-sm text-ink/70">
                      Timezone
                      <input
                        className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                        value={editForm.tz}
                        onChange={(e) => setEditForm((prev) => ({ ...prev, tz: e.target.value }))}
                      />
                    </label>

                    <ScheduleBuilder
                      expr={editForm.expr}
                      onChange={(expr) => setEditForm((prev) => ({ ...prev, expr }))}
                    />

                    <label className="text-sm text-ink/70">
                      Delivery
                      <select
                        className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
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

                    <label className="text-sm text-ink/70 md:col-span-2">
                      Prompt message
                      <textarea
                        className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-3 py-2 text-sm"
                        rows={3}
                        value={editForm.message}
                        onChange={(e) => setEditForm((prev) => ({ ...prev, message: e.target.value }))}
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
                        onClick={() => setEditingName(null)}
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
      ) : (
        <SectionCard title="Your Tasks">
          <p className="text-sm text-ink/70">No scheduled tasks configured yet.</p>
        </SectionCard>
      )}
    </div>
  );
}
