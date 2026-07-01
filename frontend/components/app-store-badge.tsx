export const APP_STORE_URL = "https://apps.apple.com/us/app/nbhd/id6779158519";

// Intrinsic dimensions of Apple's official badge artwork (public/images/app-store-badge.svg).
const BADGE_RATIO = 119.66407 / 40;

/**
 * "Download on the App Store" — Apple's official, unmodified badge linking to the
 * NBHD iOS app. Per Apple's marketing guidelines we don't recolor/redraw the art;
 * we only scale it (min height 40px) and preserve clear space via the caller's
 * layout. Renders a plain <img> because the site is a static export (unoptimized).
 */
export function AppStoreBadge({
  className = "",
  height = 48,
}: {
  className?: string;
  height?: number;
}) {
  const width = Math.round(height * BADGE_RATIO);
  return (
    <a
      href={APP_STORE_URL}
      target="_blank"
      rel="noopener noreferrer"
      aria-label="Download NBHD on the App Store"
      className={`inline-block transition hover:opacity-90 active:scale-[0.98] ${className}`}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src="/images/app-store-badge.svg"
        alt="Download on the App Store"
        width={width}
        height={height}
        style={{ height, width }}
      />
    </a>
  );
}
