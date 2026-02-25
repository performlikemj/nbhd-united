"use client";

import { type MouseEvent, type TouchEvent as ReactTouchEvent, useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MarkdownRenderer } from "@/components/markdown-renderer";
import { MarkdownEditor } from "@/components/journal/markdown-editor";
import { MarkdownHelpSheet } from "@/components/journal/markdown-help-sheet";
import { QuickLogInput } from "@/components/journal/quick-log-input";
import { useQueryClient } from "@tanstack/react-query";
import {
  useDocumentQuery,
  useUpdateDocumentMutation,
  useAppendDocumentMutation,
} from "@/lib/queries";
import { fetchDocument } from "@/lib/api";

function todayISO(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function shiftDate(dateStr: string, days: number): string {
  const [y, m, d] = dateStr.split("-").map(Number);
  const date = new Date(y, m - 1, d);
  date.setDate(date.getDate() + days);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function formatDate(dateStr: string): string {
  return new Date(dateStr + "T00:00:00").toLocaleDateString(undefined, {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

function formatDateShort(dateStr: string): string {
  return new Date(dateStr + "T00:00:00").toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

interface DocumentViewProps {
  kind: string;
  slug: string;
  onNavigate?: (kind: string, slug: string) => void;
}

export function DocumentView({ kind, slug, onNavigate }: DocumentViewProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [helpOpen, setHelpOpen] = useState(false);
  // null = not yet measured (avoids SSR/hydration mismatch)
  const [isMobile, setIsMobile] = useState<boolean | null>(null);

  // Draggable pencil FAB — Y position stored in localStorage
  const FAB_KEY = "pencil-fab-y";
  const [fabY, setFabY] = useState<number | null>(null);
  const fabDrag = useRef<{ startTouchY: number; startBtnY: number } | null>(null);

  useEffect(() => {
    const saved = localStorage.getItem(FAB_KEY);
    setFabY(saved ? parseInt(saved, 10) : window.innerHeight - 80 - 28);
  }, []);

  const handleFabTouchStart = useCallback((e: ReactTouchEvent) => {
    const touch = e.touches[0];
    fabDrag.current = { startTouchY: touch.clientY, startBtnY: fabY ?? window.innerHeight - 108 };
  }, [fabY]);

  const handleFabTouchMove = useCallback((e: ReactTouchEvent) => {
    if (!fabDrag.current) return;
    e.preventDefault();
    const delta = e.touches[0].clientY - fabDrag.current.startTouchY;
    const newY = Math.max(16, Math.min(window.innerHeight - 72, fabDrag.current.startBtnY + delta));
    setFabY(newY);
  }, []);

  const handleFabTouchEnd = useCallback(() => {
    if (fabY !== null) localStorage.setItem(FAB_KEY, String(fabY));
    fabDrag.current = null;
  }, [fabY]);

  const queryClient = useQueryClient();

  // Detect mobile viewport — runs client-side only
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 767px)");
    setIsMobile(mq.matches);
    const handler = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  // Lock body scroll + hide app shell header when mobile overlay is open.
  // iOS Safari's backdrop-filter creates a compositing layer that paints
  // above even z-[9999] portals — hiding the header is the only reliable fix.
  useEffect(() => {
    if (editing && isMobile === true) {
      const prevOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";

      // Hide the sticky app shell header so it can't bleed above the portal
      const appHeader = document.querySelector<HTMLElement>("header.sticky, header[class*='sticky']");
      const prevVisibility = appHeader ? appHeader.style.visibility : null;
      if (appHeader) appHeader.style.visibility = "hidden";

      return () => {
        document.body.style.overflow = prevOverflow;
        if (appHeader && prevVisibility !== null) appHeader.style.visibility = prevVisibility;
      };
    }
  }, [editing, isMobile]);

  const effectiveSlug = slug;
  const { data: doc, isLoading, error } = useDocumentQuery(kind, effectiveSlug);
  const updateMutation = useUpdateDocumentMutation();
  const appendMutation = useAppendDocumentMutation();

  useEffect(() => {
    if (kind !== "daily") return;

    // Only prefetch previous day (not next — that would create future documents)
    const prevSlug = shiftDate(effectiveSlug, -1);
    void queryClient.prefetchQuery({
      queryKey: ["document", kind, prevSlug],
      queryFn: () => fetchDocument(kind, prevSlug),
    });
  }, [effectiveSlug, kind, queryClient]);

  const handleEdit = () => {
    setDraft(doc?.markdown || "");
    setEditing(true);
  };

  const handleContentAreaClick = (e: MouseEvent<HTMLDivElement>) => {
    const target = e.target as Element | null;
    if (target?.closest("input[type='checkbox']")) {
      return;
    }

    handleEdit();
  };

  const handleSave = async () => {
    await updateMutation.mutateAsync({
      kind,
      slug: effectiveSlug,
      data: { markdown: draft },
    });
    setEditing(false);
  };

  const handleCancel = () => {
    setEditing(false);
  };

  const handleQuickLog = async (content: string) => {
    await appendMutation.mutateAsync({
      kind,
      slug: effectiveSlug,
      content,
    });
  };

  const handleCheckboxToggle = useCallback(
    (lineIndex: number, checked: boolean) => {
      if (!doc?.markdown) return;
      const lines = doc.markdown.split("\n");
      const line = lines[lineIndex];
      if (!line) return;

      if (checked) {
        lines[lineIndex] = line.replace(/\[([ ])\]/, "[x]");
      } else {
        lines[lineIndex] = line.replace(/\[([xX])\]/, "[ ]");
      }

      const newMarkdown = lines.join("\n");
      updateMutation.mutate({
        kind,
        slug: effectiveSlug,
        data: { markdown: newMarkdown },
      });
    },
    [doc?.markdown, kind, effectiveSlug, updateMutation],
  );

  const handleDateNav = (days: number) => {
    const newSlug = shiftDate(slug, days);
    onNavigate?.("daily", newSlug);
  };

  if (isLoading) {
    return (
      <div className="space-y-4 p-4 lg:p-6">
        <div className="h-8 w-48 animate-pulse rounded bg-border" />
        <div className="space-y-2">
          {[1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="h-4 animate-pulse rounded bg-border" style={{ width: `${60 + Math.random() * 30}%` }} />
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 lg:p-6">
        <p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">
          Could not load document.
        </p>
      </div>
    );
  }

  // Document doesn't exist yet (404 → null)
  if (!doc) {
    return (
      <div className="flex h-full flex-col">
        {/* Header with date nav */}
        {kind === "daily" && (
          <div className="flex items-center justify-between border-b border-border px-4 py-2 lg:px-6 lg:py-3">
            <div className="flex items-center gap-2">
              <button type="button" onClick={() => handleDateNav(-1)} className="rounded p-1 text-ink-faint hover:bg-surface-hover hover:text-ink" title="Previous day">
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" /></svg>
              </button>
              <h1 className="text-lg font-semibold text-ink">{formatDate(slug)}</h1>
              <button type="button" onClick={() => handleDateNav(1)} className="rounded p-1 text-ink-faint hover:bg-surface-hover hover:text-ink" title="Next day">
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" /></svg>
              </button>
            </div>
          </div>
        )}
        <div className="flex flex-1 items-center justify-center">
          <div className="text-center">
            <p className="text-lg text-ink-faint">No note yet</p>
            <p className="mt-1 text-sm text-ink-faint/60">Your assistant will create it when there is something to share.</p>
          </div>
        </div>
      </div>
    );
  }

  // Mobile full-screen editing — portal to document.body so it escapes the app
  // shell's stacking context entirely (nav bar z-index can't block it)
  if (editing && isMobile === true) {
    return createPortal(
      <div className="fixed inset-0 z-[9999] flex flex-col overflow-hidden bg-[var(--color-surface)]">
        {/* Top bar — shrink-0 so it never squishes */}
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-4 py-2">
          <span className="min-w-0 truncate text-sm font-semibold text-ink">
            {kind === "daily" ? formatDateShort(effectiveSlug) : (doc?.title ?? "Edit")}
          </span>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={handleSave}
              disabled={updateMutation.isPending}
              className="min-h-[36px] rounded-full bg-accent px-3 py-1.5 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55"
            >
              {updateMutation.isPending ? "..." : "Save"}
            </button>
            <button
              type="button"
              onClick={handleCancel}
              className="min-h-[36px] rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong"
            >
              Cancel
            </button>
          </div>
        </div>
        {/* Editor scroll area — 'scroll' (not 'auto') + overscroll-contain for iOS */}
        <div
          className="flex-1 overscroll-y-contain"
          style={{
            overflowY: "scroll",
            WebkitOverflowScrolling: "touch",
            paddingBottom: "calc(env(safe-area-inset-bottom) + 5rem)",
          }}
        >
          <MarkdownEditor
            value={draft}
            onChange={setDraft}
            onSave={handleSave}
            onHelpToggle={() => setHelpOpen(true)}
            autoFocus
            cursorKey={`doc-cursor-${kind}-${effectiveSlug}`}
            className="rounded-none border-0"
          />
        </div>
        <MarkdownHelpSheet open={helpOpen} onClose={() => setHelpOpen(false)} />
      </div>,
      document.body,
    );
  }

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2 lg:px-6 lg:py-3">
        <div className="flex items-center gap-2 min-w-0">
          {/* Date navigation for daily notes */}
          {kind === "daily" && (
            <div className="flex items-center gap-1.5 sm:gap-2">
              <button
                type="button"
                onClick={() => handleDateNav(-1)}
                aria-label="Previous day"
                className="rounded-full border border-border-strong px-2 py-1 text-sm hover:border-border-strong min-h-[44px] min-w-[44px] flex items-center justify-center"
              >
                ←
              </button>
              <label className="relative cursor-pointer min-w-0">
                <span className="text-base font-semibold text-ink sm:text-lg">
                  <span className="hidden sm:inline">{formatDate(effectiveSlug)}</span>
                  <span className="sm:hidden">{formatDateShort(effectiveSlug)}</span>
                </span>
                <input
                  type="date"
                  className="absolute inset-0 cursor-pointer opacity-0"
                  value={effectiveSlug}
                  onChange={(e) => {
                    if (e.target.value) {
                      onNavigate?.("daily", e.target.value);
                    }
                  }}
                />
              </label>
              <button
                type="button"
                onClick={() => handleDateNav(1)}
                disabled={effectiveSlug >= todayISO()}
                aria-label="Next day"
                className="rounded-full border border-border-strong px-2 py-1 text-sm hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-40 min-h-[44px] min-w-[44px] flex items-center justify-center"
              >
                →
              </button>
              {effectiveSlug !== todayISO() && (
                <button
                  type="button"
                  onClick={() => {
                    onNavigate?.("daily", todayISO());
                  }}
                  aria-label="Go to today"
                  className="rounded-full border border-border-strong px-2.5 py-1 text-xs sm:text-sm sm:px-3 hover:border-border-strong min-h-[44px]"
                >
                  Today
                </button>
              )}
            </div>
          )}

          {/* Title for non-daily docs */}
          {kind !== "daily" && (
            <h1 className="truncate text-base font-semibold text-ink sm:text-lg">{doc?.title}</h1>
          )}
        </div>

        {/* Edit/Save buttons — hidden on mobile when not editing (pencil FAB handles edit) */}
        {(isMobile !== true || editing) && (
          <div className="flex items-center gap-2">
            {editing ? (
              <>
                <button
                  type="button"
                  onClick={handleSave}
                  disabled={updateMutation.isPending}
                  className="rounded-full bg-accent px-3 py-1.5 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55 min-h-[44px]"
                >
                  {updateMutation.isPending ? "..." : "Save"}
                </button>
                <button
                  type="button"
                  onClick={handleCancel}
                  className="rounded-full border border-border-strong px-3 py-1.5 text-sm hover:border-border-strong min-h-[44px]"
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                onClick={handleEdit}
                className="rounded-full border border-border-strong px-3 py-1.5 text-sm text-ink-faint hover:border-border-strong hover:text-ink min-h-[44px]"
              >
                Edit
              </button>
            )}
          </div>
        )}
      </div>

      {/* Content — desktop edit inline, or view mode */}
      <div className="flex-1 overflow-x-hidden overflow-y-auto">
        {editing && isMobile !== true ? (
          <div className="p-4 lg:p-6">
            <MarkdownEditor
              value={draft}
              onChange={setDraft}
              onSave={handleSave}
              onHelpToggle={() => setHelpOpen(true)}
              autoFocus
              cursorKey={`doc-cursor-${kind}-${effectiveSlug}`}
              minRows={Math.max(20, (draft.split("\n").length + 5))}
            />
          </div>
        ) : (
          <div
            className={
              isMobile === true
                ? "p-4"
                : "group cursor-text rounded-sm border border-transparent p-4 transition-colors duration-150 hover:border-border hover:bg-surface-hover lg:p-6"
            }
            onClick={isMobile === true ? undefined : handleContentAreaClick}
          >
            {isMobile !== true && (
              <p className="pointer-events-none mb-2 flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-ink-faint">
                ✎ Tap anywhere to edit
              </p>
            )}
            {doc?.markdown ? (
              <MarkdownRenderer content={doc.markdown} onCheckboxToggle={handleCheckboxToggle} />
            ) : (
              <p className="text-sm italic text-ink-faint">
                {isMobile === true ? "Tap the pencil button to start writing..." : "Tap anywhere to start writing..."}
              </p>
            )}
          </div>
        )}
      </div>

      {/* Mobile floating edit button — draggable along Y axis */}
      {isMobile === true && !editing && fabY !== null && (
        <button
          type="button"
          onClick={handleEdit}
          onTouchStart={handleFabTouchStart}
          onTouchMove={handleFabTouchMove}
          onTouchEnd={handleFabTouchEnd}
          aria-label="Edit note — drag to reposition"
          className="fixed right-4 z-40 rounded-full bg-accent text-white p-3 shadow-lg touch-none select-none"
          style={{ top: `${fabY}px` }}
        >
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5Z" />
          </svg>
        </button>
      )}

      {/* Quick log for daily notes */}
      {kind === "daily" && !editing && (
        <div className="border-t border-border px-4 py-2.5 pb-[max(0.625rem,env(safe-area-inset-bottom))] lg:px-6 lg:py-3">
          <QuickLogInput
            onSubmit={handleQuickLog}
            isPending={appendMutation.isPending}
          />
          {appendMutation.isError && (
            <p className="mt-1 text-xs text-rose-text">Failed to add entry.</p>
          )}
        </div>
      )}
      <MarkdownHelpSheet open={helpOpen} onClose={() => setHelpOpen(false)} />
    </div>
  );
}
