"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import clsx from "clsx";
import { useQueryClient } from "@tanstack/react-query";
import { fetchDocument } from "@/lib/api";
import {
  useSidebarTreeQuery,
  useDeleteDocumentMutation,
  useClearDocumentMutation,
  useCreateDocumentMutation,
} from "@/lib/queries";
import { Toast, useToast } from "@/components/toast";
import type { SidebarSection } from "@/lib/types";

// ── Slugify helper ────────────────────────────────────────────────────
function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^a-z0-9-]/g, "")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

const KIND_ICONS: Record<string, string> = {
  daily: "📅",
  weekly: "📆",
  tasks: "📋",
  goals: "🎯",
  goal: "🎯",
  ideas: "💡",
  project: "📁",
  memory: "🧠",
};

const SINGLETON_KINDS = new Set(["tasks", "ideas", "memory"]);
const CLEARABLE_KINDS = new Set(["daily", "tasks", "ideas", "memory"]);

/**
 * Sidebar section kind → actual document kind.
 * The tree API returns "goals" as section kind but docs are stored as kind="goal".
 */
const SECTION_KIND_TO_DOC_KIND: Record<string, string> = {
  goals: "goal",
};

function docKind(sectionKind: string): string {
  return SECTION_KIND_TO_DOC_KIND[sectionKind] ?? sectionKind;
}

/** Kinds that support multiple user-created entries (and get a "+" button). */
const ADDABLE_DOC_KINDS = new Set(["goal", "project"]);

/** Primary nav items — the main document types */
const PRIMARY_NAV = [
  { kind: "daily", label: "Daily", icon: "📅" },
  { kind: "tasks", label: "Tasks", icon: "📋" },
  { kind: "ideas", label: "Ideas", icon: "💡" },
  { kind: "memory", label: "Memory", icon: "🧠" },
];

interface SidebarProps {
  activeKind?: string;
  activeSlug?: string;
  onNavigate: (kind: string, slug: string) => void;
  collapsed?: boolean;
  onToggle?: () => void;
}

export function Sidebar({ activeKind, activeSlug, onNavigate, collapsed, onToggle }: SidebarProps) {
  const queryClient = useQueryClient();
  const { data: tree, isLoading } = useSidebarTreeQuery();
  const [expandedSections, setExpandedSections] = useState<Set<string>>(
    new Set(["daily", "weekly", "tasks", "goals", "ideas", "project", "memory"]),
  );

  // ── Delete state ──────────────────────────────────────────────────
  const [deletingKey, setDeletingKey] = useState<string | null>(null);
  const deleteTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ── Add state ─────────────────────────────────────────────────────
  const [addingKind, setAddingKind] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const addInputRef = useRef<HTMLInputElement | null>(null);

  // ── Toast ─────────────────────────────────────────────────────────
  const [toast, showToast] = useToast();

  const deleteMutation = useDeleteDocumentMutation();
  const clearMutation = useClearDocumentMutation();
  const createMutation = useCreateDocumentMutation();

  // Clear delete timer on unmount
  useEffect(() => {
    return () => {
      if (deleteTimerRef.current) clearTimeout(deleteTimerRef.current);
    };
  }, []);

  // Auto-focus add input when it appears
  useEffect(() => {
    if (addingKind && addInputRef.current) {
      addInputRef.current.focus();
    }
  }, [addingKind]);

  const startDelete = useCallback((key: string) => {
    if (deleteTimerRef.current) clearTimeout(deleteTimerRef.current);
    setDeletingKey(key);
    deleteTimerRef.current = setTimeout(() => {
      setDeletingKey(null);
    }, 4000);
  }, []);

  const cancelDelete = useCallback(() => {
    if (deleteTimerRef.current) clearTimeout(deleteTimerRef.current);
    setDeletingKey(null);
  }, []);

  const confirmDelete = useCallback(
    (kind: string, slug: string) => {
      if (deleteTimerRef.current) clearTimeout(deleteTimerRef.current);
      setDeletingKey(null);
      deleteMutation.mutate(
        { kind, slug },
        {
          onSuccess: () => {
            showToast("Deleted", "success");
            if (activeKind === kind && activeSlug === slug) {
              const today = new Date();
              const todaySlug = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
              onNavigate("daily", todaySlug);
            }
          },
          onError: () => showToast("Delete failed", "error"),
        },
      );
    },
    [deleteMutation, showToast, activeKind, activeSlug, onNavigate],
  );

  const confirmClear = useCallback(
    (kind: string, slug: string) => {
      if (deleteTimerRef.current) clearTimeout(deleteTimerRef.current);
      setDeletingKey(null);
      clearMutation.mutate(
        { kind, slug },
        {
          onSuccess: () => showToast("Cleared", "success"),
          onError: () => showToast("Clear failed", "error"),
        },
      );
    },
    [clearMutation, showToast],
  );

  const openAdd = useCallback((kind: string) => {
    setAddingKind(kind);
    setNewName("");
  }, []);

  const cancelAdd = useCallback(() => {
    setAddingKind(null);
    setNewName("");
  }, []);

  const confirmAdd = useCallback(() => {
    if (!addingKind || !newName.trim()) return;
    const slug = slugify(newName.trim());
    if (!slug) return;
    createMutation.mutate(
      { kind: addingKind, slug, title: newName.trim(), markdown: "" },
      {
        onSuccess: (doc) => {
          setAddingKind(null);
          setNewName("");
          onNavigate(addingKind, doc.slug);
        },
        onError: () => showToast("Create failed", "error"),
      },
    );
  }, [addingKind, newName, createMutation, onNavigate, showToast]);

  const toggleSection = (kind: string) => {
    setExpandedSections((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) {
        next.delete(kind);
      } else {
        next.add(kind);
      }
      return next;
    });
  };

  if (collapsed) {
    return (
      <div className="flex flex-col items-center border-r border-white/5 bg-c-dark/80 backdrop-blur-2xl py-4">
        <button
          type="button"
          onClick={onToggle}
          className="rounded p-2 text-ink-faint hover:bg-surface-hover hover:text-ink min-h-[44px] min-w-[44px] flex items-center justify-center"
          aria-label="Expand sidebar"
        >
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
        </button>
      </div>
    );
  }

  const prefetchDocument = (kind: string, slug: string) => {
    void queryClient.prefetchQuery({
      queryKey: ["document", kind, slug],
      queryFn: () => fetchDocument(kind, slug),
    });
  };

  function todaySlug() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  // User documents: goals, projects, and other multi-doc sections from the tree
  const userDocSections = (tree ?? []).filter((section: SidebarSection) => {
    const dk = docKind(section.kind);
    // Show sections that have user-created docs or are addable
    return ADDABLE_DOC_KINDS.has(dk) || (!SINGLETON_KINDS.has(section.kind) && section.kind !== "daily" && section.items.length > 0);
  });

  return (
    <nav aria-label="Journal sidebar" className="flex h-full w-64 flex-col border-r border-white/5 bg-c-dark/80 backdrop-blur-2xl">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-white/5 px-4 py-5">
        <div>
          <h2 className="font-headline text-xl font-bold text-ink">Journal</h2>
          <p className="text-[10px] uppercase tracking-[0.15em] text-ink-faint">Celestial Sanctuary</p>
        </div>
        {onToggle && (
          <button
            type="button"
            onClick={onToggle}
            className="rounded p-1 text-ink-faint hover:bg-white/5 hover:text-ink min-h-[44px] min-w-[44px] flex items-center justify-center"
            aria-label="Collapse sidebar"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
          </button>
        )}
      </div>

      {/* New Entry button */}
      <div className="px-3 py-3">
        <button
          type="button"
          onClick={() => onNavigate("daily", todaySlug())}
          className="w-full rounded-xl bg-accent px-4 py-3 text-sm font-bold text-white flex items-center justify-center gap-2 transition-all hover:brightness-110 glow-purple"
        >
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
          </svg>
          New Entry
        </button>
      </div>

      {/* Primary nav */}
      <div className="px-2 py-1 space-y-0.5">
        {PRIMARY_NAV.map((item) => {
          const isActive = activeKind === item.kind || (item.kind === "daily" && activeKind === "daily");
          const slug = item.kind === "daily" ? todaySlug() : item.kind;
          return (
            <button
              key={item.kind}
              type="button"
              onMouseEnter={() => prefetchDocument(item.kind, slug)}
              onClick={() => onNavigate(item.kind, slug)}
              className={clsx(
                "flex w-full items-center gap-3 rounded-xl px-4 py-2.5 text-left text-sm transition min-h-[44px]",
                isActive
                  ? "bg-accent/10 font-semibold text-accent"
                  : "text-ink-faint hover:bg-white/5 hover:text-ink",
              )}
            >
              <span className="text-base">{item.icon}</span>
              <span>{item.label}</span>
            </button>
          );
        })}
      </div>

      {/* User documents (goals, projects, etc.) */}
      <div className="flex-1 overflow-y-auto px-2 py-2 border-t border-white/5 mt-1">
        {isLoading ? (
          <div className="space-y-2 px-3">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-4 animate-pulse rounded bg-border" />
            ))}
          </div>
        ) : (
          userDocSections.map((section: SidebarSection) => {
            const expanded = expandedSections.has(section.kind);
            const items = section.items;
            const sectionDocKind = docKind(section.kind);
            const isAddable = ADDABLE_DOC_KINDS.has(sectionDocKind);
            const isClearable = CLEARABLE_KINDS.has(section.kind);

            if (items.length === 0 && !isAddable) return null;

            return (
              <div key={section.kind} className="mb-1">
                {/* Section header */}
                <div className="flex items-center gap-0.5">
                  <button
                    type="button"
                    onClick={() => toggleSection(section.kind)}
                    className="flex flex-1 items-center gap-1 rounded px-3 py-1 text-xs font-medium uppercase tracking-wider text-ink-faint hover:text-ink-muted"
                  >
                    <svg
                      className={clsx("h-3 w-3 transition-transform", expanded && "rotate-90")}
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                    </svg>
                    <span>{KIND_ICONS[section.kind] || "📄"}</span>
                    <span>{section.label}</span>
                    <span className="ml-auto text-[10px] text-ink-faint">{items.length}</span>
                  </button>

                  {isAddable && (
                    <button
                      type="button"
                      title={`Add new ${section.label.replace(/s$/, "").toLowerCase()}`}
                      onClick={() => openAdd(sectionDocKind)}
                      className="flex h-[28px] w-[28px] flex-shrink-0 items-center justify-center rounded text-ink-faint hover:bg-white/5 hover:text-ink"
                    >
                      <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
                      </svg>
                    </button>
                  )}
                </div>

                {/* Inline add input */}
                {isAddable && addingKind === sectionDocKind && (
                  <div className="ml-2 mt-1 flex items-center gap-1 px-1">
                    <input
                      ref={addInputRef}
                      type="text"
                      value={newName}
                      onChange={(e) => setNewName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") confirmAdd();
                        if (e.key === "Escape") cancelAdd();
                      }}
                      onBlur={(e) => {
                        if (!e.currentTarget.parentElement?.contains(e.relatedTarget as Node)) {
                          cancelAdd();
                        }
                      }}
                      placeholder="Name..."
                      className="min-w-0 flex-1 rounded border border-border bg-surface px-2 py-1 text-xs text-ink placeholder-ink-faint focus:outline-none focus:ring-1 focus:ring-accent"
                    />
                    <button
                      type="button"
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={confirmAdd}
                      disabled={!newName.trim() || createMutation.isPending}
                      className="flex h-[28px] min-w-[40px] items-center justify-center rounded bg-emerald-600 px-2 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
                    >
                      {createMutation.isPending ? "…" : "Save"}
                    </button>
                  </div>
                )}

                {/* Document list */}
                {expanded && (
                  <div className="ml-2 mt-0.5">
                    {items.map((item) => {
                      const itemKey = `${sectionDocKind}-${item.slug}`;
                      const isConfirming = deletingKey === itemKey;

                      return (
                        <div
                          key={itemKey}
                          className={clsx(
                            "group relative flex items-center rounded-lg transition-colors",
                            isConfirming && "bg-rose-500/10",
                          )}
                        >
                          <button
                            type="button"
                            onMouseEnter={() => prefetchDocument(sectionDocKind, item.slug)}
                            onClick={() => {
                              if (!isConfirming) onNavigate(sectionDocKind, item.slug);
                            }}
                            className={clsx(
                              "flex min-h-[36px] flex-1 items-center px-3 py-1 text-left text-sm transition",
                              activeKind === sectionDocKind && activeSlug === item.slug
                                ? "font-medium text-ink"
                                : "text-ink-muted hover:text-ink",
                            )}
                          >
                            <span className="truncate">{item.title}</span>
                          </button>

                          {isConfirming ? (
                            <div className="flex flex-shrink-0 items-center gap-0.5 pr-1">
                              <span className="mr-0.5 text-[10px] text-rose-400">{isClearable ? "Clear?" : "Delete?"}</span>
                              <button
                                type="button"
                                onClick={() => isClearable ? confirmClear(sectionDocKind, item.slug) : confirmDelete(sectionDocKind, item.slug)}
                                className="flex h-[28px] w-[28px] items-center justify-center rounded text-emerald-500 hover:bg-emerald-500/10"
                                title={isClearable ? "Confirm clear" : "Confirm delete"}
                              >
                                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                                </svg>
                              </button>
                              <button
                                type="button"
                                onClick={cancelDelete}
                                className="flex h-[28px] w-[28px] items-center justify-center rounded text-ink-faint hover:bg-surface-hover"
                                title="Cancel"
                              >
                                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                                </svg>
                              </button>
                            </div>
                          ) : (
                            <button
                              type="button"
                              onClick={(e) => {
                                e.stopPropagation();
                                startDelete(itemKey);
                              }}
                              className="flex h-[36px] w-[36px] flex-shrink-0 items-center justify-center rounded text-ink-faint opacity-100 transition hover:text-rose-400 md:opacity-0 md:group-hover:opacity-100"
                              title={isClearable ? "Clear" : "Delete"}
                            >
                              {isClearable ? (
                                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4l16 16M9 9l-4.5 4.5a2.121 2.121 0 000 3L7 19h5l1-1M15 9l3.5 3.5a2.121 2.121 0 010 3L15 19" />
                                </svg>
                              ) : (
                                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                  <path
                                    strokeLinecap="round"
                                    strokeLinejoin="round"
                                    strokeWidth={2}
                                    d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
                                  />
                                </svg>
                              )}
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>

      {/* Toast notifications */}
      {toast && (
        <Toast
          key={toast.id}
          message={toast.message}
          type={toast.type}
          onDismiss={() => {
            /* Toast self-dismisses via its own timer */
          }}
        />
      )}
    </nav>
  );
}
