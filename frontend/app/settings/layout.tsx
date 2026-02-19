"use client";

import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode } from "react";

import {
  fetchIntegrations,
  getLLMConfig,
  fetchMe,
  fetchCronJobs,
  fetchPersonas,
  fetchPreferences,
  fetchTelegramStatus,
  fetchTenant,
  fetchUsageHistory,
  fetchUsageSummary,
} from "@/lib/api";

const settingsTabs = [
  { href: "/settings", label: "Account" },
  { href: "/settings/integrations", label: "Integrations" },
  { href: "/settings/cron-jobs", label: "Scheduled Tasks" },
  { href: "/settings/usage", label: "Usage" },
  { href: "/settings/billing", label: "Billing" },
  { href: "/settings/ai-provider", label: "AI Provider" },
];

export default function SettingsLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const queryClient = useQueryClient();

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
      case "/settings/ai-provider":
        void queryClient.prefetchQuery({ queryKey: ["llm-config"], queryFn: getLLMConfig });
        break;
      default:
        break;
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-ink">Settings</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Manage your account, integrations, scheduled tasks, usage, and billing.
        </p>
      </div>

      <nav className="flex flex-wrap items-center gap-1 border-b border-border pb-3">
        {settingsTabs.map((tab) => {
          const active = pathname === tab.href;
          return (
            <Link
              key={tab.href}
              href={tab.href}
              className={clsx(
                "rounded-full px-3 py-1.5 text-sm transition",
                active
                  ? "bg-accent text-white"
                  : "text-ink-muted hover:bg-surface-hover hover:text-ink",
              )}
              onMouseEnter={() => prefetchTabData(tab.href)}
            >
              {tab.label}
            </Link>
          );
        })}
      </nav>

      {children}
    </div>
  );
}
