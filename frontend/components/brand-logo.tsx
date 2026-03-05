"use client";

import Image from "next/image";
import Link from "next/link";

import { useTheme } from "@/components/theme-provider";

/**
 * Brand logo — icon + wordmark.
 * Uses the green icon on light surfaces and white icon on dark surfaces.
 */
export function BrandLogo({ size = 32, showText = true }: { size?: number; showText?: boolean }) {
  const { theme } = useTheme();
  const iconSrc = theme === "dark" ? "/icons/brand/icon-white-64.png" : "/icons/brand/icon-green-64.png";

  return (
    <Link href="/" className="flex items-center gap-2 transition hover:opacity-80">
      <Image
        src={iconSrc}
        alt="Neighborhood United"
        width={size}
        height={size}
        className="rounded-sm"
        priority
      />
      {showText && (
        <span className="font-mono text-xs uppercase tracking-[0.24em] text-ink-faint">
          Neighborhood United
        </span>
      )}
    </Link>
  );
}

/** Icon-only version for compact spaces (mobile header, favicon-adjacent). */
export function BrandIcon({ size = 36 }: { size?: number }) {
  const { theme } = useTheme();
  const iconSrc = theme === "dark" ? "/icons/brand/icon-white-64.png" : "/icons/brand/icon-green-64.png";

  return (
    <Link href="/" className="transition hover:opacity-80">
      <Image
        src={iconSrc}
        alt="Neighborhood United"
        width={size}
        height={size}
        className="rounded-sm"
        priority
      />
    </Link>
  );
}
