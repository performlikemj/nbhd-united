"use client";

import Link from "next/link";

export function SiteFooter() {
  return (
    <footer className="mt-auto border-t border-border bg-surface/50">
      <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-4 sm:px-6">
        <Link href="/" className="font-mono text-xs uppercase tracking-[0.24em] text-ink-faint transition hover:text-ink-muted">
          NBHD United
        </Link>
        <nav className="flex flex-wrap items-center gap-4 text-xs text-ink-faint">
          <Link href="/legal/terms" className="transition hover:text-ink-muted">
            Terms of Service
          </Link>
          <Link href="/legal/privacy" className="transition hover:text-ink-muted">
            Privacy Policy
          </Link>
          <Link href="/legal/refund" className="transition hover:text-ink-muted">
            Refund Policy
          </Link>
          <a
            href="mailto:mj@bywayofmj.com"
            className="transition hover:text-ink-muted"
          >
            Contact
          </a>
        </nav>
      </div>
    </footer>
  );
}
