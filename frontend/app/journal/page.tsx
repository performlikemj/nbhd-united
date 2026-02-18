"use client";

import { useState, useEffect } from "react";
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
      {/* Mobile sidebar toggle */}
      <button
        type="button"
        onClick={() => setMobileSidebarOpen(!mobileSidebarOpen)}
        className="fixed bottom-4 left-4 z-50 rounded-full bg-accent p-3 text-white shadow-lg lg:hidden"
        aria-label="Toggle sidebar"
      >
        <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          {mobileSidebarOpen ? (
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          ) : (
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
          )}
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
