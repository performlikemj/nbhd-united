"use client";

import clsx from "clsx";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ReactNode, useEffect, useRef, useState } from "react";

import { logout } from "@/lib/api";
import { clearTokens, isLoggedIn } from "@/lib/auth";
import { useMeQuery, useTenantQuery } from "@/lib/queries";
import { BrandLogo } from "@/components/brand-logo";
import { ErrorBoundary } from "@/components/error-boundary";
import { SiteFooter } from "@/components/site-footer";
import { useTheme } from "@/components/theme-provider";

const baseNavItems = [
  { href: "/journal", label: "Journal" },
  { href: "/constellation", label: "★ Constellation" },
  { href: "/horizons", label: "◎ Horizons" },
];

const fuelNavItem = { href: "/finance", label: "◆ Fuel" };

const settingsNavItem = { href: "/settings", label: "Settings" };

const publicPages = ["/", "/login", "/signup", "/legal/terms", "/legal/privacy", "/legal/refund"];

function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();

  return (
    <button
      type="button"
      onClick={toggleTheme}
      className="flex h-11 w-11 items-center justify-center rounded-full border border-border text-sm transition hover:border-border-strong"
      aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
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
        className="flex h-11 items-center gap-2 rounded-full border border-border px-3 text-sm text-ink-muted transition hover:border-border-strong hover:text-ink"
        aria-label="User menu"
        aria-expanded={open}
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

function PlatformBudgetBanner() {
  const { data: tenant } = useTenantQuery();

  if (!tenant?.platform_budget_exceeded) {
    return null;
  }

  return (
    <div className="border-b border-amber-300/40 bg-amber-50 px-4 py-3 text-center text-sm text-amber-900 dark:border-amber-500/30 dark:bg-amber-950/50 dark:text-amber-200">
      <strong>Beta notice:</strong> Our platform budget for this month has been reached.
      Your assistant may be temporarily unavailable &mdash; service will resume when the budget resets.
      Thanks for your patience!
    </div>
  );
}

function BackgroundLayers() {
  return (
    <>
      <div
        className="pointer-events-none fixed inset-0 -z-10"
        style={{ background: "var(--bg-gradient)" }}
      />
      <div
        className="pointer-events-none fixed inset-0 -z-10 bg-[linear-gradient(rgba(18,31,38,0.05)_1px,transparent_1px),linear-gradient(90deg,rgba(18,31,38,0.05)_1px,transparent_1px)] bg-[size:32px_32px]"
        style={{ opacity: "var(--grid-opacity)" }}
      />
    </>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [checked, setChecked] = useState(false);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  const isPublicPage = publicPages.includes(pathname) || pathname.startsWith("/legal/");
  const { data: tenant } = useTenantQuery();

  const navItems = [
    ...baseNavItems,
    ...(tenant?.finance_enabled ? [fuelNavItem] : []),
    settingsNavItem,
  ];

  // Close mobile menu on route change
  useEffect(() => {
    setMobileMenuOpen(false);
  }, [pathname]);

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
        <a href="#main-content" className="skip-link">Skip to main content</a>
        <header className="border-b border-border bg-surface/75 backdrop-blur-xl backdrop-saturate-150">
          <div className="mx-auto flex w-full max-w-6xl items-center px-4 py-3 sm:px-6">
            <BrandLogo size={36} />
          </div>
        </header>
        <main id="main-content" className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6">{children}</main>
        <SiteFooter />
      </div>
    );
  }

  return (
    <div className="relative flex min-h-screen flex-col overflow-x-hidden">
      <BackgroundLayers />
      <a href="#main-content" className="skip-link">Skip to main content</a>

      <header className="sticky top-0 z-30 border-b border-border bg-surface/75 backdrop-blur-xl backdrop-saturate-150">
        <div className="mx-auto flex w-full max-w-6xl min-w-0 items-center justify-between gap-2 px-4 py-2.5 sm:gap-3 sm:px-6 sm:py-3">
          <div className="min-w-0">
            <BrandLogo size={32} />
            <h1 className="text-sm font-semibold text-ink sm:text-lg">
              <span className="hidden sm:inline">Subscriber Control Console</span>
              <span className="sm:hidden">Console</span>
            </h1>
          </div>
          <div className="flex items-center gap-2">
            <div className="hidden shrink-0 sm:block">
              <TrialBadge />
            </div>
            {/* Desktop nav */}
            <nav className="hidden items-center gap-1 rounded-full border border-border bg-surface p-1 md:flex" role="navigation" aria-label="Main navigation">
              {navItems.map((item) => {
                const active = pathname.startsWith(item.href);
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    aria-current={active ? "page" : undefined}
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
            {/* Hamburger — mobile only */}
            <button
              type="button"
              onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
              className="flex h-11 w-11 flex-col items-center justify-center gap-[5px] rounded-full border border-border md:hidden"
              aria-label={mobileMenuOpen ? "Close navigation menu" : "Open navigation menu"}
              aria-expanded={mobileMenuOpen}
              aria-controls="mobile-nav-menu"
            >
              <span className={clsx("block h-0.5 w-5 rounded-full bg-ink transition-transform", mobileMenuOpen && "translate-y-[7px] rotate-45")} />
              <span className={clsx("block h-0.5 w-5 rounded-full bg-ink transition-opacity", mobileMenuOpen && "opacity-0")} />
              <span className={clsx("block h-0.5 w-5 rounded-full bg-ink transition-transform", mobileMenuOpen && "-translate-y-[7px] -rotate-45")} />
            </button>
          </div>
        </div>

        {/* Mobile nav menu */}
        <div
          id="mobile-nav-menu"
          className={clsx(
            "overflow-hidden border-t border-border bg-surface transition-[max-height] duration-200 ease-out md:hidden",
            mobileMenuOpen ? "max-h-60" : "max-h-0 border-t-transparent",
          )}
        >
          <nav className="flex flex-col gap-1 px-4 py-3" role="navigation" aria-label="Mobile navigation">
            {navItems.map((item) => {
              const active = pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={clsx(
                    "flex min-h-[44px] items-center rounded-lg px-3 text-sm font-medium transition",
                    active
                      ? "bg-accent/10 text-accent"
                      : "text-ink-muted hover:bg-surface-hover hover:text-ink",
                  )}
                >
                  {item.label}
                </Link>
              );
            })}
            <div className="px-3 py-2 sm:hidden">
              <TrialBadge />
            </div>
          </nav>
        </div>
      </header>

      {/* <PlatformBudgetBanner /> */}

      <main id="main-content" className="mx-auto w-full max-w-6xl flex-1 flex flex-col min-h-0 px-4 py-8 sm:px-6">
        <ErrorBoundary fallback={
          <div className="rounded-panel border border-rose-border bg-rose-bg p-6 text-center">
            <p className="text-sm font-medium text-rose-text">Something went wrong loading this page.</p>
            <a href="/journal" className="mt-2 inline-block text-sm text-accent underline">Go to Journal</a>
          </div>
        }>
          {children}
        </ErrorBoundary>
      </main>
      <SiteFooter />
    </div>
  );
}
