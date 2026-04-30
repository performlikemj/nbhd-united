"use client";

import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode } from "react";

import {
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
  fetchWorkspaces,
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
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M14.25 6.087c0-.355.186-.676.401-.959.221-.29.349-.634.349-1.003 0-1.036-1.007-1.875-2.25-1.875s-2.25.84-2.25 1.875c0 .369.128.713.349 1.003.215.283.401.604.401.959v.75H8.25A2.25 2.25 0 006 9v3.75c0 .414-.336.75-.75.75h-.75c-.355 0-.676.186-.959.401-.29.221-.634.349-1.003.349-1.036 0-1.875 1.007-1.875 2.25s.84 2.25 1.875 2.25c.369 0 .713-.128 1.003-.349.283-.215.604-.401.959-.401h.75a.75.75 0 00.75-.75V14.25h2.25a2.25 2.25 0 002.25-2.25V9h.75c.355 0 .676-.186.959-.401.29-.221.634-.349 1.003-.349 1.036 0 1.875 1.007 1.875 2.25s-.84 2.25-1.875 2.25c-.369 0-.713-.128-1.003-.349-.283-.215-.604-.401-.959-.401H15V15a2.25 2.25 0 01-2.25 2.25H9v.75" />
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

function WorkspaceIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z" />
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
  { href: "/settings/workspaces", label: "Workspaces", icon: <WorkspaceIcon /> },
  { href: "/settings/integrations", label: "Integrations", icon: <PuzzleIcon /> },
  { href: "/settings/connected-apps", label: "Connected Apps", icon: <KeyIcon /> },
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
      case "/settings/workspaces":
        void queryClient.prefetchQuery({ queryKey: ["workspaces"], queryFn: fetchWorkspaces });
        break;
      case "/settings/usage":
        void queryClient.prefetchQuery({ queryKey: ["usage-summary"], queryFn: fetchUsageSummary });
        void queryClient.prefetchQuery({ queryKey: ["usage-history"], queryFn: fetchUsageHistory });
        break;
      case "/settings/billing":
        void queryClient.prefetchQuery({ queryKey: ["tenant"], queryFn: fetchTenant });
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
