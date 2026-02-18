"use client";

import Link from "next/link";
import { ReactNode } from "react";

const legalLinks = [
  { href: "/legal/terms", label: "Terms of Service" },
  { href: "/legal/privacy", label: "Privacy Policy" },
  { href: "/legal/refund", label: "Refund Policy" },
];

export function LegalPage({
  title,
  lastUpdated,
  children,
}: {
  title: string;
  lastUpdated: string;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-[60vh] justify-center py-12">
      <div className="w-full max-w-3xl rounded-panel border border-border bg-surface/90 p-8 shadow-panel animate-reveal">
        <Link href="/" className="font-mono text-xs uppercase tracking-[0.24em] text-ink-muted transition hover:text-ink">
          NBHD United
        </Link>
        <h1 className="mt-2 text-2xl font-semibold text-ink">{title}</h1>
        <p className="mt-1 text-sm text-ink-faint">Last updated: {lastUpdated}</p>
        <hr className="my-6 border-border" />
        <div className="prose prose-sm max-w-none text-ink-muted prose-headings:text-ink prose-h2:text-lg prose-h2:font-semibold prose-h2:mt-8 prose-h2:mb-3 prose-h3:text-base prose-h3:font-medium prose-h3:mt-6 prose-h3:mb-2 prose-p:leading-relaxed prose-ul:my-2 prose-li:my-0.5 prose-a:text-ink prose-a:underline hover:prose-a:text-ink-muted">
          {children}
        </div>
        <hr className="my-8 border-border" />
        <div className="flex flex-wrap gap-4 text-sm">
          {legalLinks.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="text-ink-faint underline transition hover:text-ink-muted"
            >
              {link.label}
            </Link>
          ))}
        </div>
      </div>
    </div>
  );
}
