"use client";

import clsx from "clsx";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ReactNode, useEffect, useRef, useState } from "react";

import { logout } from "@/lib/api";
import { clearTokens, isLoggedIn } from "@/lib/auth";
import { useMeQuery } from "@/lib/queries";
import { SiteFooter } from "@/components/site-footer";
import { useTheme } from "@/components/theme-provider";

const navItems = [
  { href: "/journal", label: "Journal" },
  { href: "/settings", label: "Settings" },
];

const publicPages = ["/", "/login", "/signup", "/legal/terms", "/legal/privacy", "/legal/refund"];

function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();

  return (
    <button
      type="button"
      onClick={toggleTheme}
      className="rounded-full border border-border p-2 text-sm transition hover:border-border-strong"
      aria-label="Toggle theme"
      title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
    >
      {theme === "dark" ? "☀️" : "🌙"}
    </button>
  );
}

function UserMenu({ onLogout }: { onLogout: () => void }) {
  const { data: me } = useMeQuery();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handler(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const displayName = me?.display_name || me?.email || "User";
  const initials = displayName
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 rounded-full border border-border px-3 py-1.5 text-sm text-ink-muted transition hover:border-border-strong hover:text-ink"
      >
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-surface-hover font-mono text-[10px] font-medium text-ink-faint">
          {initials}
        </span>
        <span className="hidden sm:inline">{displayName}</span>
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-2 w-48 rounded-panel border border-border bg-surface p-1 shadow-panel animate-reveal z-40">
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onLogout();
            }}
            className="block w-full rounded-lg px-3 py-2 text-left text-sm text-ink-muted hover:bg-surface-hover hover:text-ink"
          >
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}

function TrialBadge() {
  const { data: me } = useMeQuery();
  const tenant = me?.tenant;
  const isTrialEnded = tenant?.is_trial && !tenant?.has_active_subscription;
  const daysLeft = tenant?.trial_days_remaining;

  if (!tenant?.is_trial) {
    return null;
  }

  return (
    <span
      className={clsx(
        "inline-flex items-center rounded-full border px-3 py-1 text-xs font-medium",
        isTrialEnded
          ? "border-rose-border bg-rose-bg text-rose-text"
          : "border-accent/30 bg-accent/10 text-accent",
      )}
    >
      {isTrialEnded
        ? "Trial ended"
        : `Trial: ${daysLeft ?? 0} days left`}
    </span>
  );
}

function BackgroundLayers() {
  return (
    <>
      <div
        className="pointer-events-none absolute inset-0 -z-10"
        style={{ background: "var(--bg-gradient)" }}
      />
      <div
        className="pointer-events-none absolute inset-0 -z-10 bg-[linear-gradient(rgba(18,31,38,0.05)_1px,transparent_1px),linear-gradient(90deg,rgba(18,31,38,0.05)_1px,transparent_1px)] bg-[size:32px_32px]"
        style={{ opacity: "var(--grid-opacity)" }}
      />
    </>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [checked, setChecked] = useState(false);

  const isPublicPage = publicPages.includes(pathname) || pathname.startsWith("/legal/");

  useEffect(() => {
    if (!isPublicPage && !isLoggedIn()) {
      router.replace("/login");
    } else {
      setChecked(true);
    }
  }, [pathname, isPublicPage, router]);

  const handleLogout = async () => {
    try {
      await logout();
    } catch {
      // Proceed with local logout even if server-side revocation fails.
    } finally {
      clearTokens();
      router.push("/login");
    }
  };

  if (!checked && !isPublicPage) {
    return null;
  }

  if (isPublicPage) {
    return (
      <div className="relative flex min-h-screen flex-col overflow-x-hidden">
        <BackgroundLayers />
        <header className="border-b border-border bg-surface/75 backdrop-blur">
          <div className="mx-auto flex w-full max-w-6xl items-center px-4 py-3 sm:px-6">
            <Link href="/" className="font-mono text-xs uppercase tracking-[0.24em] text-ink-faint transition hover:text-ink">
              NBHD United
            </Link>
          </div>
        </header>
        <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6">{children}</main>
        <SiteFooter />
      </div>
    );
  }

  return (
    <div className="relative flex min-h-screen flex-col overflow-x-hidden">
      <BackgroundLayers />

      <header className="sticky top-0 z-30 border-b border-border bg-surface/75 backdrop-blur">
        <div className="mx-auto flex w-full max-w-6xl min-w-0 flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:gap-3 sm:px-6">
          <div className="min-w-0">
            <p className="font-mono text-xs uppercase tracking-[0.24em] text-ink-faint">NBHD United</p>
            <h1 className="text-sm font-semibold text-ink sm:text-lg">
              <span className="hidden sm:inline">Subscriber Control Console</span>
              <span className="sm:hidden">Subscriber</span>
            </h1>
          </div>
          <div className="flex w-full min-w-0 items-center gap-2 overflow-x-auto pb-1 sm:w-auto sm:overflow-visible sm:pb-0">
            <div className="shrink-0">
              <TrialBadge />
            </div>
            <nav className="flex min-w-0 flex-1 items-center gap-1 rounded-full border border-border bg-surface p-1 sm:flex-initial">
              {navItems.map((item) => {
                const active = pathname.startsWith(item.href);
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={clsx(
                      "shrink-0 rounded-full px-3 py-1.5 text-sm transition",
                      active
                        ? "bg-accent text-white"
                        : "text-ink-muted hover:bg-surface-hover hover:text-ink",
                    )}
                  >
                    {item.label}
                  </Link>
                );
              })}
            </nav>
            <ThemeToggle />
            <UserMenu onLogout={handleLogout} />
          </div>
        </div>
      </header>

      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6">{children}</main>
      <SiteFooter />
    </div>
  );
}
