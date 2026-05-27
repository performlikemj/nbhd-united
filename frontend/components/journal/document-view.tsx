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

/** Inline pencil button for the mobile bottom dock */
function PencilButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label="Edit note"
      className="shrink-0 rounded-full bg-accent text-white transition active:scale-90 hover:brightness-110 shadow-[0_2px_8px_rgba(124,107,240,0.25)] min-h-[44px] min-w-[44px] flex items-center justify-center"
    >
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
        <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5Z" />
      </svg>
    </button>
  );
}

type SaveStatus = "idle" | "saving" | "saved";

function SaveStatusIndicator({ status }: { status: SaveStatus }) {
  return (
    <span
      className="flex shrink-0 items-center gap-1 text-xs text-ink-faint min-w-[64px] justify-end"
      aria-live="polite"
    >
      {status === "saving" && <span>Saving…</span>}
      {status === "saved" && (
        <>
          <svg className="h-3 w-3 text-signal-text" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
          </svg>
          <span className="text-signal-text/80">Saved</span>
        </>
      )}
    </span>
  );
}

export function DocumentView({ kind, slug, onNavigate, onToggleSidebar }: DocumentViewProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [helpOpen, setHelpOpen] = useState(false);
  const [savedIndicator, setSavedIndicator] = useState(false);
  const saveIndicatorTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>("idle");
  const lastSavedRef = useRef<string>("");
  const portalRef = useRef<HTMLDivElement | null>(null);

  // Align with the app shell's mobile tab bar (`lg:hidden` = max-width: 1023px).
  // Below this width the sidebar collapses and the tab bar shows, so the journal
  // also needs its mobile chrome (fixed-bottom action bar, full-screen editor).
  const isMobile = useSyncExternalStore(
    (cb) => {
      const mq = window.matchMedia("(max-width: 1023px)");
      mq.addEventListener("change", cb);
      return () => mq.removeEventListener("change", cb);
    },
    () => window.matchMedia("(max-width: 1023px)").matches,
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

  // Track the visual viewport while the mobile editor is open so the dock
  // sits directly above the on-screen keyboard. iOS Safari doesn't shrink the
  // layout viewport when the keyboard opens, so fixed-bottom elements end up
  // hidden behind the keyboard — resize the portal to vv.height instead.
  useEffect(() => {
    if (!(editing && isMobile === true)) return;
    if (typeof window === "undefined" || !window.visualViewport) return;
    const vv = window.visualViewport;
    const update = () => {
      const el = portalRef.current;
      if (!el) return;
      el.style.height = `${vv.height}px`;
      el.style.transform = `translateY(${vv.offsetTop}px)`;
    };
    update();
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    return () => {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
    };
  }, [editing, isMobile]);

  // Autosave on mobile: debounced write 1.5s after the last keystroke.
  useEffect(() => {
    if (!(editing && isMobile === true)) return;
    if (draft === lastSavedRef.current) return;
    const payload = draft;
    const t = setTimeout(() => {
      setSaveStatus("saving");
      updateMutation.mutate(
        { kind, slug, data: { markdown: payload } },
        {
          onSuccess: () => {
            lastSavedRef.current = payload;
            setSaveStatus("saved");
          },
          onError: () => setSaveStatus("idle"),
        },
      );
    }, 1500);
    return () => clearTimeout(t);
    // updateMutation is stable from React Query; intentionally excluded.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, editing, isMobile, kind, slug]);

  // Fade the "Saved" pip back to idle after 2s.
  useEffect(() => {
    if (saveStatus !== "saved") return;
    const t = setTimeout(() => setSaveStatus("idle"), 2000);
    return () => clearTimeout(t);
  }, [saveStatus]);

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
    const initial = doc?.markdown || "";
    setDraft(initial);
    lastSavedRef.current = initial;
    setSaveStatus("idle");
    setEditing(true);
  };

  // Mobile "Done" — flush any pending diff, then exit. Stays in editor on
  // failure so unsaved keystrokes aren't silently lost.
  const handleDone = async () => {
    try {
      if (draft !== lastSavedRef.current) {
        setSaveStatus("saving");
        await updateMutation.mutateAsync({ kind, slug, data: { markdown: draft } });
        lastSavedRef.current = draft;
      }
      setEditing(false);
      setMobileEditor(null);
      mobileEditorRef.current = null;
      setSaveStatus("idle");
    } catch {
      setSaveStatus("idle");
    }
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

  // Note: a missing `doc` is handled inline by the main render path below —
  // the PATCH endpoint upserts, so the editor still works on a blank day.

  // ── Mobile full-screen editing portal ──
  if (editing && isMobile === true) {
    return createPortal(
      <div
        ref={portalRef}
        className="fixed left-0 top-0 right-0 z-[9999] flex flex-col bg-[#0B0F13]"
        style={{ height: "100dvh" }}
      >
        {/* Top bar — context only, no destination-action buttons */}
        <div className="flex shrink-0 items-center gap-2 border-b border-white/[0.06] px-2 py-2">
          <button
            type="button"
            onClick={handleDone}
            disabled={updateMutation.isPending}
            aria-label="Close editor"
            className="flex shrink-0 items-center justify-center rounded-xl p-2 text-ink-faint hover:bg-white/[0.04] hover:text-ink transition min-h-[40px] min-w-[40px] disabled:opacity-50"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
          <span className="min-w-0 flex-1 truncate text-sm font-semibold text-ink">
            {kind === "daily"
              ? new Date(slug + "T00:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })
              : (doc?.title ?? "Edit")}
          </span>
          <SaveStatusIndicator status={saveStatus} />
        </div>

        {/* Scrollable editor */}
        <div
          className="flex-1 overflow-y-auto overscroll-y-contain"
          style={{ WebkitOverflowScrolling: "touch" }}
        >
          <MarkdownEditor
            value={draft}
            onChange={setDraft}
            onSave={handleDone}
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

        {/* Dock — formatting + Done, pinned above the keyboard */}
        <div className="shrink-0 border-t border-white/[0.06] bg-[#0B0F13]/95 backdrop-blur-xl">
          <div className="flex items-center gap-1 px-1 py-1.5">
            <EditorToolbar editor={mobileEditor} className="min-w-0 flex-1 border-0" />
            <button
              type="button"
              onMouseDown={(e) => e.preventDefault()}
              onClick={handleDone}
              disabled={updateMutation.isPending}
              className="shrink-0 rounded-full bg-accent px-4 py-2 text-sm font-semibold text-white transition hover:brightness-110 disabled:opacity-50 min-h-[40px]"
            >
              Done
            </button>
          </div>
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

      {/* Mobile bottom action bar — quick-log + open-editor in one row. */}
      {isMobile === true && !editing && (
        <div className="shrink-0 border-t border-white/[0.04] px-3 py-2 pb-[max(0.5rem,env(safe-area-inset-bottom))]">
          <div className="flex items-center gap-2">
            {kind === "daily" ? (
              <div className="min-w-0 flex-1">
                <QuickLogInput
                  onSubmit={handleQuickLog}
                  isPending={appendMutation.isPending}
                />
              </div>
            ) : (
              <div className="flex-1" />
            )}
            <PencilButton onClick={handleEdit} />
          </div>
          {appendMutation.isError && (
            <p className="mt-1 text-xs text-rose-text">Failed to add entry.</p>
          )}
        </div>
      )}

      {/* Desktop quick log (daily only) */}
      {kind === "daily" && !editing && isMobile !== true && (
        <div className="border-t border-white/[0.04] px-4 py-2.5 lg:px-6 lg:py-3">
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
