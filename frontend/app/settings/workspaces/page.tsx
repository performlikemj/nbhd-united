"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { Toast, useToast } from "@/components/toast";
import { Workspace } from "@/lib/types";
import {
  useCreateWorkspaceMutation,
  useDeleteWorkspaceMutation,
  useSwitchWorkspaceMutation,
  useUpdateWorkspaceMutation,
  useWorkspacesQuery,
} from "@/lib/queries";

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function getErrorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  return "Something went wrong.";
}

function formatRelative(iso: string | null): string {
  if (!iso) return "Never used";
  const now = Date.now();
  const then = new Date(iso).getTime();
  const diffSec = Math.floor((now - then) / 1000);
  if (diffSec < 60) return "Just now";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  if (diffSec < 604800) return `${Math.floor(diffSec / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}

/* ------------------------------------------------------------------ */
/*  Form types                                                         */
/* ------------------------------------------------------------------ */

type CreateFormState = {
  name: string;
  description: string;
};

function defaultCreateForm(): CreateFormState {
  return { name: "", description: "" };
}

type EditFormState = {
  name: string;
  description: string;
};

function toEditForm(workspace: Workspace): EditFormState {
  return {
    name: workspace.name,
    description: workspace.description,
  };
}

type ActionFeedback = {
  status: "idle" | "saving" | "success" | "error";
  text: string;
};

const SAVE_CLEAR_DELAY_MS = 2000;
const ERROR_CLEAR_DELAY_MS = 4000;

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

/* ------------------------------------------------------------------ */
/*  Page                                                               */
/* ------------------------------------------------------------------ */

export default function SettingsWorkspacesPage() {
  const { data, isLoading, error } = useWorkspacesQuery();
  const createMutation = useCreateWorkspaceMutation();
  const updateMutation = useUpdateWorkspaceMutation();
  const deleteMutation = useDeleteWorkspaceMutation();
  const switchMutation = useSwitchWorkspaceMutation();

  /* ── Create form ── */
  const [showCreate, setShowCreate] = useState(false);
  const [createForm, setCreateForm] = useState<CreateFormState>(defaultCreateForm);
  const [createFeedback, setCreateFeedback] = useState<ActionFeedback>({ status: "idle", text: "" });
  const createTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /* ── Edit form ── */
  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<EditFormState>({ name: "", description: "" });
  const [editFeedback, setEditFeedback] = useState<ActionFeedback>({ status: "idle", text: "" });
  const editTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /* ── Delete confirm ── */
  const [deleteConfirmSlug, setDeleteConfirmSlug] = useState<string | null>(null);
  const deleteTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /* ── Toast ── */
  const [toast, showToast] = useToast();

  /* ── Cleanup timeouts ── */
  useEffect(() => {
    const createRef = createTimeoutRef;
    const editRef = editTimeoutRef;
    const deleteRef = deleteTimerRef;
    return () => {
      if (createRef.current) clearTimeout(createRef.current);
      if (editRef.current) clearTimeout(editRef.current);
      if (deleteRef.current) clearTimeout(deleteRef.current);
    };
  }, []);

  const workspaces = data?.workspaces ?? [];
  const limit = data?.limit ?? 4;
  const atLimit = workspaces.length >= limit;

  /* ── Handlers ── */

  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (createTimeoutRef.current) clearTimeout(createTimeoutRef.current);
    setCreateFeedback({ status: "saving", text: "" });

    try {
      await createMutation.mutateAsync({
        name: createForm.name.trim(),
        description: createForm.description.trim(),
      });
      setCreateForm(defaultCreateForm());
      setShowCreate(false);
      setSuccess(setCreateFeedback, createTimeoutRef);
      showToast("Workspace created", "success");
    } catch (err) {
      if (createTimeoutRef.current) clearTimeout(createTimeoutRef.current);
      setError(setCreateFeedback, createTimeoutRef, getErrorMessage(err));
    }
  };

  const handleStartEdit = (workspace: Workspace) => {
    setEditingSlug(workspace.slug);
    setEditForm(toEditForm(workspace));
    setEditFeedback({ status: "idle", text: "" });
  };

  const handleCancelEdit = () => {
    setEditingSlug(null);
    setEditFeedback({ status: "idle", text: "" });
  };

  const handleUpdate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editingSlug) return;
    if (editTimeoutRef.current) clearTimeout(editTimeoutRef.current);
    setEditFeedback({ status: "saving", text: "" });

    try {
      await updateMutation.mutateAsync({
        slug: editingSlug,
        data: {
          name: editForm.name.trim(),
          description: editForm.description.trim(),
        },
      });
      setEditingSlug(null);
      setSuccess(setEditFeedback, editTimeoutRef);
      showToast("Workspace updated", "success");
    } catch (err) {
      if (editTimeoutRef.current) clearTimeout(editTimeoutRef.current);
      setError(setEditFeedback, editTimeoutRef, getErrorMessage(err));
    }
  };

  const handleStartDelete = useCallback((slug: string) => {
    if (deleteTimerRef.current) clearTimeout(deleteTimerRef.current);
    setDeleteConfirmSlug(slug);
    deleteTimerRef.current = setTimeout(() => {
      setDeleteConfirmSlug(null);
    }, 4000);
  }, []);

  const handleConfirmDelete = useCallback(
    async (slug: string) => {
      if (deleteTimerRef.current) clearTimeout(deleteTimerRef.current);
      try {
        await deleteMutation.mutateAsync(slug);
        setDeleteConfirmSlug(null);
        showToast("Workspace deleted", "success");
      } catch (err) {
        showToast(getErrorMessage(err), "error");
      }
    },
    [deleteMutation, showToast],
  );

  const handleCancelDelete = useCallback(() => {
    if (deleteTimerRef.current) clearTimeout(deleteTimerRef.current);
    setDeleteConfirmSlug(null);
  }, []);

  const handleSwitch = useCallback(
    async (workspace: Workspace) => {
      if (workspace.is_active) return;
      try {
        await switchMutation.mutateAsync(workspace.slug);
        showToast(`Switched to ${workspace.name}`, "success");
      } catch (err) {
        showToast(getErrorMessage(err), "error");
      }
    },
    [switchMutation, showToast],
  );

  /* ── Render ── */

  if (isLoading) {
    return (
      <div className="space-y-6">
        <SectionCardSkeleton lines={5} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-6">
        <SectionCard title="Workspaces">
          <p className="text-sm text-rose-500">Failed to load workspaces: {(error as Error).message}</p>
        </SectionCard>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <SectionCard
        title="Workspaces"
        subtitle="Organize your conversations into focused contexts. Your assistant automatically routes messages to the right workspace based on topic."
      >
        {/* Header with limit indicator and Add button */}
        <div className="mb-4 flex items-center justify-between gap-4">
          <p className="text-xs text-ink-faint">
            {workspaces.length} of {limit} workspaces
          </p>
          <button
            type="button"
            onClick={() => setShowCreate(true)}
            disabled={atLimit}
            className="rounded-full bg-accent px-4 py-2 text-sm font-medium text-white transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-45"
            title={atLimit ? `Maximum ${limit} workspaces reached` : undefined}
          >
            + New Workspace
          </button>
        </div>

        {/* Empty state */}
        {workspaces.length === 0 ? (
          <div className="rounded-panel border border-dashed border-border p-8 text-center">
            <p className="font-medium text-ink">No workspaces yet</p>
            <p className="mt-2 text-sm text-ink-muted">
              Create your first workspace to organize conversations by topic. A &ldquo;General&rdquo; default
              workspace will be created automatically.
            </p>
          </div>
        ) : (
          /* Workspace cards — grid on desktop, stack on mobile */
          <div className="grid gap-4 sm:grid-cols-2">
            {workspaces.map((workspace) => {
              const isEditing = editingSlug === workspace.slug;
              const isConfirmingDelete = deleteConfirmSlug === workspace.slug;

              return (
                <article
                  key={workspace.id}
                  className={`group flex flex-col rounded-panel border bg-surface p-4 transition ${
                    workspace.is_active
                      ? "border-accent/60 ring-1 ring-accent/20"
                      : "border-border hover:border-border-strong"
                  }`}
                >
                  {isEditing ? (
                    /* Edit form (inline) */
                    <form className="space-y-3" onSubmit={handleUpdate}>
                      <div>
                        <label className="block text-xs font-medium uppercase tracking-wide text-ink-muted">
                          Name
                        </label>
                        <input
                          type="text"
                          value={editForm.name}
                          onChange={(e) => setEditForm((prev) => ({ ...prev, name: e.target.value }))}
                          maxLength={60}
                          required
                          className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
                        />
                      </div>
                      <div>
                        <label className="block text-xs font-medium uppercase tracking-wide text-ink-muted">
                          Description
                        </label>
                        <textarea
                          value={editForm.description}
                          onChange={(e) => setEditForm((prev) => ({ ...prev, description: e.target.value }))}
                          rows={3}
                          placeholder="What topics does this workspace cover?"
                          className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
                        />
                      </div>
                      <div className="flex items-center gap-2">
                        <button
                          type="submit"
                          disabled={updateMutation.isPending}
                          className="rounded-full bg-accent px-4 py-1.5 text-sm font-medium text-white transition hover:bg-accent-hover disabled:opacity-45"
                        >
                          {updateMutation.isPending ? "Saving..." : "Save"}
                        </button>
                        <button
                          type="button"
                          onClick={handleCancelEdit}
                          className="rounded-full border border-border px-4 py-1.5 text-sm text-ink-muted transition hover:border-border-strong hover:text-ink"
                        >
                          Cancel
                        </button>
                      </div>
                      {editFeedback.status === "error" ? (
                        <p className="text-xs text-rose-500">{editFeedback.text}</p>
                      ) : null}
                    </form>
                  ) : (
                    /* Display mode */
                    <>
                      <header className="mb-2 flex items-start justify-between gap-3">
                        <button
                          type="button"
                          onClick={() => handleSwitch(workspace)}
                          disabled={workspace.is_active}
                          className="flex flex-1 items-center gap-2 text-left disabled:cursor-default"
                          aria-label={workspace.is_active ? `${workspace.name} is active` : `Switch to ${workspace.name}`}
                        >
                          <h3 className="font-headline text-base font-semibold text-ink">{workspace.name}</h3>
                          {workspace.is_default ? (
                            <span className="inline-flex rounded-full bg-signal/15 px-2 py-0.5 text-xs font-medium text-signal">
                              Default
                            </span>
                          ) : null}
                          {workspace.is_active ? (
                            <span className="inline-flex items-center gap-1 rounded-full bg-emerald-bg px-2 py-0.5 text-xs font-medium text-emerald-text">
                              <span className="h-1.5 w-1.5 rounded-full bg-emerald-text" />
                              Active
                            </span>
                          ) : null}
                        </button>
                        <div className="flex gap-1">
                          <button
                            type="button"
                            onClick={() => handleStartEdit(workspace)}
                            className="rounded-lg p-2 text-ink-muted transition hover:bg-surface-hover hover:text-ink"
                            aria-label="Edit workspace"
                            title="Edit"
                          >
                            <PencilIcon />
                          </button>
                          <button
                            type="button"
                            onClick={() => handleStartDelete(workspace.slug)}
                            disabled={workspace.is_default}
                            className="rounded-lg p-2 text-ink-muted transition hover:bg-rose-bg hover:text-rose-text disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-ink-muted"
                            aria-label="Delete workspace"
                            title={workspace.is_default ? "Cannot delete the default workspace" : "Delete"}
                          >
                            <TrashIcon />
                          </button>
                        </div>
                      </header>

                      {workspace.description ? (
                        <p className="line-clamp-2 text-sm text-ink-muted">{workspace.description}</p>
                      ) : (
                        <p className="text-sm italic text-ink-faint">No description</p>
                      )}

                      <p className="mt-3 text-xs text-ink-faint">
                        Last active {formatRelative(workspace.last_used_at)}
                      </p>

                      {/* Inline delete confirmation */}
                      {isConfirmingDelete ? (
                        <div className="mt-3 rounded-panel border border-rose-500/30 bg-rose-bg/50 p-3">
                          <p className="text-xs font-medium text-rose-text">
                            Delete <strong>{workspace.name}</strong>? Conversation history will be lost.
                          </p>
                          <div className="mt-2 flex gap-2">
                            <button
                              type="button"
                              onClick={() => handleConfirmDelete(workspace.slug)}
                              disabled={deleteMutation.isPending}
                              className="rounded-full bg-rose-500 px-3 py-1 text-xs font-medium text-white transition hover:bg-rose-600 disabled:opacity-45"
                            >
                              {deleteMutation.isPending ? "Deleting..." : "Delete"}
                            </button>
                            <button
                              type="button"
                              onClick={handleCancelDelete}
                              className="rounded-full border border-border px-3 py-1 text-xs text-ink-muted transition hover:border-border-strong hover:text-ink"
                            >
                              Cancel
                            </button>
                          </div>
                        </div>
                      ) : null}
                    </>
                  )}
                </article>
              );
            })}
          </div>
        )}

        {/* Footer hint */}
        {workspaces.length > 0 ? (
          <p className="mt-6 text-center text-xs text-ink-faint">
            Your assistant can also create and switch workspaces from chat. Try saying &ldquo;create a workspace
            for X&rdquo; or &ldquo;this is work stuff&rdquo;.
          </p>
        ) : null}
      </SectionCard>

      {/* Create modal (portal) */}
      {showCreate
        ? createPortal(
            <CreateModal
              form={createForm}
              setForm={setCreateForm}
              feedback={createFeedback}
              isPending={createMutation.isPending}
              onSubmit={handleCreate}
              onClose={() => {
                setShowCreate(false);
                setCreateForm(defaultCreateForm());
                setCreateFeedback({ status: "idle", text: "" });
              }}
            />,
            document.body,
          )
        : null}

      {toast && <Toast {...toast} onDismiss={() => undefined} />}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Create modal                                                       */
/* ------------------------------------------------------------------ */

function CreateModal({
  form,
  setForm,
  feedback,
  isPending,
  onSubmit,
  onClose,
}: {
  form: CreateFormState;
  setForm: (updater: (prev: CreateFormState) => CreateFormState) => void;
  feedback: ActionFeedback;
  isPending: boolean;
  onSubmit: (e: FormEvent<HTMLFormElement>) => void;
  onClose: () => void;
}) {
  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="create-workspace-title"
    >
      <div className="w-full max-w-md rounded-panel border border-border bg-surface-elevated p-6 shadow-panel">
        <header className="mb-4 flex items-start justify-between gap-4">
          <div>
            <h2 id="create-workspace-title" className="font-headline text-xl font-bold text-ink">
              Create Workspace
            </h2>
            <p className="mt-1 text-sm text-ink-muted">
              Give it a clear name and brief description so your assistant knows when to route messages here.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1 text-ink-muted transition hover:bg-surface-hover hover:text-ink"
            aria-label="Close"
          >
            <XIcon />
          </button>
        </header>

        <form onSubmit={onSubmit} className="space-y-4">
          <div>
            <label className="block text-xs font-medium uppercase tracking-wide text-ink-muted">
              Workspace name
            </label>
            <input
              type="text"
              value={form.name}
              onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
              maxLength={60}
              required
              autoFocus
              placeholder="e.g., Work, Translation, Fitness"
              className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
            />
            <p className="mt-1 text-xs text-ink-faint">{form.name.length}/60 characters</p>
          </div>

          <div>
            <label className="block text-xs font-medium uppercase tracking-wide text-ink-muted">
              Description (optional)
            </label>
            <textarea
              value={form.description}
              onChange={(e) => setForm((prev) => ({ ...prev, description: e.target.value }))}
              rows={3}
              placeholder="What topics does this workspace cover? Your assistant uses this to route messages."
              className="mt-1 w-full rounded-panel border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
            />
          </div>

          {feedback.status === "error" ? (
            <p className="text-xs text-rose-500">{feedback.text}</p>
          ) : null}

          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <button
              type="button"
              onClick={onClose}
              className="rounded-full border border-border px-4 py-2 text-sm text-ink-muted transition hover:border-border-strong hover:text-ink"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isPending || !form.name.trim()}
              className="rounded-full bg-accent px-4 py-2 text-sm font-medium text-white transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-45"
            >
              {isPending ? "Creating..." : "Create Workspace"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Icons                                                              */
/* ------------------------------------------------------------------ */

function PencilIcon() {
  return (
    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0115.75 21H5.25A2.25 2.25 0 013 18.75V8.25A2.25 2.25 0 015.25 6H10" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
    </svg>
  );
}

function XIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
    </svg>
  );
}
