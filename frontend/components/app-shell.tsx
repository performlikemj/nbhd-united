"use client";

import clsx from "clsx";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ReactNode, useEffect, useRef, useState, useCallback } from "react";

import { logout } from "@/lib/api";
import { clearTokens, isLoggedIn } from "@/lib/auth";
import { useMeQuery, useTenantQuery } from "@/lib/queries";
import { clearPersistedCache } from "@/lib/query-persist";
import { BrandLogo, BrandIcon } from "@/components/brand-logo";
import { ErrorBoundary } from "@/components/error-boundary";
import { SiteFooter } from "@/components/site-footer";
import { SynapseNetwork } from "@/components/landing/synapse-network";
import {
  IconJournal,
  IconConstellation,
  IconHorizons,
  IconGravity,
  IconFuel,
  IconSettings,
  IconLogOut,
} from "@/components/icons/constellation";

const publicPages = ["/", "/login", "/signup", "/legal/terms", "/legal/privacy", "/legal/refund"];

interface NavItem {
  href: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}

function useNavItems(tenant?: { finance_enabled?: boolean; fuel_enabled?: boolean } | null): NavItem[] {
  const items: NavItem[] = [
    { href: "/journal", label: "Journal", icon: IconJournal },
    { href: "/constellation", label: "Constellation", icon: IconConstellation },
    { href: "/horizons", label: "Horizons", icon: IconHorizons },
  ];
  if (tenant?.finance_enabled) {
    items.push({ href: "/finance", label: "Gravity", icon: IconGravity });
  }
  if (tenant?.fuel_enabled) {
    items.push({ href: "/fuel", label: "Fuel", icon: IconFuel });
  }
  items.push({ href: "/settings", label: "Settings", icon: IconSettings });
  return items;
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
        className="flex h-10 w-10 items-center justify-center rounded-full bg-white/[0.03] border border-white/[0.06] text-ink-muted transition hover:bg-white/[0.06] hover:text-ink hover:border-white/[0.10] focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:ring-offset-2 focus-visible:ring-offset-[#0B0F13]"
        aria-label="User menu"
        aria-expanded={open}
      >
        <span className="font-mono text-[11px] font-medium text-ink-faint">
          {initials}
        </span>
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-2 w-56 rounded-2xl border border-white/[0.06] bg-[#0F1419]/95 backdrop-blur-2xl p-2 shadow-[0_20px_55px_rgba(0,0,0,0.45)] animate-reveal z-40">
          <div className="px-3 py-2.5 border-b border-white/[0.04] mb-1">
            <p className="text-xs text-ink-muted truncate">{displayName}</p>
          </div>
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onLogout();
            }}
            className="flex w-full items-center gap-2.5 rounded-xl px-3 py-3 text-left text-sm text-ink-muted hover:bg-white/[0.04] hover:text-ink transition min-h-[48px]"
          >
            <IconLogOut className="h-4 w-4" />
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

  if (!tenant?.is_trial) return null;

  return (
    <span
      className={clsx(
        "inline-flex items-center rounded-full border px-3 py-1.5 text-xs font-medium",
        isTrialEnded
          ? "border-rose-border bg-rose-bg text-rose-text"
          : "border-accent/25 bg-accent/[0.08] text-accent",
      )}
    >
      {isTrialEnded
        ? "Trial ended"
        : `Trial: ${daysLeft ?? 0}d`}
    </span>
  );
}

function TrialDot() {
  const { data: me } = useMeQuery();
  const tenant = me?.tenant;
  const isTrialEnded = tenant?.is_trial && !tenant?.has_active_subscription;

  if (!tenant?.is_trial) return null;

  return (
    <div className="relative group">
      <div
        className={clsx(
          "h-2 w-2 rounded-full",
          isTrialEnded ? "bg-rose-text" : "bg-accent"
        )}
      />
      <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 hidden group-hover:block">
        <div className="rounded-lg bg-[#0F1419] border border-white/[0.06] px-2.5 py-1.5 text-[10px] whitespace-nowrap text-ink-muted shadow-lg">
          {isTrialEnded ? "Trial ended" : `${tenant?.trial_days_remaining ?? 0}d free trial`}
        </div>
      </div>
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
        className="pointer-events-none fixed inset-0 -z-10 bg-[linear-gradient(rgba(226,232,240,0.03)_1px,transparent_1px),linear-gradient(90deg,rgba(226,232,240,0.03)_1px,transparent_1px)] bg-[size:32px_32px]"
        style={{ opacity: "var(--grid-opacity)" }}
      />
      <SynapseNetwork className="fixed inset-0 -z-10 opacity-[0.04]" />
    </>
  );
}

function MobileTabBar({
  items,
  pathname,
}: {
  items: NavItem[];
  pathname: string;
}) {
  return (
    <nav
      className="z-[50] lg:hidden mobile-tab-shadow bg-[#0B0F13]/90 backdrop-blur-2xl border-t border-white/[0.04] pb-[env(safe-area-inset-bottom)]"
      role="navigation"
      aria-label="Mobile navigation"
    >
      <div className="flex items-center justify-around h-14">
        {items.map((item) => {
          const active = pathname.startsWith(item.href);
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              className="flex flex-1 flex-col items-center justify-center gap-0.5 py-1.5 text-[10px] font-medium transition-all duration-200 min-h-[44px]"
            >
              <span
                className={clsx(
                  "transition-all duration-200 p-1.5 rounded-xl",
                  active
                    ? "text-accent"
                    : "text-ink-faint"
                )}
              >
                <Icon className={clsx(
                  "h-[22px] w-[22px] transition-transform duration-200",
                  active ? "scale-110" : ""
                )} />
              </span>
              <span
                className={clsx(
                  "transition-colors duration-200",
                  active
                    ? "text-accent"
                    : "text-ink-faint"
                )}
              >
                {item.label}
              </span>
              {active && (
                <span className="absolute bottom-1.5 h-1 w-1 rounded-full bg-accent shadow-[0_0_6px_rgba(124,107,240,0.6)]" />
              )}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [headerBorder, setHeaderBorder] = useState(false);

  const isPublicPage = publicPages.includes(pathname) || pathname.startsWith("/legal/");
  const { data: tenant } = useTenantQuery();
  const navItems = useNavItems(tenant);

  // Scroll listener for header blur/border transition (main is the scroll container)
  useEffect(() => {
    const main = document.getElementById("main-content");
    if (!main) return;
    const handleScroll = () => {
      setHeaderBorder(main.scrollTop > 8);
    };
    main.addEventListener("scroll", handleScroll, { passive: true });
    return () => main.removeEventListener("scroll", handleScroll);
  }, [pathname]);

  useEffect(() => {
    if (!isPublicPage && !isLoggedIn()) {
      router.replace("/login");
    }
  }, [pathname, isPublicPage, router]);

  const handleLogout = async () => {
    try {
      await logout();
    } catch {
      // Proceed with local logout even if server-side revocation fails.
    } finally {
      clearTokens();
      clearPersistedCache();
      router.push("/login");
    }
  };

  // Full-bleed pages — no shell chrome
  const fullBleedPages = ["/", "/signup", "/login", "/onboarding"];
  if (fullBleedPages.includes(pathname)) {
    return (
      <ErrorBoundary>
        <a href="#main-content" className="skip-link">Skip to main content</a>
        <main id="main-content">{children}</main>
      </ErrorBoundary>
    );
  }

  if (isPublicPage) {
    return (
      <div className="relative flex min-h-screen flex-col overflow-x-hidden">
        <BackgroundLayers />
        <a href="#main-content" className="skip-link">Skip to main content</a>
        <header className="border-b border-white/[0.04] bg-[#0B0F13]/80 backdrop-blur-xl">
          <div className="mx-auto flex w-full max-w-6xl items-center px-4 py-2.5 sm:px-6">
            <BrandLogo size={32} />
          </div>
        </header>
        <main id="main-content" className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6">{children}</main>
        <SiteFooter />
      </div>
    );
  }

  return (
    <div
      className="relative flex h-[100dvh] flex-col overflow-x-hidden"
      style={{ paddingTop: "env(safe-area-inset-top)" }}
    >
      <BackgroundLayers />
      <a href="#main-content" className="skip-link">Skip to main content</a>

      {/* Elegant slim header */}
      <header
        className={clsx(
          "sticky top-0 z-30 bg-[#0B0F13]/70 backdrop-blur-xl transition-all duration-300",
          headerBorder
            ? "border-b border-white/[0.06] shadow-[0_1px_16px_rgba(0,0,0,0.3)]"
            : "border-b border-transparent"
        )}
      >
        <div className="mx-auto flex w-full max-w-6xl min-w-0 items-center justify-between gap-2 px-3 py-2.5 sm:gap-3 sm:px-6">
          {/* Left: Logo + concise title */}
          <div className="flex items-center gap-2.5 min-w-0 shrink-0">
            <Link
              href="/journal"
              className="shrink-0 rounded-lg focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:ring-offset-2 focus-visible:ring-offset-[#0B0F13]"
            >
              <BrandLogo size={32} />
            </Link>
            <span className="text-xs font-medium text-ink-faint tracking-wide hidden xl:block">
              Neighborhood
            </span>
          </div>

          {/* Center: pill nav — desktop only */}
          <nav
            className="hidden lg:flex items-center gap-0.5 rounded-full border border-white/[0.06] bg-white/[0.02] backdrop-blur-sm p-0.5"
            role="navigation"
            aria-label="Main navigation"
          >
            {navItems.map((item) => {
              const active = pathname.startsWith(item.href);
              const Icon = item.icon;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={clsx(
                    "relative flex items-center gap-1.5 rounded-full px-3 py-2 text-xs font-medium transition-all duration-200 min-h-[40px]",
                    active
                      ? "bg-accent/15 text-accent shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]"
                      : "text-ink-faint hover:bg-white/[0.04] hover:text-ink-muted",
                  )}
                >
                  <Icon className="h-4 w-4" />
                  <span>{item.label}</span>
                </Link>
              );
            })}
          </nav>

          {/* Right: trial + user menu */}
          <div className="flex items-center gap-2 sm:gap-3 shrink-0">
            <div className="hidden sm:block">
              <TrialBadge />
            </div>
            <div className="block sm:hidden">
              <TrialDot />
            </div>
            <UserMenu onLogout={handleLogout} />
          </div>
        </div>
      </header>

      <main
        id="main-content"
        className="mx-auto w-full max-w-6xl flex-1 flex flex-col min-h-0 overflow-y-auto px-4 py-4 sm:py-6 sm:px-6 lg:py-8"
      >
        <div className="content-fade-up flex-1 min-h-0 flex flex-col">
          <ErrorBoundary
            fallback={
              <div className="rounded-2xl border border-rose-border bg-rose-bg p-6 text-center">
                <p className="text-sm font-medium text-rose-text">Something went wrong loading this page.</p>
                <a href="/journal" className="mt-2 inline-block text-sm text-accent underline">Go to Journal</a>
              </div>
            }
          >
            {children}
          </ErrorBoundary>
        </div>
      </main>

      {/* Mobile tab bar */}
      <MobileTabBar items={navItems} pathname={pathname} />
    </div>
  );
}
