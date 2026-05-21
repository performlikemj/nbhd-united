"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { SectionCard } from "@/components/section-card";
import {
  type EntityRegistryEntry,
  type PIIDenylistEntry,
  addPIIDenylistEntry,
  bulkAddPIIDenylistEntries,
  deleteEntityRegistryEntry,
  fetchEntityRegistry,
  fetchPIIDenylist,
  removePIIDenylistEntry,
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

// An entry is "curated" once the user has filled in either a relationship
// or notes. That's the signal the user has actually claimed this entry —
// vs. the long tail of NER auto-detections sitting at name-only.
function isCurated(entry: EntityRegistryEntry): boolean {
  return Boolean(entry.relationship?.trim()) || Boolean(entry.notes?.trim());
}

function matchesSearch(entry: EntityRegistryEntry, query: string): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  return (
    entry.placeholder.toLowerCase().includes(q) ||
    (entry.name ?? "").toLowerCase().includes(q) ||
    (entry.relationship ?? "").toLowerCase().includes(q) ||
    (entry.notes ?? "").toLowerCase().includes(q)
  );
}

export default function PeopleSettingsPage() {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["entity-registry"],
    queryFn: fetchEntityRegistry,
  });
  const denylistQuery = useQuery({
    queryKey: ["pii-denylist"],
    queryFn: fetchPIIDenylist,
  });

  const [drafts, setDrafts] = useState<DraftMap>({});
  const [errorMap, setErrorMap] = useState<Record<string, string>>({});
  const [search, setSearch] = useState("");
  // Default ON. Tenants commonly accumulate hundreds of NER auto-detections
  // before curating any; showing them all by default makes the page unusable
  // (the 826-entry canary case). Users can toggle off to audit the full set.
  const [showOnlyCurated, setShowOnlyCurated] = useState(true);
  // Multi-select: placeholder strings of rows the user has ticked for
  // bulk Ignore. Cleared on a successful bulk submission.
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [bulkError, setBulkError] = useState<string | null>(null);

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

  // Denylist: adding marks an entry's name as "not PII for me" — the
  // redactor stops substituting placeholders for it on detection AND
  // stops the existing entity_map entry from driving the Step 1 regex
  // pass. The entity_map row stays so historical refs rehydrate.
  const addDenyMutation = useMutation({
    mutationFn: (name: string) => addPIIDenylistEntry(name),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["pii-denylist"] });
    },
  });

  const removeDenyMutation = useMutation({
    mutationFn: (key: string) => removePIIDenylistEntry(key),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["pii-denylist"] });
    },
  });

  const bulkDenyMutation = useMutation({
    mutationFn: (names: string[]) => bulkAddPIIDenylistEntries(names),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["pii-denylist"] });
      setSelectedKeys(new Set());
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

  const handleIgnore = async (entry: EntityRegistryEntry) => {
    const name = entry.name?.trim();
    if (!name) return;
    try {
      await addDenyMutation.mutateAsync(name);
    } catch (err) {
      setErrorMap((prev) => ({
        ...prev,
        [entry.placeholder]: err instanceof Error ? err.message : "Could not ignore",
      }));
    }
  };

  const handleUndoIgnore = async (deny: PIIDenylistEntry) => {
    try {
      await removeDenyMutation.mutateAsync(deny.key);
    } catch {
      // The denylist section has its own error region below; surface
      // an alert as a low-effort fallback since this is a rare path.
      window.alert("Could not re-enable redaction for this word.");
    }
  };

  const toggleSelect = (placeholder: string) => {
    setSelectedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(placeholder)) next.delete(placeholder);
      else next.add(placeholder);
      return next;
    });
  };

  const handleBulkIgnore = async () => {
    setBulkError(null);
    const selectedEntries = entries.filter(
      (e) => selectedKeys.has(e.placeholder) && (e.name?.trim() ?? "") !== "",
    );
    if (selectedEntries.length === 0) return;
    const names = selectedEntries.map((e) => e.name.trim());
    try {
      const result = await bulkDenyMutation.mutateAsync(names);
      if (result.skipped.length > 0) {
        setBulkError(
          `Ignored ${result.added.length} entries; skipped ${result.skipped.length} (empty or invalid).`,
        );
      }
    } catch (err) {
      setBulkError(err instanceof Error ? err.message : "Bulk ignore failed");
    }
  };

  const entries = useMemo(() => data?.entries ?? [], [data]);

  const filteredEntries = useMemo(() => {
    return entries.filter((entry) => {
      if (showOnlyCurated && !isCurated(entry)) return false;
      if (!matchesSearch(entry, search)) return false;
      return true;
    });
  }, [entries, showOnlyCurated, search]);

  const curatedCount = useMemo(() => entries.filter(isCurated).length, [entries]);

  const denylistEntries = useMemo(
    () => denylistQuery.data?.entries ?? [],
    [denylistQuery.data],
  );
  const denylistKeys = useMemo(
    () => new Set(denylistEntries.map((d) => d.key)),
    [denylistEntries],
  );

  // Selection helpers — operate over the currently visible rows so
  // "Select all" only ticks what's on screen given current filters.
  const visiblePlaceholders = useMemo(
    () => filteredEntries.map((e) => e.placeholder),
    [filteredEntries],
  );
  const selectedVisibleCount = useMemo(
    () => visiblePlaceholders.filter((ph) => selectedKeys.has(ph)).length,
    [visiblePlaceholders, selectedKeys],
  );
  const allVisibleSelected =
    visiblePlaceholders.length > 0 && selectedVisibleCount === visiblePlaceholders.length;
  const someVisibleSelected =
    selectedVisibleCount > 0 && selectedVisibleCount < visiblePlaceholders.length;

  const toggleSelectAllVisible = () => {
    setSelectedKeys((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) {
        for (const ph of visiblePlaceholders) next.delete(ph);
      } else {
        for (const ph of visiblePlaceholders) next.add(ph);
      }
      return next;
    });
  };

  const clearSelection = () => setSelectedKeys(new Set());

  return (
    <div className="space-y-6">
      <div
        className="rounded-xl border border-accent/20 bg-accent/[0.06] px-4 py-3 text-sm text-ink-muted"
        role="note"
      >
        <p>
          <span className="font-semibold text-ink">This is a privacy feature.</span>{" "}
          We swap out personal details with placeholders before sending your messages to AI models, then put the
          real values back when the model replies. Sometimes the detector mis-identifies an ordinary word as a name —
          you can help your experience by occasionally marking those entries as “Ignore.”
        </p>
      </div>

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
          <>
            <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex flex-1 items-center gap-3">
                <input
                  type="search"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search name, relationship, notes…"
                  className="w-full max-w-sm rounded-xl border border-white/10 bg-white/[0.05] px-4 py-2.5 text-sm text-[#e0e3e8] outline-none placeholder:text-white/25 focus:border-[#5dd9d0]/50 focus:shadow-[0_0_8px_rgba(93,217,208,0.15)] transition min-h-[44px]"
                />
                <label className="flex shrink-0 items-center gap-2 text-xs text-ink-muted cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={showOnlyCurated}
                    onChange={(e) => setShowOnlyCurated(e.target.checked)}
                    className="h-4 w-4 rounded border-white/20 bg-white/[0.05] accent-accent"
                  />
                  Show only curated
                </label>
              </div>
              <p className="text-xs text-ink-faint shrink-0">
                Showing {filteredEntries.length} of {entries.length}
                {showOnlyCurated && curatedCount < entries.length && (
                  <span> · {entries.length - curatedCount} hidden auto-detections</span>
                )}
              </p>
            </div>

            {filteredEntries.length === 0 && (
              <p className="text-sm text-ink-muted">
                No entries match.
                {showOnlyCurated && (
                  <>
                    {" "}
                    Try unchecking{" "}
                    <button
                      type="button"
                      onClick={() => setShowOnlyCurated(false)}
                      className="underline text-accent hover:text-accent-hover"
                    >
                      “Show only curated”
                    </button>{" "}
                    to see auto-detected entries.
                  </>
                )}
                {!showOnlyCurated && search && (
                  <>
                    {" "}
                    <button
                      type="button"
                      onClick={() => setSearch("")}
                      className="underline text-accent hover:text-accent-hover"
                    >
                      Clear search
                    </button>
                    .
                  </>
                )}
              </p>
            )}

            {/* Select-all-visible toggle, anchored above the row list so it
                relates to whatever the current filter shows. Sticky bulk
                action bar (below) shows when anything is selected. */}
            <div className="mb-2 flex items-center justify-between gap-3 px-1 text-xs text-ink-muted">
              <label className="inline-flex items-center gap-2 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={allVisibleSelected}
                  ref={(el) => {
                    if (el) el.indeterminate = someVisibleSelected;
                  }}
                  onChange={toggleSelectAllVisible}
                  disabled={visiblePlaceholders.length === 0}
                  className="h-4 w-4 rounded border-white/20 bg-white/[0.05] accent-accent disabled:cursor-not-allowed disabled:opacity-50"
                  aria-label={
                    allVisibleSelected
                      ? `Deselect all ${visiblePlaceholders.length} visible entries`
                      : `Select all ${visiblePlaceholders.length} visible entries`
                  }
                />
                Select all visible
              </label>
              {selectedKeys.size > 0 && (
                <span className="text-ink-faint">{selectedKeys.size} selected total</span>
              )}
            </div>

            {selectedKeys.size > 0 && (
              <div
                className="sticky top-2 z-10 mb-3 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-accent/30 bg-accent/10 px-4 py-3 backdrop-blur-md"
                role="region"
                aria-label="Bulk actions"
              >
                <p className="text-sm text-ink">
                  <span className="font-semibold">{selectedKeys.size}</span> selected
                </p>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={clearSelection}
                    disabled={bulkDenyMutation.isPending}
                    className="rounded-lg border border-border bg-transparent px-3 py-2 text-xs text-ink-muted transition hover:bg-surface-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
                  >
                    Clear selection
                  </button>
                  <button
                    type="button"
                    onClick={handleBulkIgnore}
                    disabled={bulkDenyMutation.isPending}
                    className="glow-purple rounded-lg bg-accent px-4 py-2 text-xs font-semibold text-white transition hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
                  >
                    {bulkDenyMutation.isPending
                      ? "Ignoring…"
                      : `Ignore ${selectedKeys.size} selected`}
                  </button>
                </div>
                {bulkError && (
                  <p className="w-full text-xs text-ink-muted" role="status">
                    {bulkError}
                  </p>
                )}
              </div>
            )}

            <div className="space-y-3">
            {filteredEntries.map((entry) => {
              const draft = drafts[entry.placeholder] ?? entryToDraft(entry);
              const dirty = !draftEquals(entry, draft);
              const rowError = errorMap[entry.placeholder];
              const isSaving =
                updateMutation.isPending &&
                updateMutation.variables?.placeholder === entry.placeholder;
              const isDeleting =
                deleteMutation.isPending && deleteMutation.variables === entry.placeholder;
              const isIgnoring =
                addDenyMutation.isPending && addDenyMutation.variables === entry.name?.trim();
              const alreadyIgnored = (() => {
                const n = entry.name?.trim();
                if (!n) return false;
                return denylistKeys.has(n.toLowerCase());
              })();

              const isSelected = selectedKeys.has(entry.placeholder);

              return (
                <div
                  key={entry.placeholder}
                  className="rounded-xl border border-border bg-surface/60 p-4 backdrop-blur-sm"
                >
                  <div className="mb-3 flex items-center justify-between gap-4">
                    <div className="flex items-center gap-3">
                      <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={() => toggleSelect(entry.placeholder)}
                        aria-label={`Select ${entry.placeholder}`}
                        className="h-4 w-4 rounded border-white/20 bg-white/[0.05] accent-accent cursor-pointer"
                      />
                      <code className="font-mono text-xs uppercase tracking-[0.12em] text-ink-faint">
                        {entry.placeholder}
                      </code>
                    </div>
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
                    {alreadyIgnored ? (
                      <span className="text-xs text-ink-faint">Ignored — won’t be redacted</span>
                    ) : (
                      <button
                        type="button"
                        onClick={() => handleIgnore(entry)}
                        disabled={isSaving || isDeleting || isIgnoring || !entry.name?.trim()}
                        title="Mark this entry as not personal — future messages with this word won’t be redacted, and existing references still resolve."
                        className="rounded-lg border border-border bg-transparent px-3 py-2 text-xs text-ink-muted transition hover:bg-surface-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
                      >
                        {isIgnoring ? "Ignoring…" : "Ignore"}
                      </button>
                    )}
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
          </>
        )}

        <p className="mt-6 text-xs text-ink-faint">
          Edits apply to future messages only. Past journal entries and notes that already contain
          the previous name aren’t rewritten.
        </p>
      </SectionCard>

      <SectionCard
        title="Ignored words"
        subtitle="Words you’ve marked as not personal. They won’t be redacted in future messages, but existing references in your history still resolve. Re-enable redaction if you change your mind."
      >
        {denylistQuery.isLoading && (
          <div className="text-sm text-ink-muted" role="status">
            Loading…
          </div>
        )}
        {denylistQuery.error && (
          <div
            role="alert"
            className="rounded-xl border border-rose-border bg-rose-bg px-4 py-2.5 text-sm text-rose-text"
          >
            Could not load the ignored list:{" "}
            {denylistQuery.error instanceof Error ? denylistQuery.error.message : "unknown error"}
          </div>
        )}
        {!denylistQuery.isLoading && !denylistQuery.error && denylistEntries.length === 0 && (
          <p className="text-sm text-ink-muted">
            Nothing ignored yet. Use the “Ignore” button above on any entry that isn’t actually a person.
          </p>
        )}
        {denylistEntries.length > 0 && (
          <ul className="space-y-2">
            {denylistEntries.map((deny) => {
              const isRemoving =
                removeDenyMutation.isPending && removeDenyMutation.variables === deny.key;
              return (
                <li
                  key={deny.key}
                  className="flex items-center justify-between gap-4 rounded-xl border border-border bg-surface/60 px-4 py-3 backdrop-blur-sm"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm text-ink">{deny.key}</p>
                    {deny.decided_at && (
                      <p className="text-[10px] uppercase tracking-[0.12em] text-ink-faint">
                        Ignored {new Date(deny.decided_at).toLocaleDateString()}
                      </p>
                    )}
                  </div>
                  <button
                    type="button"
                    onClick={() => handleUndoIgnore(deny)}
                    disabled={isRemoving}
                    className="rounded-lg border border-border bg-transparent px-3 py-2 text-xs text-ink-muted transition hover:bg-surface-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
                  >
                    {isRemoving ? "Re-enabling…" : "Re-enable redaction"}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
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
