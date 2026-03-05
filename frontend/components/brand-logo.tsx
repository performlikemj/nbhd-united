import Image from "next/image";
import Link from "next/link";

/**
 * Brand logo — icon + wordmark.
 * Uses the white icon on dark surfaces (default) or light version when specified.
 */
export function BrandLogo({ size = 24, showText = true }: { size?: number; showText?: boolean }) {
  return (
    <Link href="/" className="flex items-center gap-2 transition hover:opacity-80">
      <Image
        src="/images/logo-light.png"
        alt="Neighborhood United"
        width={size}
        height={size}
        className="rounded-sm"
        style={{ objectFit: "contain" }}
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
export function BrandIcon({ size = 28 }: { size?: number }) {
  return (
    <Link href="/" className="transition hover:opacity-80">
      <Image
        src="/images/logo-light.png"
        alt="Neighborhood United"
        width={size}
        height={size}
        className="rounded-sm"
        style={{ objectFit: "contain" }}
        priority
      />
    </Link>
  );
}
