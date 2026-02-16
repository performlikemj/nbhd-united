"use client";

import { useState, useEffect } from "react";
import { Sidebar } from "@/components/journal/sidebar";
import { DocumentView } from "@/components/journal/document-view";

function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

export default function JournalPage() {
  const [activeKind, setActiveKind] = useState("daily");
  const [activeSlug, setActiveSlug] = useState(todayISO());
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  // Parse hash on mount for deep linking: #daily/2026-02-16
  useEffect(() => {
    const hash = window.location.hash.slice(1);
    if (hash) {
      const parts = hash.split("/");
      if (parts.length >= 2) {
        setActiveKind(parts[0]);
        setActiveSlug(parts.slice(1).join("/"));
      } else if (parts.length === 1) {
        // Singleton: #tasks, #ideas, #memory
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
      {/* Mobile sidebar toggle */}
      <button
        type="button"
        onClick={() => setMobileSidebarOpen(!mobileSidebarOpen)}
        className="fixed bottom-4 left-4 z-50 rounded-full bg-ink p-3 text-white shadow-lg lg:hidden"
      >
        <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
        </svg>
      </button>

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
      {mobileSidebarOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0 bg-black/30"
            onClick={() => setMobileSidebarOpen(false)}
          />
          <div className="relative z-10 h-full w-72">
            <Sidebar
              activeKind={activeKind}
              activeSlug={activeSlug}
              onNavigate={handleNavigate}
            />
          </div>
        </div>
      )}

      {/* Main content */}
      <div className="flex-1 overflow-hidden bg-white">
        <DocumentView
          kind={activeKind}
          slug={activeSlug}
          onNavigate={handleNavigate}
        />
      </div>
    </div>
  );
}
