"use client";

import { useState, useCallback } from "react";
import { Sidebar } from "@/components/journal/sidebar";
import { DocumentView } from "@/components/journal/document-view";
import { useSidebarTreeQuery } from "@/lib/queries";
import { fetchDocument } from "@/lib/api";
import { useQueryClient } from "@tanstack/react-query";

function todayISO(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
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
  const [viewKey, setViewKey] = useState(0);

  // Recent entries from sidebar tree
  const { data: tree } = useSidebarTreeQuery();
  const dailyEntries = (tree ?? []).find((s) => s.kind === "daily")?.items ?? [];

  const queryClient = useQueryClient();
  const prefetchDocument = useCallback(
    (kind: string, slug: string) => {
      void queryClient.prefetchQuery({
        queryKey: ["document", kind, slug],
        queryFn: () => fetchDocument(kind, slug),
      });
    },
    [queryClient],
  );

  const handleNavigate = (kind: string, slug: string) => {
    setActiveKind(kind);
    setActiveSlug(slug);
    window.location.hash = `${kind}/${slug}`;
    setMobileSidebarOpen(false);
    setViewKey((k) => k + 1);
  };

  return (
    <div className="flex h-full">
      {/* Sidebar toggle — near document title on mobile */}
      <button
        type="button"
        className="shrink-0 flex items-center justify-center w-10 h-10 rounded-xl border border-white/[0.06] bg-white/[0.02] text-ink-faint transition hover:bg-white/[0.04] hover:text-ink mr-2 lg:hidden"
        onClick={() => setMobileSidebarOpen(true)}
        aria-label="Open sidebar"
      >
        <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h10M4 18h16" />
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
          recentEntries={dailyEntries.map((e) => ({ slug: e.slug, title: e.title }))}
        />
      </div>

      {/* Mobile sidebar overlay */}
      <div
        className={`fixed inset-0 z-40 lg:hidden transition-opacity duration-200 ${
          mobileSidebarOpen ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      >
        <div
          className="absolute inset-0 bg-overlay/40 backdrop-blur-sm"
          aria-hidden="true"
          onClick={() => setMobileSidebarOpen(false)}
        />
        <div
          className={`relative z-10 h-full w-72 border-r border-white/[0.04] bg-[#0B0F13]/95 backdrop-blur-2xl shadow-[8px_0_40px_rgba(0,0,0,0.5)] transition-transform duration-250 ease-out ${
            mobileSidebarOpen ? "translate-x-0" : "-translate-x-full"
          }`}
        >
          <Sidebar
            activeKind={activeKind}
            activeSlug={activeSlug}
            onNavigate={handleNavigate}
            recentEntries={dailyEntries.map((e) => ({ slug: e.slug, title: e.title }))}
          />
        </div>
      </div>

      {/* Main content */}
      <div className="min-w-0 flex-1 overflow-hidden">
        <div
          key={`${activeKind}-${activeSlug}-${viewKey}`}
          className="view-transition-enter h-full"
        >
          <DocumentView
            kind={activeKind}
            slug={activeSlug}
            onNavigate={handleNavigate}
          />
        </div>
      </div>
    </div>
  );
}
