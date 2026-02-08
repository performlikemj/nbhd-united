"use client";

import clsx from "clsx";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";

import { clearTokens, isLoggedIn } from "@/lib/auth";

const navItems = [
  { href: "/", label: "Home" },
  { href: "/onboarding", label: "Onboarding" },
  { href: "/integrations", label: "Integrations" },
  { href: "/usage", label: "Usage" },
  { href: "/billing", label: "Billing" },
];

const publicPages = ["/login", "/signup"];

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [checked, setChecked] = useState(false);

  const isPublicPage = publicPages.includes(pathname);

  useEffect(() => {
    if (!isPublicPage && !isLoggedIn()) {
      router.replace("/login");
    } else {
      setChecked(true);
    }
  }, [pathname, isPublicPage, router]);

  const handleLogout = () => {
    clearTokens();
    router.push("/login");
  };

  if (!checked && !isPublicPage) {
    return null;
  }

  if (isPublicPage) {
    return (
      <div className="relative min-h-screen">
        <div className="pointer-events-none absolute inset-0 -z-10 bg-[radial-gradient(circle_at_20%_20%,rgba(255,194,153,0.42),transparent_40%),radial-gradient(circle_at_85%_15%,rgba(112,194,184,0.45),transparent_32%),linear-gradient(180deg,#f8f6ef_0%,#eef4f4_48%,#f9f9f6_100%)]" />
        <div className="pointer-events-none absolute inset-0 -z-10 bg-[linear-gradient(rgba(18,31,38,0.05)_1px,transparent_1px),linear-gradient(90deg,rgba(18,31,38,0.05)_1px,transparent_1px)] bg-[size:32px_32px] opacity-70 animate-pulseGrid" />
        <main className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6">{children}</main>
      </div>
    );
  }

  return (
    <div className="relative min-h-screen">
      <div className="pointer-events-none absolute inset-0 -z-10 bg-[radial-gradient(circle_at_20%_20%,rgba(255,194,153,0.42),transparent_40%),radial-gradient(circle_at_85%_15%,rgba(112,194,184,0.45),transparent_32%),linear-gradient(180deg,#f8f6ef_0%,#eef4f4_48%,#f9f9f6_100%)]" />
      <div className="pointer-events-none absolute inset-0 -z-10 bg-[linear-gradient(rgba(18,31,38,0.05)_1px,transparent_1px),linear-gradient(90deg,rgba(18,31,38,0.05)_1px,transparent_1px)] bg-[size:32px_32px] opacity-70 animate-pulseGrid" />

      <header className="sticky top-0 z-30 border-b border-ink/10 bg-white/75 backdrop-blur">
        <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-3 sm:px-6">
          <div>
            <p className="font-mono text-xs uppercase tracking-[0.24em] text-ink/70">NBHD United</p>
            <h1 className="text-lg font-semibold text-ink">Subscriber Control Console</h1>
          </div>
          <div className="flex items-center gap-3">
            <nav className="flex items-center gap-1 rounded-full border border-ink/15 bg-white p-1">
              {navItems.map((item) => {
                const active = pathname === item.href;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={clsx(
                      "rounded-full px-3 py-1.5 text-sm transition",
                      active
                        ? "bg-ink text-white"
                        : "text-ink/75 hover:bg-ink/8 hover:text-ink"
                    )}
                  >
                    {item.label}
                  </Link>
                );
              })}
            </nav>
            <button
              type="button"
              onClick={handleLogout}
              className="rounded-full border border-ink/15 px-3 py-1.5 text-sm text-ink/70 transition hover:border-ink/30 hover:text-ink"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6">{children}</main>
    </div>
  );
}
