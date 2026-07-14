"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import type { MessagingChannel, PairingLink } from "@/components/channel/types";

const MOBILE_QUERY = "(max-width: 639px)";

const channelCopy = {
  telegram: {
    label: "Telegram",
    desktopInstruction: "Scan or open Telegram, then tap Start.",
    mobileInstruction: "Open Telegram, then tap Start.",
    actionClass: "border-telegram text-telegram hover:bg-telegram/10",
    dotClass: "bg-signal",
  },
  line: {
    label: "LINE",
    desktopInstruction:
      "Open LINE, follow NBHD if prompted, then send the prepared message.",
    mobileInstruction: "Open LINE, then send the prepared message.",
    actionClass: "border-line text-line hover:bg-line/10",
    dotClass: "bg-signal",
  },
} as const;

function subscribeToMobileQuery(callback: () => void) {
  const mediaQuery = window.matchMedia(MOBILE_QUERY);
  mediaQuery.addEventListener("change", callback);
  return () => mediaQuery.removeEventListener("change", callback);
}

function getMobileSnapshot(): boolean | null {
  return window.matchMedia(MOBILE_QUERY).matches;
}

function getMobileServerSnapshot(): boolean | null {
  return null;
}

function formatRemainingTime(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

interface ChannelPairingPanelProps {
  channel: MessagingChannel;
  panelId: string;
  link: PairingLink | null;
  isGenerating: boolean;
  generationError: string | null;
  statusError: string | null;
  onRegenerate: () => void | Promise<void>;
  onRetryStatus: () => void | Promise<unknown>;
  onSwitch?: () => void;
  onClose?: () => void;
}

export function ChannelPairingPanel({
  channel,
  panelId,
  link,
  isGenerating,
  generationError,
  statusError,
  onRegenerate,
  onRetryStatus,
  onSwitch,
  onClose,
}: ChannelPairingPanelProps) {
  const copy = channelCopy[channel];
  const isMobile = useSyncExternalStore(
    subscribeToMobileQuery,
    getMobileSnapshot,
    getMobileServerSnapshot,
  );
  const [now, setNow] = useState(() => Date.now());
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    panelRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!link) return;
    const interval = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [link]);

  const expiry = link ? Date.parse(link.expires_at) : Number.NaN;
  const remainingSeconds = Number.isFinite(expiry)
    ? Math.max(0, Math.ceil((expiry - now) / 1000))
    : 0;
  const expired = Boolean(link) && remainingSeconds === 0;

  return (
    <div
      ref={panelRef}
      id={panelId}
      tabIndex={-1}
      className="mt-4 rounded-panel border border-border bg-bg/35 p-4 sm:p-5"
      aria-label={`Connect ${copy.label}`}
    >
      {!link ? (
        <div className="flex min-h-32 flex-col items-center justify-center gap-3 text-center">
          {isGenerating ? (
            <p className="text-sm text-ink-muted" role="status">
              Creating a secure {copy.label} link…
            </p>
          ) : null}
          {generationError ? (
            <div
              className="w-full rounded-panel border border-rose-border bg-rose-bg p-3 text-left"
              role="alert"
            >
              <p className="text-sm text-rose-text">{generationError}</p>
              <button
                type="button"
                onClick={() => void onRegenerate()}
                className="mt-2 min-h-[44px] rounded-full border border-rose-border px-4 py-2 text-sm font-medium text-rose-text transition hover:bg-rose-bg disabled:cursor-not-allowed disabled:opacity-50"
                disabled={isGenerating}
              >
                Try again
              </button>
            </div>
          ) : null}
        </div>
      ) : (
        <div className={isMobile === false ? "grid gap-5 sm:grid-cols-[152px_1fr]" : ""}>
          {isMobile === false ? (
            // The viewport check is intentional: mobile markup never contains a QR image.
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={link.qr_code}
              alt={`QR code to connect NBHD in ${copy.label}`}
              className="h-[152px] w-[152px] rounded-panel border border-border bg-white"
            />
          ) : null}

          <div className="flex min-w-0 flex-col items-start">
            <p className="text-sm leading-relaxed text-ink-muted">
              {isMobile === false ? copy.desktopInstruction : copy.mobileInstruction}
            </p>
            {isMobile !== false ? (
              <p className="mt-1 text-xs text-ink-faint">
                We&rsquo;ll confirm here when you return.
              </p>
            ) : null}

            {expired ? (
              <div
                className="mt-4 w-full rounded-panel border border-amber-border bg-amber-bg p-3"
                role="alert"
              >
                <p className="text-sm text-amber-text">
                  This link has expired. Generate a new one to keep connecting.
                </p>
              </div>
            ) : (
              <a
                href={link.deep_link}
                target="_blank"
                rel="noopener noreferrer"
                className={`mt-4 inline-flex min-h-[44px] items-center justify-center rounded-full border px-5 py-2 text-sm font-semibold transition active:scale-[0.98] ${copy.actionClass}`}
              >
                Open in {copy.label}
              </a>
            )}

            {!expired ? (
              <div className="mt-4 flex items-center gap-2 text-xs text-ink-muted" role="status">
                <span className={`h-2 w-2 rounded-full ${copy.dotClass}`} aria-hidden="true" />
                <span>Waiting for {copy.label}…</span>
              </div>
            ) : null}
            <p className="mt-1 text-xs text-ink-faint">
              {expired
                ? "Link expired"
                : `Link expires in ${formatRemainingTime(remainingSeconds)}`}
            </p>
          </div>
        </div>
      )}

      {generationError && link ? (
        <p
          className="mt-4 rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text"
          role="alert"
        >
          {generationError}
        </p>
      ) : null}

      {statusError ? (
        <div
          className="mt-4 rounded-panel border border-rose-border bg-rose-bg p-3"
          role="alert"
        >
          <p className="text-sm text-rose-text">
            We couldn&rsquo;t confirm the connection. {statusError}
          </p>
          <button
            type="button"
            onClick={() => void onRetryStatus()}
            className="mt-2 min-h-[44px] rounded-full border border-rose-border px-4 py-2 text-sm font-medium text-rose-text transition hover:bg-rose-bg"
          >
            Check again
          </button>
        </div>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border pt-3">
        <button
          type="button"
          onClick={() => void onRegenerate()}
          disabled={isGenerating}
          className="min-h-[44px] text-sm font-medium text-accent transition hover:text-accent-hover disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isGenerating ? "Generating…" : "Generate a new link"}
        </button>
        {onSwitch ? (
          <button
            type="button"
            onClick={onSwitch}
            className="min-h-[44px] text-sm text-ink-muted transition hover:text-ink"
          >
            Use {channel === "telegram" ? "LINE" : "Telegram"} instead
          </button>
        ) : null}
        {onClose ? (
          <button
            type="button"
            onClick={onClose}
            className="min-h-[44px] text-sm text-ink-faint transition hover:text-ink-muted"
          >
            Close
          </button>
        ) : null}
      </div>
    </div>
  );
}
