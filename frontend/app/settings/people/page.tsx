"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { SectionCard } from "@/components/section-card";
import {
  type EntityRegistryEntry,
  deleteEntityRegistryEntry,
  fetchEntityRegistry,
  updateEntityRegistryEntry,
} from "@/lib/api";

// ── Local edit state ─────────────────────────────────────────────────────────
// We hold a draft per row so unsaved edits don't get clobbered by query
// refetches. Save / cancel collapse the draft back to the server state.

type DraftMap = Record<string, { name: string; relationship: string; notes: string }>;

function emptyDraft(): { name: string; relationship: string; notes: string } {
  return { name: "", relationship: "", notes: "" };
}

function entryToDraft(entry: EntityRegistryEntry) {
  return {
    name: entry.name ?? "",
    relationship: entry.relationship ?? "",
    notes: entry.notes ?? "",
  };
}

function draftEquals(entry: EntityRegistryEntry, draft: ReturnType<typeof emptyDraft>): boolean {
  return (
    (entry.name ?? "") === draft.name &&
    (entry.relationship ?? "") === draft.relationship &&
    (entry.notes ?? "") === draft.notes
  );
}

export default function PeopleSettingsPage() {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["entity-registry"],
    queryFn: fetchEntityRegistry,
  });

  const [drafts, setDrafts] = useState<DraftMap>({});
  const [errorMap, setErrorMap] = useState<Record<string, string>>({});

  // Seed drafts when server data first arrives, but never clobber a draft
  // the user is actively editing.
  useEffect(() => {
    if (!data?.entries) return;
    setDrafts((prev) => {
      const next = { ...prev };
      for (const entry of data.entries) {
        if (!(entry.placeholder in next)) {
          next[entry.placeholder] = entryToDraft(entry);
        }
      }
      // Drop drafts for entries that no longer exist server-side (deleted).
      const live = new Set(data.entries.map((e) => e.placeholder));
      for (const ph of Object.keys(next)) {
        if (!live.has(ph)) delete next[ph];
      }
      return next;
    });
  }, [data]);

  const updateMutation = useMutation({
    mutationFn: ({ placeholder, patch }: { placeholder: string; patch: Partial<EntityRegistryEntry> }) =>
      updateEntityRegistryEntry(placeholder, patch),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["entity-registry"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (placeholder: string) => deleteEntityRegistryEntry(placeholder),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["entity-registry"] });
    },
  });

  const handleFieldChange = (
    placeholder: string,
    field: "name" | "relationship" | "notes",
    value: string,
  ) => {
    setDrafts((prev) => ({
      ...prev,
      [placeholder]: { ...(prev[placeholder] ?? emptyDraft()), [field]: value },
    }));
  };

  const handleSave = async (entry: EntityRegistryEntry) => {
    const draft = drafts[entry.placeholder];
    if (!draft) return;
    setErrorMap((prev) => {
      const next = { ...prev };
      delete next[entry.placeholder];
      return next;
    });
    try {
      await updateMutation.mutateAsync({
        placeholder: entry.placeholder,
        patch: draft,
      });
    } catch (err) {
      setErrorMap((prev) => ({
        ...prev,
        [entry.placeholder]: err instanceof Error ? err.message : "Save failed",
      }));
    }
  };

  const handleCancel = (entry: EntityRegistryEntry) => {
    setDrafts((prev) => ({ ...prev, [entry.placeholder]: entryToDraft(entry) }));
    setErrorMap((prev) => {
      const next = { ...prev };
      delete next[entry.placeholder];
      return next;
    });
  };

  const handleDelete = async (entry: EntityRegistryEntry) => {
    const confirmed = window.confirm(
      `Delete the binding for ${entry.placeholder}? This will not rewrite past messages but stops future ones from using this name.`,
    );
    if (!confirmed) return;
    try {
      await deleteMutation.mutateAsync(entry.placeholder);
    } catch (err) {
      setErrorMap((prev) => ({
        ...prev,
        [entry.placeholder]: err instanceof Error ? err.message : "Delete failed",
      }));
    }
  };

  const entries = data?.entries ?? [];

  return (
    <div className="space-y-6">
      <SectionCard
        title="People your assistant knows"
        subtitle="When your assistant detects a name in a message, it tags it with a placeholder so the real name never leaves our servers in the form your AI provider sees. Edit a binding here if your assistant ever uses the wrong name, or add a relationship and notes so it can disambiguate “she” / “they” / “my coworker” more reliably."
      >
        {isLoading && (
          <div className="text-sm text-ink-muted" role="status">
            Loading…
          </div>
        )}

        {error && (
          <div
            role="alert"
            className="rounded-xl border border-rose-border bg-rose-bg px-4 py-2.5 text-sm text-rose-text"
          >
            Could not load the registry: {error instanceof Error ? error.message : "unknown error"}
          </div>
        )}

        {!isLoading && !error && entries.length === 0 && (
          <p className="text-sm text-ink-muted">
            No people are tracked yet. As you chat, your assistant will start populating this list.
          </p>
        )}

        {entries.length > 0 && (
          <div className="space-y-3">
            {entries.map((entry) => {
              const draft = drafts[entry.placeholder] ?? entryToDraft(entry);
              const dirty = !draftEquals(entry, draft);
              const rowError = errorMap[entry.placeholder];
              const isSaving =
                updateMutation.isPending &&
                updateMutation.variables?.placeholder === entry.placeholder;
              const isDeleting =
                deleteMutation.isPending && deleteMutation.variables === entry.placeholder;

              return (
                <div
                  key={entry.placeholder}
                  className="rounded-xl border border-border bg-surface/60 p-4 backdrop-blur-sm"
                >
                  <div className="mb-3 flex items-center justify-between gap-4">
                    <code className="font-mono text-xs uppercase tracking-[0.12em] text-ink-faint">
                      {entry.placeholder}
                    </code>
                    {entry.updated_at && (
                      <span className="text-[10px] uppercase tracking-[0.12em] text-ink-faint">
                        Updated {new Date(entry.updated_at).toLocaleDateString()}
                      </span>
                    )}
                  </div>

                  <div className="grid gap-3 sm:grid-cols-3">
                    <Field
                      label="Name"
                      value={draft.name}
                      onChange={(v) => handleFieldChange(entry.placeholder, "name", v)}
                      placeholder="e.g. Sarah Chen"
                    />
                    <Field
                      label="Relationship"
                      value={draft.relationship}
                      onChange={(v) => handleFieldChange(entry.placeholder, "relationship", v)}
                      placeholder="e.g. daughter, coworker"
                    />
                    <Field
                      label="Notes"
                      value={draft.notes}
                      onChange={(v) => handleFieldChange(entry.placeholder, "notes", v)}
                      placeholder="e.g. 4.5 years old, into Roblox"
                    />
                  </div>

                  {rowError && (
                    <div
                      role="alert"
                      className="mt-3 rounded-xl border border-rose-border bg-rose-bg px-3 py-2 text-xs text-rose-text"
                    >
                      {rowError}
                    </div>
                  )}

                  <div className="mt-3 flex items-center justify-end gap-2">
                    <button
                      type="button"
                      onClick={() => handleDelete(entry)}
                      disabled={isSaving || isDeleting}
                      className="rounded-lg border border-rose-border bg-transparent px-3 py-2 text-xs font-medium text-rose-text transition hover:bg-rose-bg disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
                    >
                      {isDeleting ? "Deleting…" : "Delete"}
                    </button>
                    {dirty && (
                      <button
                        type="button"
                        onClick={() => handleCancel(entry)}
                        disabled={isSaving}
                        className="rounded-lg border border-border bg-transparent px-3 py-2 text-xs text-ink-muted transition hover:bg-surface-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
                      >
                        Cancel
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => handleSave(entry)}
                      disabled={!dirty || isSaving || isDeleting}
                      className="glow-purple rounded-lg bg-accent px-4 py-2 text-xs font-semibold text-white transition hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
                    >
                      {isSaving ? "Saving…" : "Save"}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <p className="mt-6 text-xs text-ink-faint">
          Edits apply to future messages only. Past journal entries and notes that already contain
          the previous name aren’t rewritten.
        </p>
      </SectionCard>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="block">
      <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-white/40">
        {label}
      </span>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="mt-1 w-full rounded-xl border border-white/10 bg-white/[0.05] px-4 py-3 text-sm text-[#e0e3e8] outline-none placeholder:text-white/25 focus:border-[#5dd9d0]/50 focus:shadow-[0_0_8px_rgba(93,217,208,0.15)] transition"
      />
    </label>
  );
}
