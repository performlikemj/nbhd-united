"use client";

import { useState, useRef, useCallback } from "react";
import clsx from "clsx";
import { Sidebar } from "@/components/journal/sidebar";
import { DocumentView } from "@/components/journal/document-view";
import { useSidebarTreeQuery } from "@/lib/queries";

function todayISO(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function formatEntryDate(dateStr: string): string {
  const d = new Date(dateStr + "T00:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export default function JournalPage() {
  const [activeKind, setActiveKind] = useState(() => {
    if (typeof window === "undefined") return "daily";
    const hash = window.location.hash.slice(1);
    if (hash) {
      const parts = hash.split("/");
      if (parts.length >= 1) return parts[0];
    }
    return "daily";
  });
  const [activeSlug, setActiveSlug] = useState(() => {
    if (typeof window === "undefined") return todayISO();
    const hash = window.location.hash.slice(1);
    if (hash) {
      const parts = hash.split("/");
      if (parts.length >= 2) return parts.slice(1).join("/");
      if (parts.length === 1) return parts[0];
    }
    return todayISO();
  });
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  // Recent entries from sidebar tree
  const { data: tree } = useSidebarTreeQuery();
  const dailyEntries = (tree ?? []).find((s) => s.kind === "daily")?.items ?? [];

  // Draggable sidebar FAB — persists Y position in localStorage
  const SIDEBAR_FAB_KEY = "sidebar-fab-y";
  const [fabY, setFabY] = useState<number | null>(() => {
    if (typeof window === "undefined") return null;
    const saved = localStorage.getItem(SIDEBAR_FAB_KEY);
    return saved ? parseInt(saved, 10) : window.innerHeight - 120;
  });
  const fabDrag = useRef<{ startTouchY: number; startBtnY: number; moved: boolean } | null>(null);

  const handleFabTouchStart = useCallback((e: React.TouchEvent) => {
    const touch = e.touches[0];
    fabDrag.current = { startTouchY: touch.clientY, startBtnY: fabY ?? window.innerHeight - 120, moved: false };
  }, [fabY]);

  const handleFabTouchMove = useCallback((e: React.TouchEvent) => {
    if (!fabDrag.current) return;
    const delta = e.touches[0].clientY - fabDrag.current.startTouchY;
    if (Math.abs(delta) > 4) {
      fabDrag.current.moved = true;
      e.preventDefault();
    }
    const newY = Math.max(16, Math.min(window.innerHeight - 72, fabDrag.current.startBtnY + delta));
    setFabY(newY);
  }, []);

  const handleFabTouchEnd = useCallback(() => {
    const wasDrag = fabDrag.current?.moved ?? false;
    if (fabY !== null) localStorage.setItem(SIDEBAR_FAB_KEY, String(fabY));
    fabDrag.current = null;
    return wasDrag;
  }, [fabY]);

  const handleNavigate = (kind: string, slug: string) => {
    setActiveKind(kind);
    setActiveSlug(slug);
    window.location.hash = `${kind}/${slug}`;
    setMobileSidebarOpen(false);
  };

  return (
    <div className="flex h-full">
      {/* Mobile sidebar toggle — draggable along Y */}
      {fabY !== null && (
        <button
          type="button"
          onTouchStart={handleFabTouchStart}
          onTouchMove={handleFabTouchMove}
          onTouchEnd={(e) => {
            const wasDrag = handleFabTouchEnd();
            if (!wasDrag) {
              e.preventDefault();
              setMobileSidebarOpen(!mobileSidebarOpen);
            }
          }}
          onClick={() => {
            if (!fabDrag.current) setMobileSidebarOpen(!mobileSidebarOpen);
          }}
          style={{ top: `${fabY}px` }}
          className="fixed left-4 z-50 touch-none select-none rounded-full bg-accent p-3 text-white shadow-lg glow-purple lg:hidden"
          aria-label="Toggle sidebar — drag to reposition"
          aria-expanded={mobileSidebarOpen}
          aria-controls="journal-mobile-sidebar"
        >
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            {mobileSidebarOpen ? (
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            ) : (
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
            )}
          </svg>
        </button>
      )}

      {/* Sidebar - desktop */}
      <div className="hidden lg:block">
        <Sidebar
          activeKind={activeKind}
          activeSlug={activeSlug}
          onNavigate={handleNavigate}
          collapsed={sidebarCollapsed}
          onToggle={() => setSidebarCollapsed(!sidebarCollapsed)}
        />
      </div>

      {/* Sidebar - mobile overlay */}
      <div
        id="journal-mobile-sidebar"
        className={`fixed inset-0 z-40 lg:hidden transition-opacity duration-200 ${
          mobileSidebarOpen ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      >
        <div
          className="absolute inset-0 bg-overlay"
          aria-hidden="true"
          onClick={() => setMobileSidebarOpen(false)}
        />
        <div
          className={`relative z-10 h-full w-72 bg-c-dark/95 backdrop-blur-2xl shadow-xl transition-transform duration-200 ease-out ${
            mobileSidebarOpen ? "translate-x-0" : "-translate-x-full"
          }`}
        >
          <Sidebar
            activeKind={activeKind}
            activeSlug={activeSlug}
            onNavigate={handleNavigate}
          />
        </div>
      </div>

      {/* Recent Entries panel — desktop only, visible when viewing daily entries */}
      {activeKind === "daily" && dailyEntries.length > 0 && (
        <div className="hidden xl:block w-72 shrink-0 overflow-y-auto border-r border-white/5 bg-surface-elevated/30 backdrop-blur-sm">
          <div className="p-5">
            <h3 className="text-[10px] font-bold uppercase tracking-[0.15em] text-ink-faint mb-4">
              Recent Entries
            </h3>
            <div className="space-y-1">
              {dailyEntries.map((entry) => {
                const isActive = activeKind === "daily" && activeSlug === entry.slug;
                return (
                  <button
                    key={entry.slug}
                    type="button"
                    onClick={() => handleNavigate("daily", entry.slug)}
                    className={clsx(
                      "w-full rounded-xl p-3 text-left transition-all duration-200",
                      isActive
                        ? "bg-accent/10 border border-accent/20"
                        : "hover:bg-white/5",
                    )}
                  >
                    <span className={clsx(
                      "block text-sm font-bold mb-0.5",
                      isActive ? "text-accent" : "text-ink-muted",
                    )}>
                      {formatEntryDate(entry.slug)}
                    </span>
                    <span className={clsx(
                      "block text-sm truncate",
                      isActive ? "text-ink" : "text-ink-faint",
                    )}>
                      {entry.title}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      )}

      {/* Main content */}
      <div className="min-w-0 flex-1 overflow-hidden">
        <DocumentView
          kind={activeKind}
          slug={activeSlug}
          onNavigate={handleNavigate}
        />
      </div>
    </div>
  );
}
