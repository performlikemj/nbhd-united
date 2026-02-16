"use client";

import clsx from "clsx";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode } from "react";

const journalTabs = [
  { href: "/journal/today", label: "Today" },
  { href: "/journal/memory", label: "Memory" },
  { href: "/journal/templates", label: "Templates" },
];

export default function JournalLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-ink">Journal</h1>
        <p className="mt-1 text-sm text-ink/65">
          Your template-based daily notes, long-term memory, and templates.
        </p>
      </div>

      <nav className="flex flex-wrap items-center gap-1 border-b border-ink/10 pb-3">
        {journalTabs.map((tab) => {
          const active =
            tab.href === "/journal/today"
              ? pathname === "/journal/today" || pathname.startsWith("/journal/today/")
              : pathname.startsWith(tab.href);
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
