"use client";

import { useState, useCallback } from "react";
import { MarkdownRenderer } from "@/components/markdown-renderer";
import { MarkdownEditor } from "@/components/journal/markdown-editor";
import { MarkdownHelpSheet } from "@/components/journal/markdown-help-sheet";
import { QuickLogInput } from "@/components/journal/quick-log-input";
import {
  useDocumentQuery,
  useUpdateDocumentMutation,
  useAppendDocumentMutation,
} from "@/lib/queries";

function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

function shiftDate(dateStr: string, days: number): string {
  const d = new Date(dateStr + "T00:00:00");
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
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
  const [currentSlug, setCurrentSlug] = useState(slug);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [helpOpen, setHelpOpen] = useState(false);

  const effectiveSlug = kind === "daily" ? currentSlug : slug;
  const { data: doc, isLoading, error } = useDocumentQuery(kind, effectiveSlug);
  const updateMutation = useUpdateDocumentMutation();
  const appendMutation = useAppendDocumentMutation();

  // Update currentSlug when slug prop changes
  if (kind === "daily" && slug !== currentSlug && slug !== currentSlug) {
    setCurrentSlug(slug);
  }

  const handleEdit = () => {
    setDraft(doc?.markdown || "");
    setEditing(true);
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
    const newSlug = shiftDate(currentSlug, days);
    setCurrentSlug(newSlug);
    onNavigate?.("daily", newSlug);
  };

  if (isLoading) {
    return (
      <div className="space-y-4 p-4 lg:p-6">
        <div className="h-8 w-48 animate-pulse rounded bg-ink/10" />
        <div className="space-y-2">
          {[1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="h-4 animate-pulse rounded bg-ink/10" style={{ width: `${60 + Math.random() * 30}%` }} />
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 lg:p-6">
        <p className="rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
          Could not load document.
        </p>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink/10 px-4 py-2 lg:px-6 lg:py-3">
        <div className="flex items-center gap-2 min-w-0">
          {/* Date navigation for daily notes */}
          {kind === "daily" && (
            <div className="flex items-center gap-1.5 sm:gap-2">
              <button
                type="button"
                onClick={() => handleDateNav(-1)}
                className="rounded-full border border-ink/20 px-2 py-1 text-sm hover:border-ink/40 min-h-[36px] min-w-[36px] flex items-center justify-center"
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
                      setCurrentSlug(e.target.value);
                      onNavigate?.("daily", e.target.value);
                    }
                  }}
                />
              </label>
              <button
                type="button"
                onClick={() => handleDateNav(1)}
                disabled={effectiveSlug >= todayISO()}
                className="rounded-full border border-ink/20 px-2 py-1 text-sm hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-40 min-h-[36px] min-w-[36px] flex items-center justify-center"
              >
                →
              </button>
              {effectiveSlug !== todayISO() && (
                <button
                  type="button"
                  onClick={() => {
                    setCurrentSlug(todayISO());
                    onNavigate?.("daily", todayISO());
                  }}
                  className="rounded-full border border-ink/20 px-2.5 py-1 text-xs sm:text-sm sm:px-3 hover:border-ink/40 min-h-[36px]"
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

        {/* Edit/Save buttons */}
        <div className="flex items-center gap-2">
          {editing ? (
            <>
              <button
                type="button"
                onClick={handleSave}
                disabled={updateMutation.isPending}
                className="rounded-full bg-accent px-3 py-1.5 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55 min-h-[36px]"
              >
                {updateMutation.isPending ? "..." : "Save"}
              </button>
              <button
                type="button"
                onClick={handleCancel}
                className="rounded-full border border-ink/20 px-3 py-1.5 text-sm hover:border-ink/40 min-h-[36px]"
              >
                Cancel
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={handleEdit}
              className="rounded-full border border-ink/20 px-3 py-1.5 text-sm text-ink/50 hover:border-ink/40 hover:text-ink min-h-[36px]"
            >
              Edit
            </button>
          )}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {editing ? (
          <div className="p-4 lg:p-6">
            <MarkdownEditor
              value={draft}
              onChange={setDraft}
              onSave={handleSave}
              onHelpToggle={() => setHelpOpen(true)}
              autoFocus
              minRows={Math.max(20, (draft.split("\n").length + 5))}
            />
          </div>
        ) : (
          <div className="p-4 lg:p-6">
            {doc?.markdown ? (
              <MarkdownRenderer content={doc.markdown} onCheckboxToggle={handleCheckboxToggle} />
            ) : (
              <p className="text-sm italic text-ink/40">Empty document. Click Edit to start writing.</p>
            )}
          </div>
        )}
      </div>

      {/* Quick log for daily notes */}
      {kind === "daily" && !editing && (
        <div className="border-t border-ink/10 px-4 py-2.5 pb-[max(0.625rem,env(safe-area-inset-bottom))] lg:px-6 lg:py-3">
          <QuickLogInput
            onSubmit={handleQuickLog}
            isPending={appendMutation.isPending}
          />
          {appendMutation.isError && (
            <p className="mt-1 text-xs text-rose-600">Failed to add entry.</p>
          )}
        </div>
      )}
      <MarkdownHelpSheet open={helpOpen} onClose={() => setHelpOpen(false)} />
    </div>
  );
}
