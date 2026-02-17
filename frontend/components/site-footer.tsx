"use client";

import Link from "next/link";

export function SiteFooter() {
  return (
    <footer className="mt-auto border-t border-ink/10 bg-white/50">
      <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-4 sm:px-6">
        <Link href="/" className="font-mono text-xs uppercase tracking-[0.24em] text-ink/40 transition hover:text-ink/70">
          NBHD United
        </Link>
        <nav className="flex flex-wrap items-center gap-4 text-xs text-ink/40">
          <Link href="/legal/terms" className="transition hover:text-ink/70">
            Terms of Service
          </Link>
          <Link href="/legal/privacy" className="transition hover:text-ink/70">
            Privacy Policy
          </Link>
          <Link href="/legal/refund" className="transition hover:text-ink/70">
            Refund Policy
          </Link>
          <a
            href="mailto:mj@bywayofmj.com"
            className="transition hover:text-ink/70"
          >
            Contact
          </a>
        </nav>
      </div>
    </footer>
  );
}
