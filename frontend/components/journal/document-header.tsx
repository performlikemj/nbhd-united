"use client";

import { useState, useEffect } from "react";
import clsx from "clsx";
import { useDocumentQuery, useUpdateDocumentMutation } from "@/lib/queries";

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

export function formatDate(dateStr: string): string {
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

interface DocumentHeaderProps {
  kind: string;
  slug: string;
  isToday: boolean;
  onNavigate: (kind: string, slug: string) => void;
  onEdit: () => void;
  editing: boolean;
  onSave: () => void;
  onCancel: () => void;
  updatePending: boolean;
  isMobile: boolean | null;
  showSavedIndicator?: boolean;
  onToggleSidebar?: () => void;
  /** Typed-lifecycle docs (tasks/goal) are managed via typed records; hide the
   *  markdown Edit affordance so users aren't offered a write the backend 409s. */
  readOnly?: boolean;
}

export function DocumentHeader({
  kind,
  slug,
  isToday,
  onNavigate,
  onEdit,
  editing,
  onSave,
  onCancel,
  updatePending,
  isMobile,
  showSavedIndicator,
  onToggleSidebar,
  readOnly,
}: DocumentHeaderProps) {
  const { data: doc } = useDocumentQuery(kind, slug);
  const handleDateNav = (days: number) => {
    onNavigate("daily", shiftDate(slug, days));
  };

  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.04] px-3 py-2.5 sm:px-4 sm:py-3 lg:px-6 lg:py-4">
      <div className="flex items-center gap-2 sm:gap-3 min-w-0 flex-1">
        {/* Sidebar toggle — integrated into header on mobile */}
        {onToggleSidebar && (
          <button
            type="button"
            onClick={onToggleSidebar}
            className="shrink-0 flex items-center justify-center rounded-xl border border-white/[0.06] bg-white/[0.02] text-ink-faint transition hover:bg-white/[0.04] hover:text-ink active:scale-95 min-h-[44px] min-w-[44px] lg:hidden"
            aria-label="Open sidebar"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h10M4 18h16" />
            </svg>
          </button>
        )}

        {/* Date navigation for daily notes */}
        {kind === "daily" && (
          <div className="flex items-center gap-1.5 sm:gap-2">
            {/* Previous day */}
            <button
              type="button"
              onClick={() => handleDateNav(-1)}
              aria-label="Previous day"
              className="group rounded-xl border border-white/[0.06] bg-white/[0.02] px-2.5 py-2 text-ink-faint transition hover:text-ink hover:bg-white/[0.04] hover:border-white/[0.12] active:scale-[0.97] min-h-[44px] min-w-[44px] flex items-center justify-center"
            >
              <svg className="h-4 w-4 transition-transform group-hover:-translate-x-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
              </svg>
            </button>

            {/* Date display */}
            <label className="relative cursor-pointer min-w-0 text-center">
              {/* Today indicator */}
              {isToday && (
                <div className="flex items-center justify-center gap-1.5 text-signal-text text-[10px] font-semibold uppercase tracking-[0.12em] mb-1">
                  <svg viewBox="0 0 16 16" fill="currentColor" className="h-2.5 w-2.5">
                    <path d="M8 0L9 6l5-4-2.5 3.5L16 8l-4.5-.5L13 13l-3-2.5L6 13l1.5-5.5L3 8l4.5-2.5L5 2l4 4z" />
                  </svg>
                  Current Orbit
                </div>
              )}
              <span className="font-display text-lg font-bold text-ink sm:text-2xl tracking-tight block">
                <span className="hidden sm:inline">{formatDate(slug)}</span>
                <span className="sm:hidden">{formatDateShort(slug)}</span>
              </span>
              <input
                type="date"
                className="absolute inset-0 cursor-pointer opacity-0"
                value={slug}
                max={todayISO()}
                onChange={(e) => {
                  if (e.target.value) {
                    onNavigate("daily", e.target.value);
                  }
                }}
              />
            </label>

            {/* Next day */}
            <button
              type="button"
              onClick={() => handleDateNav(1)}
              disabled={slug >= todayISO()}
              aria-label="Next day"
              className="group rounded-xl border border-white/[0.06] bg-white/[0.02] px-2.5 py-2 text-ink-faint transition hover:text-ink hover:bg-white/[0.04] hover:border-white/[0.12] active:scale-[0.97] min-h-[44px] min-w-[44px] flex items-center justify-center disabled:opacity-30 disabled:cursor-not-allowed"
            >
              <svg className="h-4 w-4 transition-transform group-hover:translate-x-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
              </svg>
            </button>

            {/* Go to today */}
            {!isToday && (
              <button
                type="button"
                onClick={() => onNavigate("daily", todayISO())}
                aria-label="Go to today"
                className="rounded-xl border border-signal/20 bg-signal-faint px-3 py-2 text-xs font-medium text-signal-text transition hover:bg-signal-faint/50 active:scale-95 min-h-[44px]"
              >
                Today
              </button>
            )}

            {/* Saved indicator */}
            {showSavedIndicator && (
              <span className="saved-pill flex items-center gap-1 text-xs text-signal-text/70 ml-1">
                <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
                Saved
              </span>
            )}
          </div>
        )}

        {/* Title for non-daily docs */}
        {kind !== "daily" && (
          <h1 className="truncate font-display text-base text-ink sm:text-lg">{doc?.title}</h1>
        )}
      </div>

      {/* Kind badge — visible on all screen sizes for context */}
      <div className="hidden sm:flex items-center">
        <span className="rounded-full border border-white/[0.06] bg-white/[0.02] px-2.5 py-1 text-[10px] font-medium uppercase tracking-[0.12em] text-ink-faint">
          {kind}
        </span>
      </div>

      {/* Edit/Save buttons — hidden on mobile when not editing (pencil FAB handles edit) */}
      {(isMobile !== true || editing) && (
        <div className="flex items-center gap-2">
          {editing ? (
            <>
              <button
                type="button"
                onClick={onSave}
                disabled={updatePending}
                className="rounded-full bg-accent px-3 py-1.5 text-sm font-medium text-white transition hover:bg-accent/85 disabled:opacity-55 min-h-[44px]"
              >
                {updatePending ? "..." : "Save"}
              </button>
              <button
                type="button"
                onClick={onCancel}
                className="rounded-full border border-white/[0.08] px-3 py-1.5 text-sm text-ink-muted transition hover:border-white/[0.15] hover:text-ink min-h-[44px]"
              >
                Cancel
              </button>
            </>
          ) : readOnly ? null : (
            <button
              type="button"
              onClick={onEdit}
              className="rounded-full border border-white/[0.08] px-3 py-1.5 text-sm text-ink-faint transition hover:border-white/[0.15] hover:text-ink min-h-[44px]"
            >
              Edit
            </button>
          )}
        </div>
      )}
    </div>
  );
}
