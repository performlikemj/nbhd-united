"use client";

import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode } from "react";

import {
  fetchEntityRegistry,
  fetchIntegrations,
  fetchMe,
  fetchCronJobs,
  fetchPATs,
  fetchPersonas,
  fetchPreferences,
  fetchTelegramStatus,
  fetchTenant,
  fetchUsageHistory,
  fetchUsageSummary,
} from "@/lib/api";

// ── Icons ────────────────────────────────────────────────────────────────────

function UserIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0A17.933 17.933 0 0112 21.75c-2.676 0-5.216-.584-7.499-1.632z" />
    </svg>
  );
}

function PuzzleIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round">
      <path d="M9.5 3.75a1.75 1.75 0 113.5 0v1.5h3.25c.69 0 1.25.56 1.25 1.25V9.75h1.5a1.75 1.75 0 110 3.5h-1.5v3.25c0 .69-.56 1.25-1.25 1.25H13v1.5a1.75 1.75 0 11-3.5 0v-1.5H6.25c-.69 0-1.25-.56-1.25-1.25v-3.25h-.25a1.75 1.75 0 010-3.5h.25V6.5c0-.69.56-1.25 1.25-1.25H9.5v-1.5z" />
    </svg>
  );
}

function ClockIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  );
}

function ChartIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" />
    </svg>
  );
}

function CreditCardIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 8.25h19.5M2.25 9h19.5m-16.5 5.25h6m-6 2.25h3m-3.75 3h15a2.25 2.25 0 002.25-2.25V6.75A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25v10.5A2.25 2.25 0 004.5 19.5z" />
    </svg>
  );
}

function CpuIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 3v1.5M4.5 8.25H3M21 8.25h-1.5M4.5 12H3M21 12h-1.5M4.5 15.75H3M21 15.75h-1.5M8.25 19.5V21M12 3v1.5M12 19.5V21M15.75 3v1.5M15.75 19.5V21M6.75 6.75h10.5v10.5H6.75V6.75z" />
    </svg>
  );
}

function KeyIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 5.25a3 3 0 013 3m3 0a6 6 0 01-7.029 5.912c-.563-.097-1.159.026-1.563.43L10.5 17.25H8.25v2.25H6v2.25H2.25v-2.818c0-.597.237-1.17.659-1.591l6.499-6.499c.404-.404.527-1 .43-1.563A6 6 0 1121.75 8.25z" />
    </svg>
  );
}

function PeopleIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M18 18.72a9.094 9.094 0 003.741-.479 3 3 0 00-4.682-2.72m.94 3.198l.001.031c0 .225-.012.447-.037.666A11.944 11.944 0 0112 21c-2.17 0-4.207-.576-5.963-1.584A6.062 6.062 0 016 18.719m12 0a5.971 5.971 0 00-.941-3.197m0 0A5.995 5.995 0 0012 12.75a5.995 5.995 0 00-5.058 2.772m0 0a3 3 0 00-4.681 2.72 8.986 8.986 0 003.74.477m.94-3.197a5.971 5.971 0 00-.94 3.197M15 6.75a3 3 0 11-6 0 3 3 0 016 0zm6 3a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0zm-13.5 0a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0z" />
    </svg>
  );
}

function ChevronRightIcon() {
  return (
    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
    </svg>
  );
}

function ChevronLeftIcon() {
  return (
    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5L8.25 12l7.5-7.5" />
    </svg>
  );
}

// ── Tab definitions ──────────────────────────────────────────────────────────

const settingsTabs = [
  { href: "/settings", label: "Account", icon: <UserIcon /> },
  { href: "/settings/integrations", label: "Integrations", icon: <PuzzleIcon /> },
  { href: "/settings/connected-apps", label: "Connected Apps", icon: <KeyIcon /> },
  { href: "/settings/people", label: "People", icon: <PeopleIcon /> },
  { href: "/settings/cron-jobs", label: "Scheduled Tasks", icon: <ClockIcon /> },
  { href: "/settings/usage", label: "Usage", icon: <ChartIcon /> },
  { href: "/settings/billing", label: "Billing", icon: <CreditCardIcon /> },
  { href: "/settings/ai-provider", label: "AI Provider", icon: <CpuIcon /> },
];

// ── Layout ───────────────────────────────────────────────────────────────────

export default function SettingsLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const queryClient = useQueryClient();
  const isRootSettings = pathname === "/settings";

  const prefetchTabData = (href: string) => {
    switch (href) {
      case "/settings":
        void queryClient.prefetchQuery({ queryKey: ["me"], queryFn: fetchMe });
        void queryClient.prefetchQuery({ queryKey: ["preferences"], queryFn: fetchPreferences });
        void queryClient.prefetchQuery({ queryKey: ["personas"], queryFn: fetchPersonas });
        break;
      case "/settings/integrations":
        void queryClient.prefetchQuery({ queryKey: ["integrations"], queryFn: fetchIntegrations });
        void queryClient.prefetchQuery({ queryKey: ["telegram-status"], queryFn: fetchTelegramStatus });
        break;
      case "/settings/connected-apps":
        void queryClient.prefetchQuery({ queryKey: ["pats"], queryFn: fetchPATs });
        break;
      case "/settings/cron-jobs":
        void queryClient.prefetchQuery({ queryKey: ["cron-jobs"], queryFn: fetchCronJobs });
        break;
      case "/settings/usage":
        void queryClient.prefetchQuery({ queryKey: ["usage-summary"], queryFn: fetchUsageSummary });
        void queryClient.prefetchQuery({ queryKey: ["usage-history"], queryFn: fetchUsageHistory });
        break;
      case "/settings/billing":
        void queryClient.prefetchQuery({ queryKey: ["tenant"], queryFn: fetchTenant });
        break;
      case "/settings/people":
        void queryClient.prefetchQuery({ queryKey: ["entity-registry"], queryFn: fetchEntityRegistry });
        break;
      default:
        break;
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-semibold text-ink">Settings</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Manage your account, integrations, scheduled tasks, usage, and billing.
        </p>
      </div>

      <div className="md:flex md:gap-8">
        {/* ── Desktop sidebar ──────────────────────────────────────────── */}
        <nav
          className="hidden md:block md:w-56 md:shrink-0"
          aria-label="Settings navigation"
        >
          <div className="sticky top-[72px] space-y-1">
            {settingsTabs.map((tab) => {
              const active = pathname === tab.href;
              return (
                <Link
                  key={tab.href}
                  href={tab.href}
                  aria-current={active ? "page" : undefined}
                  className={clsx(
                    "flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2",
                    active
                      ? "bg-accent text-white"
                      : "text-ink-muted hover:bg-surface-hover hover:text-ink",
                  )}
                  onMouseEnter={() => prefetchTabData(tab.href)}
                >
                  {tab.icon}
                  {tab.label}
                </Link>
              );
            })}
          </div>
        </nav>

        {/* ── Mobile navigation ────────────────────────────────────────── */}
        <div className="md:hidden">
          {isRootSettings ? (
            <nav
              className="mb-6 animate-reveal rounded-panel border border-border bg-card/95 shadow-panel divide-y divide-border overflow-hidden"
              aria-label="Settings navigation"
            >
              {settingsTabs
                .filter((tab) => tab.href !== "/settings")
                .map((tab) => (
                  <Link
                    key={tab.href}
                    href={tab.href}
                    className="flex items-center gap-3 px-4 py-3 min-h-[48px] text-sm transition hover:bg-surface-hover"
                    onTouchStart={() => prefetchTabData(tab.href)}
                  >
                    <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent/10 text-accent">
                      {tab.icon}
                    </span>
                    <span className="font-medium text-ink">{tab.label}</span>
                    <span className="ml-auto text-ink-faint">
                      <ChevronRightIcon />
                    </span>
                  </Link>
                ))}
            </nav>
          ) : (
            <Link
              href="/settings"
              className="mb-4 inline-flex items-center gap-1.5 text-sm text-ink-muted transition hover:text-ink"
            >
              <ChevronLeftIcon />
              Settings
            </Link>
          )}
        </div>

        {/* ── Content ──────────────────────────────────────────────────── */}
        <div className="min-w-0 flex-1">{children}</div>
      </div>
    </div>
  );
}
