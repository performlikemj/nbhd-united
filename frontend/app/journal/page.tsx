"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { Sidebar } from "@/components/journal/sidebar";
import { DocumentView } from "@/components/journal/document-view";

function todayISO(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export default function JournalPage() {
  const [activeKind, setActiveKind] = useState("daily");
  const [activeSlug, setActiveSlug] = useState(todayISO());
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  // Draggable sidebar FAB — persists Y position in localStorage
  const SIDEBAR_FAB_KEY = "sidebar-fab-y";
  const [fabY, setFabY] = useState<number | null>(null);
  const fabDrag = useRef<{ startTouchY: number; startBtnY: number; moved: boolean } | null>(null);

  useEffect(() => {
    const saved = localStorage.getItem(SIDEBAR_FAB_KEY);
    // Default: higher than footer (window.innerHeight - 120px)
    setFabY(saved ? parseInt(saved, 10) : window.innerHeight - 120);
  }, []);

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

  // Parse hash on mount for deep linking: #daily/2026-02-16
  useEffect(() => {
    const hash = window.location.hash.slice(1);
    if (hash) {
      const parts = hash.split("/");
      if (parts.length >= 2) {
        setActiveKind(parts[0]);
        setActiveSlug(parts.slice(1).join("/"));
      } else if (parts.length === 1) {
        setActiveKind(parts[0]);
        setActiveSlug(parts[0]);
      }
    }
  }, []);

  const handleNavigate = (kind: string, slug: string) => {
    setActiveKind(kind);
    setActiveSlug(slug);
    window.location.hash = `${kind}/${slug}`;
    setMobileSidebarOpen(false);
  };

  return (
    <div className="flex h-full">
      {/* Mobile sidebar toggle — draggable along Y, clears footer */}
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
            // Desktop fallback (no touch)
            if (!fabDrag.current) setMobileSidebarOpen(!mobileSidebarOpen);
          }}
          style={{ top: `${fabY}px` }}
          className="fixed left-4 z-50 touch-none select-none rounded-full bg-accent p-3 text-white shadow-lg lg:hidden"
          aria-label="Toggle sidebar — drag to reposition"
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
        className={`fixed inset-0 z-40 lg:hidden transition-opacity duration-200 ${
          mobileSidebarOpen ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      >
        <div
          className="absolute inset-0 bg-overlay"
          onClick={() => setMobileSidebarOpen(false)}
        />
        <div
          className={`relative z-10 h-full w-72 bg-surface shadow-xl transition-transform duration-200 ease-out ${
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

      {/* Main content */}
      <div className="min-w-0 flex-1 overflow-hidden bg-surface">
        <DocumentView
          kind={activeKind}
          slug={activeSlug}
          onNavigate={handleNavigate}
        />
      </div>
    </div>
  );
}
