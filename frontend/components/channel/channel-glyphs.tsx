import type { MessagingChannel } from "@/components/channel/types";

export function TelegramGlyph({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
      className={`shrink-0 text-telegram ${className}`}
    >
      <path d="M9.78 18.65l.28-4.23 7.68-6.92c.34-.31-.07-.46-.52-.19L7.74 13.3 3.64 12c-.88-.25-.89-.86.2-1.3l15.97-6.16c.73-.33 1.43.18 1.15 1.3l-2.72 12.81c-.19.91-.74 1.13-1.5.71l-4.14-3.06-1.99 1.93c-.23.23-.42.42-.83.42z" />
    </svg>
  );
}

export function LineGlyph({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
      className={`shrink-0 text-line ${className}`}
    >
      <path d="M12 2.75c-5.24 0-9.5 3.44-9.5 7.68 0 3.8 3.38 6.98 7.94 7.58.31.07.73.2.84.47.09.24.06.62.03.87l-.13.81c-.04.24-.19.94.82.51 1.02-.43 5.48-3.23 7.48-5.53 1.38-1.51 2.02-3.05 2.02-5.19 0-4.24-4.26-7.68-9.52-7.68zM8.2 12.62H6.31c-.27 0-.5-.22-.5-.5V8.34c0-.28.23-.5.5-.5s.5.22.5.5v3.28H8.2c.28 0 .5.22.5.5s-.22.5-.5.5zm1.94-.5c0 .28-.22.5-.5.5s-.5-.22-.5-.5V8.34c0-.28.22-.5.5-.5s.5.22.5.5v3.78zm4.42 0c0 .21-.13.4-.34.47a.51.51 0 0 1-.55-.16l-1.93-2.63v2.32c0 .28-.22.5-.5.5s-.5-.22-.5-.5V8.34c0-.21.14-.4.34-.47.2-.07.43 0 .55.17l1.94 2.63V8.34c0-.28.22-.5.5-.5s.49.22.49.5v3.78zm3-2.39c.28 0 .5.22.5.5s-.22.5-.5.5h-1.39v.89h1.39c.28 0 .5.23.5.5 0 .28-.22.5-.5.5h-1.89c-.27 0-.5-.22-.5-.5V8.34c0-.28.23-.5.5-.5h1.89c.28 0 .5.22.5.5s-.22.5-.5.5h-1.39v.89h1.39z" />
    </svg>
  );
}

export function ChannelGlyph({
  channel,
  className,
}: {
  channel: MessagingChannel;
  className?: string;
}) {
  return channel === "telegram" ? (
    <TelegramGlyph className={className} />
  ) : (
    <LineGlyph className={className} />
  );
}
