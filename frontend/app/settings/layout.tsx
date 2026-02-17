"use client";

import clsx from "clsx";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode } from "react";

const settingsTabs = [
  { href: "/settings", label: "Account" },
  { href: "/settings/integrations", label: "Integrations" },
  { href: "/settings/automations", label: "Automations" },
  { href: "/settings/cron-jobs", label: "Scheduled Tasks" },
  { href: "/settings/usage", label: "Usage" },
  { href: "/settings/billing", label: "Billing" },
  { href: "/settings/ai-provider", label: "AI Provider" },
];

export default function SettingsLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-ink">Settings</h1>
        <p className="mt-1 text-sm text-ink/65">
          Manage your account, integrations, automations, usage, and billing.
        </p>
      </div>

      <nav className="flex flex-wrap items-center gap-1 border-b border-ink/10 pb-3">
        {settingsTabs.map((tab) => {
          const active = pathname === tab.href;
          return (
            <Link
              key={tab.href}
              href={tab.href}
              className={clsx(
                "rounded-full px-3 py-1.5 text-sm transition",
                active
                  ? "bg-ink text-white"
                  : "text-ink/75 hover:bg-ink/8 hover:text-ink",
              )}
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
