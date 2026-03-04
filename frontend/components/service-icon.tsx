// Service icon URLs (official favicons / brand marks)
export const SERVICE_ICONS: Record<string, string> = {
  telegram: "https://telegram.org/favicon.ico",
  line: "https://line.me/favicon.ico",
  gmail: "https://ssl.gstatic.com/ui/v1/icons/mail/rfr/gmail.ico",
  "google-calendar": "https://calendar.google.com/googlecalendar/images/favicon_v2014_1.ico",
  reddit: "https://www.redditstatic.com/desktop2x/img/favicon/favicon-32x32.png",
};

export function ServiceIcon({ provider, size = 20 }: { provider: string; size?: number }) {
  const src = SERVICE_ICONS[provider];
  if (!src) return null;
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt=""
      width={size}
      height={size}
      className="rounded-sm object-contain"
      onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
    />
  );
}
