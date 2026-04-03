"use client";

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { CapabilityChips } from "@/components/capability-chips";
import { PromptEditor, type PromptEditorHandle } from "@/components/prompt-editor";
import ScheduleBuilder, { cronToHuman } from "@/components/schedule-builder";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import { Toast, useToast } from "@/components/toast";
import TimezoneSelector from "@/components/timezone-selector";
import { FeatureTipsSection } from "@/components/feature-tips-section";
import SessionModeSelector from "@/components/session-mode-selector";
import { WorkingHoursSection } from "@/components/working-hours-section";
import { CronJob } from "@/lib/types";
import {
  useBulkDeleteCronJobsMutation,
  useCronJobsQuery,
  useCreateCronJobMutation,
  useDeleteCronJobMutation,
  useIntegrationsQuery,
  useMeQuery,
  useToggleCronJobMutation,
  useUpdateCronJobMutation,
} from "@/lib/queries";

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

/** Strip internal date-context prefix injected by _inject_date_context(). */
function stripPromptPrefix(msg: string): string {
  const marker = "1 day away.\n\n";
  const idx = msg.indexOf(marker);
  return idx !== -1 ? msg.slice(idx + marker.length) : msg;
}

/* ------------------------------------------------------------------ */
/*  Task templates                                                     */
/* ------------------------------------------------------------------ */

interface TaskTemplate {
  icon: string;
  name: string;
  message: string;
  expr: string;
  sessionTarget?: "main" | "isolated";
}

const TASK_TEMPLATES: TaskTemplate[] = [
  {
    icon: "☀️",
    name: "Morning Briefing",
    message:
      "Check my calendar, weather, and any important emails. Give me a quick summary to start my day.",
    expr: "0 7 * * *",
    sessionTarget: "main",
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
    sessionTarget: "main",
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
  { icon: "📰", name: "News", desc: "Find and summarize news articles" },
  { icon: "💬", name: "Message", desc: "Send you updates via chat" },
  { icon: "🧠", name: "Memory", desc: "Remember things about you and recall past conversations" },
  { icon: "📝", name: "Journal", desc: "Write to your daily notes and long-term memory" },
  { icon: "🎯", name: "Goals & Projects", desc: "Track goals, projects, tasks, and ideas" },
  { icon: "🔍", name: "Search Notes", desc: "Search across all your journal entries and notes" },
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
  sessionTarget: "main" | "isolated";
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
    sessionTarget: "isolated",
  };
}

type EditFormState = {
  expr: string;
  tz: string;
  message: string;
  deliveryMode: string;
  deliveryChannel: string;
  sessionTarget: "main" | "isolated";
};

function toEditForm(job: CronJob): EditFormState {
  return {
    expr: job.schedule.expr,
    tz: job.schedule.tz || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    message: stripPromptPrefix(job.payload.message),
    deliveryMode: job.delivery.mode,
    deliveryChannel: job.delivery.channel ?? "",
    sessionTarget: (job.sessionTarget === "main" ? "main" : "isolated"),
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
/** ms before inline single-delete confirmation auto-cancels */
const SINGLE_DELETE_TIMEOUT_MS = 3000;

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

type BulkBarState = "idle" | "confirming";

export default function SettingsCronJobsPage() {
  const { data: me } = useMeQuery();
  const { data: cronJobs, isLoading, error } = useCronJobsQuery();
  const { data: integrations } = useIntegrationsQuery();
  const createMutation = useCreateCronJobMutation();
  const deleteMutation = useDeleteCronJobMutation();
  const bulkDeleteMutation = useBulkDeleteCronJobsMutation();
  const toggleMutation = useToggleCronJobMutation();
  const updateMutation = useUpdateCronJobMutation();

  /* ── Connected providers (for capability chips) ── */
  const connectedProviders = useMemo(() => {
    const set = new Set<string>();
    integrations?.forEach((i) => {
      if (i.status === "active") set.add(i.provider);
    });
    return set;
  }, [integrations]);

  /* ── Create form ── */
  const [showCreate, setShowCreate] = useState(false);
  const [createForm, setCreateForm] = useState<CreateFormState>(() => defaultCreateForm(me?.timezone));
  const [showCapabilities, setShowCapabilities] = useState(false);
  const [createFeedback, setCreateFeedback] = useState<ActionFeedback>({ status: "idle", text: "" });
  const createTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const createEditorRef = useRef<PromptEditorHandle>(null);

  /* ── Edit form ── */
  const [editingName, setEditingName] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<EditFormState>({
    expr: "",
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    message: "",
    deliveryMode: "announce",
    deliveryChannel: "",
    sessionTarget: "isolated",
  });
  const [editFeedback, setEditFeedback] = useState<ActionFeedback>({ status: "idle", text: "" });
  const editTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const editEditorRef = useRef<PromptEditorHandle>(null);

  /* ── Single delete — inline per-row state ── */
  // Map of jobIdentifier → "confirming" | null
  const [singleDeleteConfirm, setSingleDeleteConfirm] = useState<Record<string, boolean>>({});
  const [singleDeleteError, setSingleDeleteError] = useState<Record<string, string>>({});
  const [singleDeletePending, setSingleDeletePending] = useState<Record<string, boolean>>({});
  // Auto-cancel timers per job
  const singleDeleteTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  /* ── Multi-select ── */
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkBarState, setBulkBarState] = useState<BulkBarState>("idle");
  const [bulkDeleteError, setBulkDeleteError] = useState<string | null>(null);

  /* ── Toast ── */
  const [toast, showToast] = useToast();

  /* ── Mobile detection ── */
  const [isMobile, setIsMobile] = useState<boolean | null>(null);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 767px)");
    setIsMobile(mq.matches);
    const handler = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  // Lock body scroll + hide header when mobile edit overlay is open
  useEffect(() => {
    if (editingName && isMobile === true) {
      const prevOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      const appHeader = document.querySelector<HTMLElement>("header.sticky, header[class*='sticky']");
      const prevVisibility = appHeader ? appHeader.style.visibility : null;
      if (appHeader) appHeader.style.visibility = "hidden";
      return () => {
        document.body.style.overflow = prevOverflow;
        if (appHeader && prevVisibility !== null) appHeader.style.visibility = prevVisibility;
      };
    }
  }, [editingName, isMobile]);

  /* ── Cleanup timers on unmount ── */
  useEffect(() => {
    return () => {
      // These refs hold timeout IDs (not DOM nodes) so .current is stable at cleanup time
      // eslint-disable-next-line react-hooks/exhaustive-deps
      if (createTimeoutRef.current) clearTimeout(createTimeoutRef.current);
      // eslint-disable-next-line react-hooks/exhaustive-deps
      if (editTimeoutRef.current) clearTimeout(editTimeoutRef.current);
      // eslint-disable-next-line react-hooks/exhaustive-deps
      Object.values(singleDeleteTimers.current).forEach(clearTimeout);
    };
  }, []);

  /* ── Feedback helpers ── */
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

  /* ── Template ── */
  const handleApplyTemplate = (template: TaskTemplate) => {
    setCreateForm((prev) => ({
      ...prev,
      name: template.name,
      message: template.message,
      expr: template.expr,
      sessionTarget: template.sessionTarget ?? "isolated",
    }));
  };

  /* ── Create ── */
  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (createTimeoutRef.current) clearTimeout(createTimeoutRef.current);
    setCreateFeedback({ status: "saving", text: "" });

    try {
      await createMutation.mutateAsync({
        name: createForm.name.trim(),
        schedule: { kind: "cron", expr: createForm.expr.trim(), tz: createForm.tz.trim() },
        sessionTarget: createForm.sessionTarget,
        ...(createForm.sessionTarget === "main" ? { wakeMode: "now" } : {}),
        payload: {
          kind: "agentTurn",
          message: createForm.message.trim(),
        },
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
      if (createTimeoutRef.current) clearTimeout(createTimeoutRef.current);
      setError(setCreateFeedback, createTimeoutRef, getErrorMessage(err));
    }
  };

  /* ── Edit ── */
  const handleStartEdit = (job: CronJob) => {
    if (editTimeoutRef.current) clearTimeout(editTimeoutRef.current);
    setEditFeedback({ status: "idle", text: "" });
    setEditingName(job.jobId ?? job.name);
    setEditForm(toEditForm(job));
  };

  const handleUpdate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editingName) return;
    if (editTimeoutRef.current) clearTimeout(editTimeoutRef.current);
    setEditFeedback({ status: "saving", text: "" });

    try {
      await updateMutation.mutateAsync({
        name: editingName,
        data: {
          schedule: { kind: "cron", expr: editForm.expr.trim(), tz: editForm.tz.trim() },
          sessionTarget: editForm.sessionTarget,
          ...(editForm.sessionTarget === "main" ? { wakeMode: "now" } : {}),
          payload: {
            kind: "agentTurn",
            message: editForm.message.trim(),
          },
          delivery: {
            mode: editForm.deliveryMode,
            ...(editForm.deliveryChannel ? { channel: editForm.deliveryChannel } : {}),
          },
        },
      });
      setEditingName(null);
      setSuccess(setEditFeedback, editTimeoutRef);
    } catch (err) {
      if (editTimeoutRef.current) clearTimeout(editTimeoutRef.current);
      setError(setEditFeedback, editTimeoutRef, getErrorMessage(err));
    }
  };

  /* ── Single delete — inline UX ── */
  const startSingleDeleteConfirm = useCallback((jobId: string) => {
    // Clear any existing timer for this job
    if (singleDeleteTimers.current[jobId]) {
      clearTimeout(singleDeleteTimers.current[jobId]);
    }
    setSingleDeleteConfirm((prev) => ({ ...prev, [jobId]: true }));
    setSingleDeleteError((prev) => ({ ...prev, [jobId]: "" }));

    // Auto-cancel after 3s
    singleDeleteTimers.current[jobId] = setTimeout(() => {
      setSingleDeleteConfirm((prev) => ({ ...prev, [jobId]: false }));
    }, SINGLE_DELETE_TIMEOUT_MS);
  }, []);

  const cancelSingleDelete = useCallback((jobId: string) => {
    if (singleDeleteTimers.current[jobId]) {
      clearTimeout(singleDeleteTimers.current[jobId]);
      delete singleDeleteTimers.current[jobId];
    }
    setSingleDeleteConfirm((prev) => ({ ...prev, [jobId]: false }));
    setSingleDeleteError((prev) => ({ ...prev, [jobId]: "" }));
  }, []);

  const confirmSingleDelete = useCallback(async (jobId: string) => {
    if (singleDeleteTimers.current[jobId]) {
      clearTimeout(singleDeleteTimers.current[jobId]);
      delete singleDeleteTimers.current[jobId];
    }
    setSingleDeletePending((prev) => ({ ...prev, [jobId]: true }));

    try {
      await deleteMutation.mutateAsync({ name: jobId });
      // Remove from selection if selected
      setSelectedIds((prev) => {
        const next = new Set(prev);
        next.delete(jobId);
        return next;
      });
      setSingleDeleteConfirm((prev) => ({ ...prev, [jobId]: false }));
    } catch (err) {
      setSingleDeleteError((prev) => ({ ...prev, [jobId]: getErrorMessage(err) }));
      // Auto-clear error after 4s and reset confirm state
      singleDeleteTimers.current[jobId] = setTimeout(() => {
        setSingleDeleteConfirm((prev) => ({ ...prev, [jobId]: false }));
        setSingleDeleteError((prev) => ({ ...prev, [jobId]: "" }));
      }, ERROR_CLEAR_DELAY_MS);
    } finally {
      setSingleDeletePending((prev) => ({ ...prev, [jobId]: false }));
    }
  }, [deleteMutation]);

  /* ── Multi-select helpers ── */
  const selectableJobs = cronJobs ?? [];

  const allSelected =
    selectableJobs.length > 0 && selectableJobs.every((j) => selectedIds.has(j.jobId ?? j.name));
  const someSelected = selectedIds.size > 0;

  const toggleSelectAll = () => {
    if (allSelected) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(selectableJobs.map((j) => j.jobId ?? j.name)));
    }
  };

  const toggleSelectOne = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  /* ── Bulk delete ── */
  const handleBulkDeleteClick = () => {
    setBulkBarState("confirming");
    setBulkDeleteError(null);
  };

  const handleBulkDeleteCancel = () => {
    setBulkBarState("idle");
    setBulkDeleteError(null);
  };

  const handleBulkDeleteConfirm = async () => {
    const ids = Array.from(selectedIds);
    setBulkDeleteError(null);

    try {
      const result = await bulkDeleteMutation.mutateAsync(ids);
      const deletedCount = result.deleted;
      setSelectedIds(new Set());
      setBulkBarState("idle");
      showToast(`${deletedCount} task${deletedCount === 1 ? "" : "s"} deleted`, "success");
    } catch (err) {
      setBulkDeleteError(getErrorMessage(err));
      setBulkBarState("idle");
    }
  };

  /* ---------------------------------------------------------------- */

  return (
    <div className="space-y-4">
      {/* Toast notification */}
      {toast && (
        <Toast
          key={toast.id}
          message={toast.message}
          type={toast.type}
          onDismiss={() => {/* state auto-managed by hook */}}
        />
      )}

      <WorkingHoursSection timezone={me?.timezone} />

      <FeatureTipsSection />

      <SectionCard
        title="Scheduled Tasks"
        subtitle="Set up recurring tasks for your AI assistant — morning briefings, news digests, reminders, and more"
      >
        {!showCreate ? (
          <button
            type="button"
            onClick={() => {
              if (createTimeoutRef.current) clearTimeout(createTimeoutRef.current);
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
              <div className="flex gap-2 overflow-x-auto pb-2 scrollbar-none sm:flex-wrap sm:overflow-visible sm:pb-0">
                {TASK_TEMPLATES.map((tpl) => (
                  <button
                    key={tpl.name}
                    type="button"
                    onClick={() => handleApplyTemplate(tpl)}
                    className={`shrink-0 rounded-full border px-4 py-2.5 text-sm transition min-h-[44px] ${
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
            <form className="grid gap-4 sm:gap-3 md:grid-cols-2" onSubmit={handleCreate}>
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

              <div className="md:col-span-2">
                <SessionModeSelector
                  value={createForm.sessionTarget}
                  onChange={(v) => setCreateForm((prev) => ({ ...prev, sessionTarget: v }))}
                />
              </div>

              <div className="text-sm text-ink-muted md:col-span-2">
                <span
                  className="block mb-1 cursor-pointer"
                  onClick={() => createEditorRef.current?.focus()}
                >
                  What should your agent do?
                </span>
                <PromptEditor
                  ref={createEditorRef}
                  value={createForm.message}
                  onChange={(msg) => setCreateForm((prev) => ({ ...prev, message: msg }))}
                  placeholder="Describe the task in plain language — e.g. 'I want [Google] to check for important emails and [Weather] to give me a forecast'"
                />
              </div>

              {/* Capability chips — insert tags at cursor */}
              <div className="md:col-span-2">
                <CapabilityChips
                  message={createForm.message}
                  editorRef={createEditorRef}
                  onInsertTag={(tag) => createEditorRef.current?.insertChip(tag)}
                  connectedProviders={connectedProviders}
                />
              </div>

              {/* Capabilities reference */}
              <div className="md:col-span-2">
                <button
                  type="button"
                  onClick={() => setShowCapabilities(!showCapabilities)}
                  className="text-xs text-accent hover:underline"
                >
                  {showCapabilities ? "Hide" : "Show"} all capabilities ↗
                </button>
                {showCapabilities && (
                  <div className="mt-2 rounded-panel border border-border bg-surface-hover p-3">
                    <p className="mb-2 text-xs text-ink-faint">Write your task in plain language — your agent understands these capabilities:</p>
                    <div className="grid gap-2 sm:grid-cols-2">
                      {AGENT_CAPABILITIES.map((cap) => (
                        <div key={cap.name} className="flex items-start gap-2 text-sm">
                          <span className="text-base leading-5">{cap.icon}</span>
                          <div>
                            <span className="font-medium text-ink">{cap.name}</span>
                            <span className="text-ink-muted"> — {cap.desc}</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              <div className="flex flex-col gap-3 md:col-span-2">
                <button
                  type="submit"
                  disabled={createMutation.isPending}
                  className={`${getSaveButtonClass(createFeedback.status)} w-full sm:w-auto min-h-[48px]`}
                >
                  {createMutation.isPending
                    ? "Creating..."
                    : createFeedback.status === "success"
                      ? "✓ Created"
                      : "Create"}
                </button>
                {createFeedback.status === "error" ? (
                  <p className="text-xs text-rose-500">{createFeedback.text}</p>
                ) : null}
                <button
                  type="button"
                  onClick={() => {
                    if (createTimeoutRef.current) clearTimeout(createTimeoutRef.current);
                    setCreateFeedback({ status: "idle", text: "" });
                    setShowCreate(false);
                  }}
                  className="w-full rounded-full border border-border-strong px-4 py-2 text-sm hover:border-border-strong sm:w-auto min-h-[48px]"
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
          {/* ── Bulk action bar ── */}
          <div
            className={[
              "overflow-hidden transition-all duration-300",
              someSelected ? "max-h-24 mb-3" : "max-h-0",
            ].join(" ")}
            aria-live="polite"
          >
            <div className="flex flex-col gap-2 rounded-panel border border-border bg-surface-hover px-4 py-3 sm:flex-row sm:flex-wrap sm:items-center sm:gap-3">
              {bulkBarState === "idle" ? (
                <>
                  <span className="text-sm font-medium text-ink">
                    {selectedIds.size} task{selectedIds.size === 1 ? "" : "s"} selected
                  </span>
                  <button
                    type="button"
                    onClick={handleBulkDeleteClick}
                    disabled={bulkDeleteMutation.isPending}
                    className="rounded-full border border-rose-border bg-rose-bg px-3 py-1.5 text-sm text-rose-text hover:bg-rose-bg/80 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Delete selected
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedIds(new Set());
                      setBulkBarState("idle");
                    }}
                    className="rounded-full border border-border-strong px-3 py-1.5 text-sm text-ink-muted hover:text-ink"
                  >
                    Clear
                  </button>
                  {bulkDeleteError && (
                    <p className="text-xs text-rose-500">{bulkDeleteError}</p>
                  )}
                </>
              ) : (
                <>
                  <span className="text-sm font-medium text-rose-600">
                    Delete {selectedIds.size} task{selectedIds.size === 1 ? "" : "s"}? This cannot be undone.
                  </span>
                  <button
                    type="button"
                    onClick={handleBulkDeleteConfirm}
                    disabled={bulkDeleteMutation.isPending}
                    className="rounded-full border border-rose-500 bg-rose-500 px-3 py-1.5 text-sm text-white hover:bg-rose-600 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {bulkDeleteMutation.isPending ? "Deleting…" : "Confirm"}
                  </button>
                  <button
                    type="button"
                    onClick={handleBulkDeleteCancel}
                    disabled={bulkDeleteMutation.isPending}
                    className="rounded-full border border-border-strong px-3 py-1.5 text-sm text-ink-muted hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Cancel
                  </button>
                </>
              )}
            </div>
          </div>

          {/* ── Select all header row ── */}
          {selectableJobs.length > 1 && (
            <div className="mb-2 flex items-center gap-3 px-1">
              {/* 44px touch target wrapper */}
              <div className="flex h-11 w-11 items-center justify-center">
                <input
                  type="checkbox"
                  aria-label="Select all tasks"
                  checked={allSelected}
                  onChange={toggleSelectAll}
                  className="h-4 w-4 cursor-pointer accent-accent"
                />
              </div>
              <span className="text-xs text-ink-faint">
                {allSelected ? "Deselect all" : "Select all"}
              </span>
            </div>
          )}

          <div className="space-y-3">
            {cronJobs.map((job) => {
              const jobIdentifier = job.jobId ?? job.name;
              const isSelected = selectedIds.has(jobIdentifier);
              const isConfirmingDelete = !!singleDeleteConfirm[jobIdentifier];
              const isSingleDeletePending = !!singleDeletePending[jobIdentifier];
              const singleError = singleDeleteError[jobIdentifier] ?? "";

              return (
                <article
                  key={job.name}
                  className={[
                    "rounded-panel border p-4 transition-colors duration-150",
                    isConfirmingDelete
                      ? "border-rose-border bg-rose-bg/30"
                      : isSelected
                        ? "border-border bg-surface-hover"
                        : "border-border bg-surface-elevated",
                  ].join(" ")}
                >
                  <div className="flex flex-wrap items-start gap-2">
                    {/* Checkbox — 44px touch target */}
                    <div className="flex h-11 w-11 flex-shrink-0 items-center justify-center">
                      <input
                        type="checkbox"
                        aria-label={`Select ${job.name}`}
                        checked={isSelected}
                        onChange={() => toggleSelectOne(jobIdentifier)}
                        className="h-4 w-4 cursor-pointer accent-accent"
                      />
                    </div>

                    {/* Job info */}
                    <div className="min-w-0 flex-1">
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
                          <span className="rounded-full bg-surface-hover px-2.5 py-0.5 text-xs text-ink-muted">
                            {job.sessionTarget === "main" ? "Main" : "Background"}
                          </span>
                        </div>
                      </div>

                      <p className="mt-2 text-sm text-ink-muted line-clamp-2">
                        {stripPromptPrefix(job.payload?.message ?? "").slice(0, 200)}
                      </p>

                      {/* Action buttons */}
                      <div className="mt-3 flex flex-wrap gap-2.5">
                        <button
                          type="button"
                          onClick={() =>
                            toggleMutation.mutate({ name: jobIdentifier, enabled: !job.enabled })
                          }
                          disabled={toggleMutation.isPending}
                          className="rounded-full border border-border-strong px-3.5 py-2 text-sm hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-45 min-h-[44px]"
                        >
                          {job.enabled ? "Disable" : "Enable"}
                        </button>

                        <button
                          type="button"
                          onClick={() => handleStartEdit(job)}
                          className="rounded-full border border-border-strong px-3.5 py-2 text-sm hover:border-border-strong min-h-[44px]"
                        >
                          Edit
                        </button>

                        {/* Single delete — inline confirm */}
                        {isConfirmingDelete ? (
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="text-sm text-rose-600 font-medium">Delete?</span>
                            <button
                              type="button"
                              onClick={() => void confirmSingleDelete(jobIdentifier)}
                              disabled={isSingleDeletePending}
                              aria-label="Confirm delete"
                              className="flex h-11 w-11 items-center justify-center rounded-full border border-rose-500 bg-rose-500 text-white hover:bg-rose-600 disabled:cursor-not-allowed disabled:opacity-50"
                            >
                              {isSingleDeletePending ? "…" : "✓"}
                            </button>
                            <button
                              type="button"
                              onClick={() => cancelSingleDelete(jobIdentifier)}
                              disabled={isSingleDeletePending}
                              aria-label="Cancel delete"
                              className="flex h-11 w-11 items-center justify-center rounded-full border border-border-strong text-sm text-ink-muted hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
                            >
                              ✕
                            </button>
                            {singleError && (
                              <p className="w-full text-xs text-rose-500">{singleError}</p>
                            )}
                          </div>
                        ) : (
                          <button
                            type="button"
                            onClick={() => startSingleDeleteConfirm(jobIdentifier)}
                            className="rounded-full border border-rose-border px-3.5 py-2 text-sm text-rose-text hover:border-rose-border min-h-[44px]"
                          >
                            Delete
                          </button>
                        )}
                      </div>

                      {/* Edit form — full-screen overlay on mobile, inline on desktop */}
                      {editingName === jobIdentifier && isMobile === true
                        ? createPortal(
                            <div className="fixed inset-0 z-[9999] flex flex-col overflow-hidden bg-[var(--bg)]">
                              <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border bg-surface/75 backdrop-blur-xl px-4 py-3">
                                <span className="min-w-0 truncate text-sm font-semibold text-ink">Edit Task</span>
                                <div className="flex shrink-0 items-center gap-2">
                                  <button
                                    type="button"
                                    onClick={(e) => { e.preventDefault(); void handleUpdate(e as unknown as FormEvent<HTMLFormElement>); }}
                                    disabled={updateMutation.isPending}
                                    className="min-h-[44px] rounded-full bg-accent px-4 py-2 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55"
                                  >
                                    {updateMutation.isPending ? "..." : "Save"}
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => {
                                      if (editTimeoutRef.current) clearTimeout(editTimeoutRef.current);
                                      setEditFeedback({ status: "idle", text: "" });
                                      setEditingName(null);
                                    }}
                                    className="min-h-[44px] rounded-full border border-border-strong px-4 py-2 text-sm hover:border-border-strong"
                                  >
                                    Cancel
                                  </button>
                                </div>
                              </div>
                              <div
                                className="flex-1 overscroll-y-contain"
                                style={{ overflowY: "scroll", WebkitOverflowScrolling: "touch", paddingBottom: "calc(env(safe-area-inset-bottom) + 80px)" }}
                              >
                                <div className="grid gap-4 p-4">
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
                                      className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2.5 text-sm min-h-[44px]"
                                      value={editForm.deliveryMode}
                                      onChange={(e) =>
                                        setEditForm((prev) => ({ ...prev, deliveryMode: e.target.value }))
                                      }
                                    >
                                      <option value="announce">Announce (send message)</option>
                                      <option value="none">Silent (no message)</option>
                                    </select>
                                  </label>

                                  <SessionModeSelector
                                    value={editForm.sessionTarget}
                                    onChange={(v) => setEditForm((prev) => ({ ...prev, sessionTarget: v }))}
                                  />

                                  <div className="text-sm text-ink-muted">
                                    <span
                                      className="block mb-1 cursor-pointer"
                                      onClick={() => editEditorRef.current?.focus()}
                                    >
                                      What should your agent do?
                                    </span>
                                    <PromptEditor
                                      ref={editEditorRef}
                                      value={editForm.message}
                                      onChange={(msg) => setEditForm((prev) => ({ ...prev, message: msg }))}
                                      minHeight="150px"
                                    />
                                  </div>

                                  <CapabilityChips
                                    message={editForm.message}
                                    editorRef={editEditorRef}
                                    onInsertTag={(tag) => editEditorRef.current?.insertChip(tag)}
                                    connectedProviders={connectedProviders}
                                  />

                                  {editFeedback.status === "error" ? (
                                    <p className="text-xs text-rose-500">{editFeedback.text}</p>
                                  ) : null}
                                </div>
                              </div>
                            </div>,
                            document.body,
                          )
                        : editingName === jobIdentifier ? (
                        <form
                          className="mt-4 grid gap-4 rounded-panel border border-border p-3 sm:gap-3 md:grid-cols-2"
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
                              className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2.5 text-sm min-h-[44px]"
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

                          <div className="md:col-span-2">
                            <SessionModeSelector
                              value={editForm.sessionTarget}
                              onChange={(v) => setEditForm((prev) => ({ ...prev, sessionTarget: v }))}
                            />
                          </div>

                          <div className="text-sm text-ink-muted md:col-span-2">
                            <span
                              className="block mb-1 cursor-pointer"
                              onClick={() => editEditorRef.current?.focus()}
                            >
                              What should your agent do?
                            </span>
                            <PromptEditor
                              ref={editEditorRef}
                              value={editForm.message}
                              onChange={(msg) => setEditForm((prev) => ({ ...prev, message: msg }))}
                            />
                          </div>

                          {/* Capability chips — insert tags at cursor */}
                          <div className="md:col-span-2">
                            <CapabilityChips
                              message={editForm.message}
                              editorRef={editEditorRef}
                              onInsertTag={(tag) => editEditorRef.current?.insertChip(tag)}
                              connectedProviders={connectedProviders}
                            />
                          </div>

                          <div className="flex flex-col gap-3 md:col-span-2">
                            <div className="flex gap-2">
                              <button
                                type="submit"
                                disabled={updateMutation.isPending}
                                className={`${getSaveButtonClass(editFeedback.status)} min-h-[44px]`}
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
                                  if (editTimeoutRef.current) clearTimeout(editTimeoutRef.current);
                                  setEditFeedback({ status: "idle", text: "" });
                                  setEditingName(null);
                                }}
                                className="rounded-full border border-border-strong px-3.5 py-2 text-sm hover:border-border-strong min-h-[44px]"
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
                    </div>
                  </div>
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
