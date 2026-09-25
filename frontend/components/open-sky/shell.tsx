"use client";

import clsx from "clsx";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode, useEffect, useRef, useState } from "react";

import { ErrorBoundary } from "@/components/error-boundary";
import {
  IconConstellation,
  IconCore,
  IconFuel,
  IconHorizons,
  IconJournal,
  IconLogOut,
  IconMore,
  IconNeighborhood,
  IconSettings,
} from "@/components/icons/constellation";
import { OpenSkyStarfield } from "@/components/open-sky/starfield";
import { useMeQuery } from "@/lib/queries";
import type { Tenant } from "@/lib/types";

type IconType = React.ComponentType<{ className?: string }>;

function IconOverview({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" className={className} aria-hidden="true">
      <circle cx="12" cy="12" r="3.2" />
      <path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1" />
    </svg>
  );
}

function IconLog({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" className={className} aria-hidden="true">
      <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
      <path d="M3.5 9.5h17M9.5 9.5v10" />
    </svg>
  );
}

interface Section {
  href: string;
  label: string;
  icon: IconType;
}

/** The redesign's sections, filtered by the same tenant flags as the old nav. */
export function openSkySections(tenant: Tenant | null | undefined): Section[] {
  const items: Section[] = [{ href: "/overview", label: "Overview", icon: IconOverview }];
  items.push({ href: "/journal", label: "Journal", icon: IconJournal });
  if (tenant?.constellation_enabled) items.push({ href: "/constellation", label: "Constellation", icon: IconConstellation });
  items.push({ href: "/horizons", label: "Horizons", icon: IconHorizons });
  if (tenant?.fuel_enabled) {
    items.push({ href: "/fuel", label: "Fuel", icon: IconFuel });
    items.push({ href: "/log", label: "Body log", icon: IconLog });
  }
  if (tenant?.core_enabled) items.push({ href: "/core", label: "Core", icon: IconCore });
  if (tenant?.neighborhood_enabled) items.push({ href: "/friends", label: "Neighborhood", icon: IconNeighborhood });
  items.push({ href: "/settings", label: "Settings", icon: IconSettings });
  return items;
}

function isActive(pathname: string, href: string) {
  return pathname === href || pathname.startsWith(`${href}/`);
}

function Wordmark({ compact = false }: { compact?: boolean }) {
  return (
    <Link href="/overview" className="os-focus inline-flex items-center rounded-md text-os-ink" aria-label="NBHD — Overview">
      <span className={clsx("font-medium tracking-[0.32em] text-os-muted", compact ? "text-[0.75rem]" : "text-[0.8125rem]")}>
        {compact ? "N" : "N B H D"}
      </span>
    </Link>
  );
}

function AccountButton({ onLogout, align = "right" }: { onLogout: () => void; align?: "right" | "left-up" }) {
  const { data: me } = useMeQuery();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);
  const name = me?.display_name || me?.email || "You";
  const initials = name.split(" ").map((w) => w[0]).join("").slice(0, 2).toUpperCase();
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label="Account"
        aria-expanded={open}
        className="os-focus flex h-11 w-11 items-center justify-center rounded-full border border-os-hairline text-[0.75rem] font-semibold text-os-muted hover:text-os-ink"
      >
        {initials}
      </button>
      {open ? (
        <div
          className={clsx(
            "absolute z-50 w-56 rounded-2xl border border-os-hairline bg-os-surface-solid p-2",
            align === "right" ? "right-0 top-full mt-2" : "bottom-full left-0 mb-2",
          )}
        >
          <p className="truncate px-3 py-2 text-[0.8125rem] text-os-muted">{name}</p>
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onLogout();
            }}
            className="os-focus flex min-h-[44px] w-full items-center gap-2.5 rounded-xl px-3 text-left text-[0.9375rem] text-os-ink hover:bg-os-accent-soft"
          >
            <IconLogOut className="h-4 w-4" />
            Sign out
          </button>
        </div>
      ) : null}
    </div>
  );
}

/** Desktop (≥1024): a text rail on the sky, divided from content by a hairline. */
function DesktopRail({ sections, pathname, onLogout }: { sections: Section[]; pathname: string; onLogout: () => void }) {
  return (
    <aside className="hidden w-[240px] shrink-0 flex-col border-r border-os-hairline lg:flex" aria-label="Sections">
      <div className="px-6 pb-8 pt-7">
        <Wordmark />
      </div>
      <nav className="flex-1 overflow-y-auto px-3" aria-label="Main navigation">
        <ul className="space-y-0.5">
          {sections.map((s) => {
            const active = isActive(pathname, s.href);
            return (
              <li key={s.href}>
                <Link
                  href={s.href}
                  aria-current={active ? "page" : undefined}
                  className={clsx(
                    "os-focus relative flex min-h-[40px] items-center rounded-lg px-3 text-[0.9375rem] transition-colors",
                    active ? "text-white" : "text-os-muted hover:text-os-ink",
                  )}
                >
                  {active ? (
                    <span className="absolute left-0 top-1/2 h-4 w-[2px] -translate-y-1/2 rounded-full bg-os-accent" aria-hidden="true" />
                  ) : null}
                  {s.label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
      <div className="space-y-4 px-6 pb-6 pt-4">
        <p className="text-[0.8125rem] leading-relaxed text-os-faint">
          Talk to your assistant on iPhone, Telegram or LINE. This page is for looking and tidying.
        </p>
        <AccountButton onLogout={onLogout} align="left-up" />
      </div>
    </aside>
  );
}

/** Tablet (768–1023): an icon rail. */
function TabletRail({ sections, pathname, onLogout }: { sections: Section[]; pathname: string; onLogout: () => void }) {
  return (
    <aside className="hidden w-[76px] shrink-0 flex-col items-center border-r border-os-hairline md:flex lg:hidden" aria-label="Sections">
      <div className="pb-6 pt-6">
        <Wordmark compact />
      </div>
      <nav className="flex-1 overflow-y-auto" aria-label="Main navigation">
        <ul className="flex flex-col items-center gap-1">
          {sections.map((s) => {
            const active = isActive(pathname, s.href);
            const Icon = s.icon;
            return (
              <li key={s.href}>
                <Link
                  href={s.href}
                  aria-current={active ? "page" : undefined}
                  aria-label={s.label}
                  title={s.label}
                  className={clsx(
                    "os-focus flex h-12 w-12 items-center justify-center rounded-xl transition-colors",
                    active ? "text-white" : "text-os-faint hover:text-os-ink",
                  )}
                >
                  <Icon className="h-[22px] w-[22px]" />
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
      <div className="pb-6 pt-4">
        <AccountButton onLogout={onLogout} align="left-up" />
      </div>
    </aside>
  );
}

/** Phone web (<768): the iPhone's floating capsule bar — Overview, Log, People, More. */
function PhoneBar({ sections, pathname, onLogout }: { sections: Section[]; pathname: string; onLogout: () => void }) {
  const [moreOpen, setMoreOpen] = useState(false);
  const primary: Section[] = [
    { href: "/overview", label: "Overview", icon: IconOverview },
    ...(sections.some((s) => s.href === "/log") ? [{ href: "/log", label: "Log", icon: IconLog }] : []),
    ...(sections.some((s) => s.href === "/friends") ? [{ href: "/friends", label: "People", icon: IconNeighborhood }] : []),
  ];
  const primaryHrefs = new Set(primary.map((s) => s.href));
  const rest = sections.filter((s) => !primaryHrefs.has(s.href));
  const moreActive = rest.some((s) => isActive(pathname, s.href));

  return (
    <>
      {moreOpen ? (
        <div className="fixed inset-x-0 bottom-0 z-40 md:hidden" style={{ height: "100dvh" }}>
          <button type="button" aria-label="Close menu" className="absolute inset-0 bg-overlay" onClick={() => setMoreOpen(false)} />
          <div
            role="dialog"
            aria-label="More sections"
            className="absolute inset-x-3 rounded-3xl border border-os-hairline bg-os-surface-solid p-3"
            style={{ bottom: "calc(env(safe-area-inset-bottom) + 96px)" }}
          >
            <ul>
              {rest.map((s) => {
                const Icon = s.icon;
                return (
                  <li key={s.href}>
                    <Link
                      href={s.href}
                      onClick={() => setMoreOpen(false)}
                      className="os-focus flex min-h-[52px] items-center gap-3 rounded-xl px-3 text-[1rem] text-os-ink hover:bg-os-accent-soft"
                    >
                      <Icon className="h-5 w-5 text-os-accent" />
                      {s.label}
                    </Link>
                  </li>
                );
              })}
              <li className="os-hairline-top mt-1 pt-1">
                <button
                  type="button"
                  onClick={() => {
                    setMoreOpen(false);
                    onLogout();
                  }}
                  className="os-focus flex min-h-[52px] w-full items-center gap-3 rounded-xl px-3 text-left text-[1rem] text-os-muted hover:bg-os-accent-soft"
                >
                  <IconLogOut className="h-5 w-5" />
                  Sign out
                </button>
              </li>
            </ul>
          </div>
        </div>
      ) : null}
      <nav
        aria-label="Main navigation"
        className="fixed inset-x-4 z-50 md:hidden"
        style={{ bottom: "calc(env(safe-area-inset-bottom) + 12px)" }}
      >
        <div className="mx-auto grid max-w-[480px] grid-flow-col auto-cols-fr items-center rounded-full border border-os-hairline bg-os-bar px-1.5 py-1.5 backdrop-blur-md">
          {primary.map((s) => {
            const active = isActive(pathname, s.href);
            const Icon = s.icon;
            return (
              <Link
                key={s.href}
                href={s.href}
                aria-current={active ? "page" : undefined}
                className={clsx(
                  "os-focus flex min-h-[56px] flex-col items-center justify-center gap-1 rounded-full text-[0.75rem]",
                  active ? "font-semibold text-white" : "text-os-muted",
                )}
              >
                <Icon className="h-[22px] w-[22px]" />
                {s.label}
              </Link>
            );
          })}
          <button
            type="button"
            onClick={() => setMoreOpen((v) => !v)}
            aria-expanded={moreOpen}
            className={clsx(
              "os-focus flex min-h-[56px] flex-col items-center justify-center gap-1 rounded-full text-[0.75rem]",
              moreActive || moreOpen ? "font-semibold text-white" : "text-os-muted",
            )}
          >
            <IconMore className="h-[22px] w-[22px]" />
            More
          </button>
        </div>
      </nav>
    </>
  );
}

/**
 * The Open Sky app shell (web redesign). Rendered instead of the legacy shell
 * only for tenants with `web_redesign`. Sections, auth and data are unchanged.
 */
export function OpenSkyShell({
  tenant,
  onLogout,
  children,
}: {
  tenant: Tenant | null | undefined;
  onLogout: () => void;
  children: ReactNode;
}) {
  const pathname = usePathname();
  const sections = openSkySections(tenant);

  return (
    <div className="relative flex h-[100dvh] overflow-hidden text-os-ink" style={{ paddingTop: "env(safe-area-inset-top)" }}>
      <OpenSkyStarfield />
      <a href="#main-content" className="skip-link">Skip to main content</a>
      <DesktopRail sections={sections} pathname={pathname} onLogout={onLogout} />
      <TabletRail sections={sections} pathname={pathname} onLogout={onLogout} />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center justify-between px-4 pb-1 pt-3 md:hidden">
          <Wordmark />
          <AccountButton onLogout={onLogout} />
        </div>
        <main
          id="main-content"
          className="min-h-0 flex-1 overflow-y-auto px-4 pb-32 pt-4 sm:px-6 md:px-10 md:pb-12 md:pt-8 lg:px-14"
        >
          <div className="mx-auto w-full max-w-[1100px]">
            <ErrorBoundary
              fallback={
                <div className="os-hairline-top pt-4">
                  <p className="text-[0.9375rem] text-os-ink">Something went wrong loading this page.</p>
                  <a href="/overview" className="mt-2 inline-block text-[0.9375rem] text-os-accent underline">
                    Go to Overview
                  </a>
                </div>
              }
            >
              {children}
            </ErrorBoundary>
          </div>
        </main>
      </div>
      <PhoneBar sections={sections} pathname={pathname} onLogout={onLogout} />
    </div>
  );
}
