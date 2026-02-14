"use client";

import clsx from "clsx";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ReactNode, useEffect, useRef, useState } from "react";

import { logout } from "@/lib/api";
import { clearTokens, isLoggedIn } from "@/lib/auth";
import { hasPreviewKey, setPreviewKey } from "@/lib/preview";
import { useMeQuery } from "@/lib/queries";
import { SiteFooter } from "@/components/site-footer";

const navItems = [
  { href: "/", label: "Home" },
  { href: "/onboarding", label: "Onboarding" },
  { href: "/integrations", label: "Integrations" },
  { href: "/automations", label: "Automations" },
  { href: "/usage", label: "Usage" },
  { href: "/billing", label: "Billing" },
];

const publicPages = ["/login", "/signup"];
const isReviewPage = (pathname: string): boolean => pathname.startsWith("/review");

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
        className="flex items-center gap-2 rounded-full border border-ink/15 px-3 py-1.5 text-sm text-ink/75 transition hover:border-ink/30 hover:text-ink"
      >
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-ink/10 font-mono text-[10px] font-medium text-ink/70">
          {initials}
        </span>
        <span className="hidden sm:inline">{displayName}</span>
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-2 w-48 rounded-panel border border-ink/10 bg-white p-1 shadow-panel animate-reveal z-40">
          <Link
            href="/settings"
            onClick={() => setOpen(false)}
            className="block rounded-lg px-3 py-2 text-sm text-ink/75 hover:bg-ink/5 hover:text-ink"
          >
            Settings
          </Link>
          <button
            type="button"
            onClick={() => { setOpen(false); onLogout(); }}
            className="block w-full rounded-lg px-3 py-2 text-left text-sm text-ink/75 hover:bg-ink/5 hover:text-ink"
          >
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [checked, setChecked] = useState(false);
  const [previewOk, setPreviewOk] = useState(false);

  const isPublicPage = publicPages.includes(pathname) || pathname.startsWith("/legal/") || isReviewPage(pathname);
  const isLegalPage = pathname.startsWith("/legal/");
  const isIntegrationCallbackResult =
    pathname === "/integrations" &&
    typeof window !== "undefined" &&
    (() => {
      const params = new URLSearchParams(window.location.search);
      return params.has("connected") || params.has("error");
    })();

  // Capture ?preview=<key> from URL, store in localStorage, strip from address bar.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const keyParam = params.get("preview");
    if (keyParam) {
      setPreviewKey(keyParam);
      const url = new URL(window.location.href);
      url.searchParams.delete("preview");
      window.history.replaceState({}, "", url.toString());
    }
    setPreviewOk(hasPreviewKey());
  }, []);

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

  // Preview gate — block most routes except /legal/* unless this is a callback landing page
  // that needs to surface connected/error state to the user.
  if (
    !previewOk &&
    !isLegalPage &&
    !isIntegrationCallbackResult &&
    !isReviewPage(pathname)
  ) {
    return (
      <div className="relative flex min-h-screen flex-col">
        <div className="pointer-events-none absolute inset-0 -z-10 bg-[radial-gradient(circle_at_20%_20%,rgba(255,194,153,0.42),transparent_40%),radial-gradient(circle_at_85%_15%,rgba(112,194,184,0.45),transparent_32%),linear-gradient(180deg,#f8f6ef_0%,#eef4f4_48%,#f9f9f6_100%)]" />
        <div className="pointer-events-none absolute inset-0 -z-10 bg-[linear-gradient(rgba(18,31,38,0.05)_1px,transparent_1px),linear-gradient(90deg,rgba(18,31,38,0.05)_1px,transparent_1px)] bg-[size:32px_32px] opacity-70 animate-pulseGrid" />
        <div className="flex min-h-screen items-center justify-center">
          <div className="w-full max-w-md rounded-panel border border-ink/10 bg-white/90 p-8 shadow-panel animate-reveal text-center">
            <p className="font-mono text-xs uppercase tracking-[0.24em] text-ink/70">
              NBHD United
            </p>
            <h2 className="mt-2 text-2xl font-semibold text-ink">
              Preview Access Required
            </h2>
            <p className="mt-2 text-sm text-ink/65">
              This application is currently in preview. Please use the
              invite link provided by the administrator.
            </p>
          </div>
        </div>
      </div>
    );
  }

  if (!checked && !isPublicPage) {
    return null;
  }

  if (isPublicPage) {
    return (
      <div className="relative flex min-h-screen flex-col">
        <div className="pointer-events-none absolute inset-0 -z-10 bg-[radial-gradient(circle_at_20%_20%,rgba(255,194,153,0.42),transparent_40%),radial-gradient(circle_at_85%_15%,rgba(112,194,184,0.45),transparent_32%),linear-gradient(180deg,#f8f6ef_0%,#eef4f4_48%,#f9f9f6_100%)]" />
        <div className="pointer-events-none absolute inset-0 -z-10 bg-[linear-gradient(rgba(18,31,38,0.05)_1px,transparent_1px),linear-gradient(90deg,rgba(18,31,38,0.05)_1px,transparent_1px)] bg-[size:32px_32px] opacity-70 animate-pulseGrid" />
        <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6">{children}</main>
        <SiteFooter />
      </div>
    );
  }

  return (
    <div className="relative flex min-h-screen flex-col">
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
            <UserMenu onLogout={handleLogout} />
          </div>
        </div>
      </header>

      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6">{children}</main>
      <SiteFooter />
    </div>
  );
}
