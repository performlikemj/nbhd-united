"use client";

import { useState } from "react";
import clsx from "clsx";
import { useSidebarTreeQuery } from "@/lib/queries";
import type { SidebarSection } from "@/lib/types";

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

interface SidebarProps {
  activeKind?: string;
  activeSlug?: string;
  onNavigate: (kind: string, slug: string) => void;
  collapsed?: boolean;
  onToggle?: () => void;
}

export function Sidebar({ activeKind, activeSlug, onNavigate, collapsed, onToggle }: SidebarProps) {
  const { data: tree, isLoading } = useSidebarTreeQuery();
  const [expandedSections, setExpandedSections] = useState<Set<string>>(
    new Set(["daily", "weekly", "tasks", "goals", "ideas", "project", "memory"]),
  );

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
      <div className="flex flex-col items-center border-r border-border bg-surface/50 py-4">
        <button
          type="button"
          onClick={onToggle}
          className="rounded p-2 text-ink-faint hover:bg-surface-hover hover:text-ink"
          title="Expand sidebar"
        >
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
        </button>
      </div>
    );
  }

  // Static items that always show (even if no documents exist yet)
  const staticItems: Array<{ kind: string; slug: string; label: string; icon: string }> = [
    { kind: "daily", slug: new Date().toISOString().slice(0, 10), label: "Today", icon: "📅" },
    { kind: "tasks", slug: "tasks", label: "Tasks", icon: "📋" },
    { kind: "goal", slug: "goals", label: "Goals", icon: "🎯" },
    { kind: "ideas", slug: "ideas", label: "Ideas", icon: "💡" },
    { kind: "memory", slug: "memory", label: "Memory", icon: "🧠" },
  ];

  return (
    <div className="flex h-full w-64 flex-col border-r border-border bg-surface/50">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold text-ink-muted">Journal</h2>
        {onToggle && (
          <button
            type="button"
            onClick={onToggle}
            className="rounded p-1 text-ink-faint hover:bg-surface-hover hover:text-ink"
            title="Collapse sidebar"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
          </button>
        )}
      </div>

      {/* Quick nav */}
      <div className="border-b border-border px-2 py-2">
        {staticItems.map((item) => (
          <button
            key={`${item.kind}-${item.slug}`}
            type="button"
            onClick={() => onNavigate(item.kind, item.slug)}
            className={clsx(
              "flex w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left text-sm transition",
              activeKind === item.kind && activeSlug === item.slug
                ? "bg-border font-medium text-ink"
                : "text-ink-muted hover:bg-surface-hover hover:text-ink",
            )}
          >
            <span>{item.icon}</span>
            <span>{item.label}</span>
          </button>
        ))}
      </div>

      {/* File tree */}
      <div className="flex-1 overflow-y-auto px-2 py-2">
        {isLoading ? (
          <div className="space-y-2 px-3">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-4 animate-pulse rounded bg-border" />
            ))}
          </div>
        ) : (
          tree?.map((section: SidebarSection) => {
            // Skip singleton kinds in the tree (they're in quick nav)
            if (SINGLETON_KINDS.has(section.kind) && section.items.length <= 1) return null;

            const expanded = expandedSections.has(section.kind);
            const items = section.items;
            if (items.length === 0) return null;

            return (
              <div key={section.kind} className="mb-1">
                <button
                  type="button"
                  onClick={() => toggleSection(section.kind)}
                  className="flex w-full items-center gap-1 rounded px-3 py-1 text-xs font-medium uppercase tracking-wider text-ink-faint hover:text-ink-muted"
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
                {expanded && (
                  <div className="ml-2 mt-0.5">
                    {items.map((item) => (
                      <button
                        key={`${section.kind}-${item.slug}`}
                        type="button"
                        onClick={() => onNavigate(section.kind, item.slug)}
                        className={clsx(
                          "flex w-full items-center rounded-lg px-3 py-1 text-left text-sm transition",
                          activeKind === section.kind && activeSlug === item.slug
                            ? "bg-border font-medium text-ink"
                            : "text-ink-muted hover:bg-surface-hover hover:text-ink-muted",
                        )}
                      >
                        <span className="truncate">{item.title}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
