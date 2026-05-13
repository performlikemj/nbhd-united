"use client";

import clsx from "clsx";
import { type MouseEvent, type TouchEvent as ReactTouchEvent, useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { MarkdownRenderer } from "@/components/markdown-renderer";
import { MarkdownEditor, EditorToolbar } from "@/components/journal/markdown-editor";
import { type Editor } from "@tiptap/react";
import { MarkdownHelpSheet } from "@/components/journal/markdown-help-sheet";
import { QuickLogInput } from "@/components/journal/quick-log-input";
import { EmptyState } from "@/components/journal/empty-state";
import { DocumentHeader } from "@/components/journal/document-header";
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

interface DocumentViewProps {
  kind: string;
  slug: string;
  onNavigate?: (kind: string, slug: string) => void;
  onToggleSidebar?: () => void;
}

/** Stardust skeleton loader */
function StardustSkeleton() {
  return (
    <div className="space-y-4 p-4 lg:p-8">
      <div className="h-6 w-48 stardust-skeleton rounded-lg" />
      <div className="space-y-3 pt-2">
        {[85, 72, 90, 55, 78, 60].map((w, i) => (
          <div
            key={i}
            className="h-3 stardust-skeleton rounded"
            style={{
              width: `${w}%`,
              animationDelay: `${i * 0.15}s`,
            }}
          />
        ))}
      </div>
      <div className="pt-4 space-y-2">
        <div className="h-3 w-32 stardust-skeleton rounded" />
        <div className="h-3 w-96 stardust-skeleton rounded" />
      </div>
    </div>
  );
}

/** Floating pencil FAB for mobile */ 
function PencilFab({ onClick, style }: { onClick: () => void; style: React.CSSProperties }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="fixed right-5 z-40 rounded-full bg-accent text-white p-3.5 shadow-[0_4px_16px_rgba(124,107,240,0.35)] touch-none select-none transition active:scale-90 hover:shadow-[0_4px_20px_rgba(124,107,240,0.5)]"
      style={style}
      aria-label="Edit note"
    >
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
        <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5Z" />
      </svg>
    </button>
  );
}

export function DocumentView({ kind, slug, onNavigate, onToggleSidebar }: DocumentViewProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [helpOpen, setHelpOpen] = useState(false);
  const [savedIndicator, setSavedIndicator] = useState(false);
  const saveIndicatorTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const isMobile = useSyncExternalStore(
    (cb) => {
      const mq = window.matchMedia("(max-width: 767px)");
      mq.addEventListener("change", cb);
      return () => mq.removeEventListener("change", cb);
    },
    () => window.matchMedia("(max-width: 767px)").matches,
    () => null as boolean | null,
  );

  const isToday = slug === todayISO();

  const mobileEditorRef = useRef<Editor | null>(null);
  const [mobileEditor, setMobileEditor] = useState<Editor | null>(null);

  // Reset edit state when navigating to a different document
  /* eslint-disable react-hooks/set-state-in-effect -- intentional reset when props change */
  useEffect(() => {
    setEditing(false);
    setSavedIndicator(false);
    if (saveIndicatorTimer.current) clearTimeout(saveIndicatorTimer.current);
  }, [kind, slug]);

  const queryClient = useQueryClient();

  // Lock body scroll when mobile editor is open
  useEffect(() => {
    if (editing && isMobile === true) {
      const prevOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      const prevPB = document.body.style.paddingBottom;
      document.body.style.paddingBottom = "0px";
      return () => {
        document.body.style.overflow = prevOverflow;
        document.body.style.paddingBottom = prevPB;
      };
    }
  }, [editing, isMobile]);

  const { data: doc, isLoading, error } = useDocumentQuery(kind, slug);
  const updateMutation = useUpdateDocumentMutation();
  const appendMutation = useAppendDocumentMutation();

  // Prefetch previous day
  useEffect(() => {
    if (kind !== "daily") return;
    const prevSlug = shiftDate(slug, -1);
    void queryClient.prefetchQuery({
      queryKey: ["document", kind, prevSlug],
      queryFn: () => fetchDocument(kind, prevSlug),
    });
  }, [slug, kind, queryClient]);

  const handleEdit = () => {
    setDraft(doc?.markdown || "");
    setEditing(true);
  };

  const handleSave = async () => {
    await updateMutation.mutateAsync({
      kind,
      slug,
      data: { markdown: draft },
    });
    setEditing(false);
    setMobileEditor(null);
    mobileEditorRef.current = null;
    // Show saved indicator
    setSavedIndicator(true);
    if (saveIndicatorTimer.current) clearTimeout(saveIndicatorTimer.current);
    saveIndicatorTimer.current = setTimeout(() => setSavedIndicator(false), 2000);
  };

  const handleCancel = () => {
    setEditing(false);
    setMobileEditor(null);
    mobileEditorRef.current = null;
  };

  const handleQuickLog = async (content: string) => {
    await appendMutation.mutateAsync({
      kind,
      slug,
      content,
    });
  };

  const handleCheckboxToggle = useCallback(
    (lineIndex: number, checked: boolean) => {
      if (!doc?.markdown) return;
      const lines = doc.markdown.split("\n");
      const line = lines[lineIndex];
      if (!line) return;

      const newLine = checked
        ? line.replace(/\[([ ])\]/, "[x]")
        : line.replace(/\[([xX])\]/, "[ ]");

      const newMarkdown = lines
        .map((l, i) => (i === lineIndex ? newLine : l))
        .join("\n");
      updateMutation.mutate({
        kind,
        slug,
        data: { markdown: newMarkdown },
      });
    },
    [doc, kind, slug, updateMutation],
  );

  if (isLoading) {
    return <StardustSkeleton />;
  }

  if (error) {
    return (
      <div className="p-4 lg:p-8">
        <div className="rounded-2xl border border-rose-border bg-rose-bg p-5">
          <p className="text-sm font-medium text-rose-text">Could not load document.</p>
          <p className="mt-1 text-xs text-rose-text/70">Try refreshing or navigating to a different entry.</p>
        </div>
      </div>
    );
  }

  // Document doesn't exist yet
  if (!doc) {
    return (
      <div className="flex h-full flex-col">
        <DocumentHeader
          kind={kind}
          slug={slug}
          isToday={isToday}
          onNavigate={onNavigate ?? (() => {})}
          onEdit={handleEdit}
          editing={false}
          onSave={handleSave}
          onCancel={handleCancel}
          updatePending={updateMutation.isPending}
          isMobile={isMobile}
          showSavedIndicator={false}
          onToggleSidebar={onToggleSidebar}
        />
        <EmptyState />
      </div>
    );
  }

  // ── Mobile full-screen editing portal ──
  if (editing && isMobile === true) {
    return createPortal(
      <div
        className="fixed inset-0 z-[9999] flex flex-col bg-[#0B0F13]"
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        {/* Minimal top bar */}
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-white/[0.06] px-4 py-2.5">
          <span className="min-w-0 truncate text-sm font-semibold text-ink">
            {kind === "daily"
              ? new Date(slug + "T00:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })
              : (doc?.title ?? "Edit")}
          </span>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={handleSave}
              disabled={updateMutation.isPending}
              className="rounded-full bg-accent px-4 py-1.5 text-sm font-semibold text-white transition hover:brightness-110 disabled:opacity-50 min-h-[36px]"
            >
              {updateMutation.isPending ? "Saving…" : "Save"}
            </button>
            <button
              type="button"
              onClick={handleCancel}
              className="rounded-full border border-white/[0.08] px-4 py-1.5 text-sm text-ink-muted min-h-[36px] transition hover:bg-white/[0.03]"
            >
              Cancel
            </button>
          </div>
        </div>

        {/* Floating toolbar — slim, centered */}
        <div className="shrink-0 border-b border-white/[0.04] bg-white/[0.01] px-2 py-1">
          <EditorToolbar editor={mobileEditor} className="border-0 justify-center" />
        </div>

        {/* Scrollable editor */}
        <div
          className="flex-1 overflow-y-auto overscroll-y-contain"
          style={{
            WebkitOverflowScrolling: "touch",
          }}
        >
          <MarkdownEditor
            value={draft}
            onChange={setDraft}
            onSave={handleSave}
            onHelpToggle={() => setHelpOpen(true)}
            autoFocus
            cursorKey={`doc-cursor-${kind}-${slug}`}
            className="rounded-none border-0"
            hideToolbar
            onEditorReady={(ed) => {
              mobileEditorRef.current = ed;
              setMobileEditor(ed);
            }}
          />
        </div>

        {/* Pull-to-dismiss keyboard affordance */}
        <div className="shrink-0 h-7 flex items-center justify-center border-t border-white/[0.04]">
          <div className="h-1 w-8 rounded-full bg-white/[0.08]" />
        </div>

        <MarkdownHelpSheet open={helpOpen} onClose={() => setHelpOpen(false)} />
      </div>,
      document.body,
    );
  }

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <DocumentHeader
        kind={kind}
        slug={slug}
        isToday={isToday}
        onNavigate={onNavigate ?? (() => {})}
        onEdit={handleEdit}
        editing={editing}
        onSave={handleSave}
        onCancel={handleCancel}
        updatePending={updateMutation.isPending}
        isMobile={isMobile}
        showSavedIndicator={savedIndicator}
        onToggleSidebar={onToggleSidebar}
      />

      {/* Content */}
      <div className="flex-1 overflow-x-hidden overflow-y-auto">
        {editing && isMobile !== true ? (
          <div className="p-4 lg:p-8">
            {/* Premium writing surface */}
            <div className="rounded-2xl border border-white/[0.04] bg-white/[0.015] shadow-[inset_0_1px_2px_rgba(0,0,0,0.3)] overflow-hidden">
              <MarkdownEditor
                value={draft}
                onChange={setDraft}
                onSave={handleSave}
                onHelpToggle={() => setHelpOpen(true)}
                autoFocus
                cursorKey={`doc-cursor-${kind}-${slug}`}
                minRows={Math.max(20, draft.split("\n").length + 5)}
              />
            </div>
          </div>
        ) : (
          <div
            className={clsx(
              "bg-white/[0.01] transition-all duration-300",
              isMobile === true
                ? "p-4"
                : "group cursor-text rounded-2xl border border-white/[0.03] bg-white/[0.015] p-5 lg:p-8 m-4 lg:m-6 shadow-[inset_0_1px_2px_rgba(0,0,0,0.2)] hover:border-white/[0.06] hover:shadow-[inset_0_1px_3px_rgba(0,0,0,0.3)]",
            )}
            onClick={isMobile === true ? undefined : (e) => {
              const target = e.target as Element | null;
              if (target?.closest("input[type='checkbox']")) return;
              handleEdit();
            }}
          >
            {isMobile !== true && (
              <div className="pointer-events-none mb-4 flex items-center gap-2 text-[11px] uppercase tracking-[0.12em] text-ink-faint/50 opacity-0 group-hover:opacity-100 transition-opacity duration-300">
                <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                </svg>
                Tap anywhere to edit
              </div>
            )}
            {doc?.markdown ? (
              <MarkdownRenderer content={doc.markdown} onCheckboxToggle={handleCheckboxToggle} />
            ) : (
              <EmptyState
                title="This page is blank."
                subtitle={
                  isMobile === true
                    ? "Tap the pencil to start writing."
                    : "Tap anywhere to start writing..."
                }
              />
            )}
          </div>
        )}
      </div>

      {/* Mobile pencil FAB */}
      {isMobile === true && !editing && (
        <PencilFab
          onClick={handleEdit}
          style={{
            bottom: "calc(1.25rem + env(safe-area-inset-bottom) + 56px)",
          }}
        />
      )}

      {/* Quick log */}
      {kind === "daily" && !editing && (
        <div className="border-t border-white/[0.04] px-4 py-2.5 pb-[max(0.625rem,env(safe-area-inset-bottom))] lg:px-6 lg:py-3">
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
