"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import clsx from "clsx";
import { useQueryClient } from "@tanstack/react-query";
import type { SidebarSection } from "@/lib/types";
import { fetchDocument } from "@/lib/api";
import {
  useSidebarTreeQuery,
  useDeleteDocumentMutation,
  useClearDocumentMutation,
  useCreateDocumentMutation,
} from "@/lib/queries";
import { ConfirmDialog } from "./confirm-dialog";
import {
  IconDaily,
  IconTasks,
  IconIdeas,
  IconMemory,
  IconGoals,
  IconProjects,
  IconStarPlus,
  IconDocument,
  IconMore,
} from "@/components/icons/constellation";

// ── Slugify helper ────────────────────────────────────────────────────
function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^a-z0-9-]/g, "")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

const SINGLETON_KINDS = new Set(["tasks", "ideas", "memory"]);
const CLEARABLE_KINDS = new Set(["daily", "tasks", "ideas", "memory"]);

const SECTION_KIND_TO_DOC_KIND: Record<string, string> = {
  goals: "goal",
};

function docKind(sectionKind: string): string {
  return SECTION_KIND_TO_DOC_KIND[sectionKind] ?? sectionKind;
}

const ADDABLE_DOC_KINDS = new Set(["goal", "project"]);

interface SidebarProps {
  activeKind?: string;
  activeSlug?: string;
  onNavigate: (kind: string, slug: string) => void;
  collapsed?: boolean;
  onToggle?: () => void;
  /** Recent entries (daily) to show in a dedicated section */
  recentEntries?: Array<{ slug: string; title: string }>;
}

const PRIMARY_NAV = [
  { kind: "daily", label: "Daily", icon: IconDaily },
  { kind: "tasks", label: "Tasks", icon: IconTasks },
  { kind: "ideas", label: "Ideas", icon: IconIdeas },
  { kind: "memory", label: "Memory", icon: IconMemory },
];

function SectionIcon({ kind }: { kind: string }) {
  switch (kind) {
    case "goals": return <IconGoals className="h-3.5 w-3.5" />;
    case "projects": return <IconProjects className="h-3.5 w-3.5" />;
    default: return <IconDocument className="h-3.5 w-3.5" />;
  }
}

export function Sidebar({ activeKind, activeSlug, onNavigate, collapsed, onToggle, recentEntries }: SidebarProps) {
  const queryClient = useQueryClient();
  const { data: tree, isLoading } = useSidebarTreeQuery();
  const [expandedSections, setExpandedSections] = useState<Set<string>>(
    new Set(["daily", "weekly", "tasks", "goals", "ideas", "project", "memory"]),
  );

  // ── Confirm dialog state ────────────────────────────────────────────
  const [confirmDialog, setConfirmDialog] = useState<{
    open: boolean;
    title: string;
    message: string;
    action: () => void;
  }>({ open: false, title: "", message: "", action: () => {} });

  // ── Item actions ▸ menu ──────────────────────────────────────────────
  const [menuOpenFor, setMenuOpenFor] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handler(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpenFor(null);
      }
    }
    if (menuOpenFor) {
      document.addEventListener("mousedown", handler);
      return () => document.removeEventListener("mousedown", handler);
    }
  }, [menuOpenFor]);

  // ── Add state ───────────────────────────────────────────────────────
  const [addingKind, setAddingKind] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const addInputRef = useRef<HTMLInputElement | null>(null);

  const deleteMutation = useDeleteDocumentMutation();
  const clearMutation = useClearDocumentMutation();
  const createMutation = useCreateDocumentMutation();

  // Auto-focus add input
  useEffect(() => {
    if (addingKind && addInputRef.current) {
      addInputRef.current.focus();
    }
  }, [addingKind]);

  function todaySlug(): string {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  const handleDelete = useCallback((kind: string, slug: string) => {
    setConfirmDialog({
      open: true,
      title: "Delete entry?",
      message: "This cannot be undone. The entry will be permanently removed.",
      action: () => {
        deleteMutation.mutate(
          { kind, slug },
          {
            onSuccess: () => {
              if (activeKind === kind && activeSlug === slug) {
                const today = todaySlug();
                onNavigate("daily", today);
              }
            },
          },
        );
        setConfirmDialog((prev) => ({ ...prev, open: false }));
      },
    });
  }, [deleteMutation, activeKind, activeSlug, onNavigate]);

  const handleClear = useCallback((kind: string, slug: string) => {
    setConfirmDialog({
      open: true,
      title: "Clear entry?",
      message: "This will erase all content but keep the entry itself.",
      action: () => {
        clearMutation.mutate({ kind, slug });
        setConfirmDialog((prev) => ({ ...prev, open: false }));
      },
    });
  }, [clearMutation]);

  const startAdd = useCallback((kind: string) => {
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
      },
    );
  }, [addingKind, newName, createMutation, onNavigate]);

  const toggleSection = (kind: string) => {
    setExpandedSections((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  };

  if (collapsed) {
    return (
      <div className="flex flex-col items-center border-r border-white/[0.03] bg-[#0B0F13]/80 backdrop-blur-2xl py-4 w-12">
        <button
          type="button"
          onClick={onToggle}
          className="rounded-xl p-2 text-ink-faint hover:bg-white/[0.04] hover:text-ink min-h-[44px] min-w-[44px] flex items-center justify-center transition"
          aria-label="Expand sidebar"
        >
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 5l7 7-7 7" />
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

  // User document sections
  const userDocSections = (tree ?? []).filter((section: SidebarSection) => {
    const dk = docKind(section.kind);
    return ADDABLE_DOC_KINDS.has(dk) || (!SINGLETON_KINDS.has(section.kind) && section.kind !== "daily" && section.items.length > 0);
  });

  return (
    <nav aria-label="Journal sidebar" className="flex h-full w-full lg:w-[15rem] flex-col border-r border-white/[0.03] bg-[#0B0F13]/80 backdrop-blur-2xl">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-white/[0.04] px-4 py-4">
        <div>
          <h2 className="font-headline text-lg font-semibold text-ink tracking-tight">Journal</h2>
          <p className="text-[10px] uppercase tracking-[0.15em] text-ink-faint mt-0.5">Celestial Sanctuary</p>
        </div>
        {onToggle && (
          <button
            type="button"
            onClick={onToggle}
            className="rounded-xl p-1.5 text-ink-faint hover:bg-white/[0.04] hover:text-ink min-h-[36px] min-w-[36px] flex items-center justify-center transition"
            aria-label="Collapse sidebar"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
            </svg>
          </button>
        )}
      </div>

      {/* New Entry */}
      <div className="px-3 py-3">
        <button
          type="button"
          onClick={() => onNavigate("daily", todaySlug())}
          className="group w-full rounded-xl bg-gradient-to-r from-accent/20 to-[#7C6BF0]/10 border border-accent/20 px-4 py-3 text-sm font-semibold text-ink flex items-center justify-center gap-2.5 transition hover:brightness-110 hover:border-accent/35 hover:shadow-[0_0_20px_rgba(124,107,240,0.15)]"
        >
          <IconStarPlus className="h-4 w-4 text-accent transition group-hover:rotate-12" />
          New Entry
        </button>
      </div>

      {/* Primary nav */}
      <div className="px-2 py-1 space-y-0.5">
        {PRIMARY_NAV.map((item) => {
          const isActive = activeKind === item.kind;
          const Icon = item.icon;
          const slug = item.kind === "daily" ? todaySlug() : item.kind;
          return (
            <button
              key={item.kind}
              type="button"
              onMouseEnter={() => prefetchDocument(item.kind, slug)}
              onFocus={() => prefetchDocument(item.kind, slug)}
              onClick={() => onNavigate(item.kind, slug)}
              className={clsx(
                "flex w-full items-center gap-2.5 rounded-xl px-3 py-2 text-left text-[13px] transition-all duration-200 min-h-[44px]",
                isActive
                  ? "active-glow-left bg-accent/[0.06] font-medium text-accent"
                  : "text-ink-faint hover:bg-white/[0.03] hover:text-ink-muted",
              )}
            >
              <Icon className="h-[18px] w-[18px] shrink-0" />
              <span>{item.label}</span>
            </button>
          );
        })}
      </div>

      {/* Scrollable content: Recent + user documents */}
      <div className="flex-1 overflow-y-auto px-2 py-2 border-t border-white/[0.03] mt-1.5 custom-scrollbar">
        {recentEntries && recentEntries.length > 0 && (
          <div className="px-1 pb-3 mb-2 border-b border-white/[0.03]">
            <p className="text-[9px] font-bold uppercase tracking-[0.2em] text-ink-faint mb-2 px-1.5">
              Recent
            </p>
            <div className="space-y-0.5">
              {recentEntries.map((entry) => {
                const isActive = activeKind === "daily" && activeSlug === entry.slug;
                return (
                  <button
                    key={entry.slug}
                    type="button"
                    onClick={() => onNavigate("daily", entry.slug)}
                    onMouseEnter={() => prefetchDocument("daily", entry.slug)}
                    className={clsx(
                      "w-full rounded-lg px-2.5 py-1.5 text-left text-[12px] transition min-h-[32px] truncate",
                      isActive
                        ? "text-accent bg-accent/[0.05]"
                        : "text-ink-faint/70 hover:bg-white/[0.03] hover:text-ink-muted",
                    )}
                  >
                    {entry.title}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {isLoading ? (
          <div className="space-y-2 px-3 py-2">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-3 stardust-skeleton rounded" style={{ width: `${60 + i * 15}%` }} />
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
              <div key={section.kind} className="mb-0.5">
                {/* Section header — accordion folder style */}
                <div className="flex items-center">
                  <button
                    type="button"
                    onClick={() => toggleSection(section.kind)}
                    className="flex flex-1 items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[11px] font-semibold uppercase tracking-[0.12em] text-ink-faint/60 hover:text-ink-muted transition"
                  >
                    <svg
                      className={clsx("h-3 w-3 transition-transform duration-200", expanded && "rotate-90")}
                      fill="none"
                      viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                    </svg>
                    <SectionIcon kind={section.kind} />
                    <span>{section.label}</span>
                    {items.length > 0 && (
                      <span className="ml-auto text-[10px] text-ink-faint/40 tabular-nums">{items.length}</span>
                    )}
                  </button>

                  {isAddable && (
                    <button
                      type="button"
                      title={`Add new ${section.label.replace(/s$/, "").toLowerCase()}`}
                      onClick={() => startAdd(sectionDocKind)}
                      className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-ink-faint/50 hover:bg-white/[0.04] hover:text-ink-muted transition"
                    >
                      <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
                      </svg>
                    </button>
                  )}
                </div>

                {/* Inline add */}
                {isAddable && addingKind === sectionDocKind && (
                  <div className="ml-2 mt-1 flex items-center gap-1.5 px-1">
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
                          setTimeout(cancelAdd, 150);
                        }
                      }}
                      placeholder="Name..."
                      className="min-w-0 flex-1 rounded-lg border border-white/[0.06] bg-white/[0.02] px-2.5 py-1.5 text-[12px] text-ink placeholder-ink-faint focus:outline-none focus:border-accent/30 focus:ring-1 focus:ring-accent/10 transition"
                    />
                    <button
                      type="button"
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={confirmAdd}
                      disabled={!newName.trim() || createMutation.isPending}
                      className="flex h-7 min-w-[44px] items-center justify-center rounded-lg bg-accent/15 px-2 text-[11px] font-medium text-accent hover:bg-accent/25 disabled:opacity-40 transition"
                    >
                      {createMutation.isPending ? "…" : "Add"}
                    </button>
                  </div>
                )}

                {/* Document list */}
                {expanded && (
                  <div className="mt-0.5 space-y-0.5">
                    {items.map((item) => {
                      const itemKey = `${sectionDocKind}-${item.slug}`;
                      const isActive = activeKind === sectionDocKind && activeSlug === item.slug;
                      const isMenuOpen = menuOpenFor === itemKey;

                      return (
                        <div key={itemKey} className="group relative">
                          <button
                            type="button"
                            onMouseEnter={() => prefetchDocument(sectionDocKind, item.slug)}
                            onFocus={() => prefetchDocument(sectionDocKind, item.slug)}
                            onClick={() => onNavigate(sectionDocKind, item.slug)}
                            className={clsx(
                              "flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-left text-[13px] transition min-h-[36px]",
                              isActive
                                ? "active-glow-left text-ink font-medium"
                                : "text-ink-faint/60 hover:bg-white/[0.03] hover:text-ink-muted",
                            )}
                          >
                            <span className="truncate flex-1 min-w-0 pr-1">{item.title}</span>
                            {/* More menu button — always visible on touch, group-hover on desktop */}
                            <span
                              className={clsx(
                                "shrink-0 inline-flex items-center justify-center h-6 w-6 rounded-md transition",
                                isMenuOpen
                                  ? "bg-white/[0.06] text-ink-muted"
                                  : "text-ink-faint/30 hover:text-ink-faint hover:bg-white/[0.04]",
                                "md:opacity-0 md:group-hover:opacity-100",
                              )}
                              onClick={(e) => {
                                e.stopPropagation();
                                setMenuOpenFor(isMenuOpen ? null : itemKey);
                              }}
                            >
                              <IconMore className="h-3.5 w-3.5" />
                            </span>
                          </button>

                          {/* Action dropdown */}
                          {isMenuOpen && (
                            <div
                              ref={menuRef}
                              className="absolute right-0 top-full mt-0.5 z-20 min-w-[8rem] rounded-xl border border-white/[0.06] bg-[#111720] shadow-[0_8px_24px_rgba(0,0,0,0.4)] p-1"
                            >
                              {isClearable && (
                                <button
                                  type="button"
                                  onClick={() => {
                                    setMenuOpenFor(null);
                                    handleClear(sectionDocKind, item.slug);
                                  }}
                                  className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[12px] text-ink-muted hover:bg-white/[0.04] hover:text-ink transition"
                                >
                                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                                    <path strokeLinecap="round" strokeLinejoin="round" d="M4 4l16 16M9 9l-4.5 4.5a2.121 2.121 0 000 3L7 19h5l1-1M15 9l3.5 3.5a2.121 2.121 0 010 3L15 19" />
                                  </svg>
                                  Clear
                                </button>
                              )}
                              <button
                                type="button"
                                onClick={() => {
                                  setMenuOpenFor(null);
                                  handleDelete(sectionDocKind, item.slug);
                                }}
                                className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[12px] text-rose-text/70 hover:bg-rose-500/10 hover:text-rose-text transition"
                              >
                                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                                </svg>
                                Delete
                              </button>
                            </div>
                          )}
                        </div>
                      );
                    })}

                    {items.length === 0 && isAddable && (
                      <p className="px-3 py-2 text-[11px] text-ink-faint/30 italic">
                        No {section.label.toLowerCase()} yet
                      </p>
                    )}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>

      {/* Stardust empty state when nothing loaded yet */}
      {!isLoading && userDocSections.length === 0 && (
        <div className="px-4 py-6 text-center">
          <div className="flex justify-center gap-1 mb-2">
            {[0, 0.2, 0.4].map((delay) => (
              <span
                key={delay}
                className="h-1.5 w-1.5 rounded-full bg-accent/30 stardust-dot"
                style={{ animationDelay: `${delay}s` }}
              />
            ))}
          </div>
          <p className="text-[11px] text-ink-faint/30 leading-relaxed">
            Your universe is taking shape.
            <br />
            Create your first goal or project to begin.
          </p>
        </div>
      )}

      {/* Confirm dialog */}
      <ConfirmDialog
        open={confirmDialog.open}
        title={confirmDialog.title}
        message={confirmDialog.message}
        onConfirm={confirmDialog.action}
        onCancel={() => setConfirmDialog((prev) => ({ ...prev, open: false }))}
      />
    </nav>
  );
}
