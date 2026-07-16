"use client";

import { useEffect, useState } from "react";

import { ChannelGlyph } from "@/components/channel/channel-glyphs";
import { ChannelPairingPanel } from "@/components/channel/channel-pairing-panel";
import type { MessagingChannel, PairingLink } from "@/components/channel/types";
import { StatusPill } from "@/components/status-pill";

const channelCopy = {
  telegram: {
    label: "Telegram",
    actionClass: "border-telegram text-telegram hover:bg-telegram/10",
  },
  line: {
    label: "LINE",
    actionClass: "border-line text-line hover:bg-line/10",
  },
} as const;

export const LINE_QUOTA_MESSAGE =
  "LINE’s monthly messaging allowance is used up across the platform. You’ll be able to connect LINE again at the start of next month.";

const UNLINK_CONFIRM_TIMEOUT_MS = 10_000;

interface MessagingChannelCardProps {
  channel: MessagingChannel;
  description: string;
  panelId: string;
  linked: boolean;
  connectedIdentity?: string;
  statusReady: boolean;
  statusError: string | null;
  quotaExhausted?: boolean;
  pairingOpen: boolean;
  link: PairingLink | null;
  isGenerating: boolean;
  generationError: string | null;
  onConnect: () => void | Promise<void>;
  onRegenerate: () => void | Promise<void>;
  onRetryStatus: () => void | Promise<unknown>;
  onSwitch?: () => void;
  onClose?: () => void;
  onUnlink?: () => void;
  unlinkPending?: boolean;
  className?: string;
}

export function MessagingChannelCard({
  channel,
  description,
  panelId,
  linked,
  connectedIdentity,
  statusReady,
  statusError,
  quotaExhausted = false,
  pairingOpen,
  link,
  isGenerating,
  generationError,
  onConnect,
  onRegenerate,
  onRetryStatus,
  onSwitch,
  onClose,
  onUnlink,
  unlinkPending = false,
  className = "",
}: MessagingChannelCardProps) {
  const [confirmingUnlink, setConfirmingUnlink] = useState(false);
  const copy = channelCopy[channel];
  const connectedText = connectedIdentity
    ? `${copy.label} connected as ${connectedIdentity}`
    : `${copy.label} connected`;

  useEffect(() => {
    if (!confirmingUnlink) return;

    const timeout = window.setTimeout(
      () => setConfirmingUnlink(false),
      UNLINK_CONFIRM_TIMEOUT_MS,
    );
    return () => window.clearTimeout(timeout);
  }, [confirmingUnlink]);

  return (
    <article
      className={`rounded-panel border border-border bg-surface-elevated p-4 transition-colors sm:p-5 ${className}`}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <ChannelGlyph channel={channel} />
          <h3 className="font-headline text-base font-semibold text-ink">{copy.label}</h3>
        </div>
        {linked ? <StatusPill status="active" /> : null}
      </div>

      <p className="mt-2 text-sm leading-relaxed text-ink-muted">{description}</p>

      {linked ? (
        <>
          <div
            className="mt-4 flex items-start gap-2 rounded-panel border border-emerald-text/20 bg-emerald-bg p-3 text-sm text-emerald-text"
            role="status"
            aria-live="polite"
          >
            <svg
              className="mt-0.5 h-4 w-4 shrink-0"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={2.5}
              aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
            </svg>
            <span>{connectedText}</span>
          </div>

          {channel === "line" && quotaExhausted ? (
            <div className="mt-3 rounded-panel border border-amber-border bg-amber-bg p-3">
              <p className="text-sm text-amber-text">
                {LINE_QUOTA_MESSAGE} Your connection will remain in place.
              </p>
            </div>
          ) : null}

          {onUnlink ? (
            confirmingUnlink ? (
              <div
                className="mt-4 rounded-panel border border-rose-border bg-rose-bg p-3"
                role="alert"
              >
                <p className="font-headline text-sm font-semibold text-rose-text">
                  Ready to unlink {copy.label}?
                </p>
                <p className="mt-1 text-sm leading-relaxed text-ink-muted">
                  Messages through {copy.label} will stop until you connect it again.
                </p>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    disabled={unlinkPending}
                    onClick={() => {
                      onUnlink();
                      setConfirmingUnlink(false);
                    }}
                    className="min-h-[44px] rounded-full bg-rose-text/90 px-4 py-2 text-sm font-semibold text-white transition hover:bg-rose-text disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {unlinkPending ? "Unlinking…" : `Yes, unlink ${copy.label}`}
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmingUnlink(false)}
                    className="min-h-[44px] rounded-full border border-border-strong px-4 py-2 text-sm text-ink-muted transition hover:bg-surface-hover hover:text-ink"
                  >
                    Keep connected
                  </button>
                </div>
                <p className="mt-2 text-xs text-ink-faint">
                  This confirmation closes automatically after 10 seconds.
                </p>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmingUnlink(true)}
                className="mt-4 min-h-[44px] rounded-full border border-border-strong px-4 py-2 text-sm text-ink-muted transition hover:bg-surface-hover hover:text-ink"
              >
                Unlink
              </button>
            )
          ) : null}
        </>
      ) : channel === "line" && quotaExhausted ? (
        <>
          <div className="mt-3 rounded-panel border border-amber-border bg-amber-bg p-3">
            <p className="text-sm text-amber-text">{LINE_QUOTA_MESSAGE}</p>
          </div>
          <button
            type="button"
            disabled
            aria-disabled="true"
            title="LINE monthly messaging allowance reached. Connectable again at the start of next month."
            className="mt-4 min-h-[44px] cursor-not-allowed rounded-full border border-border px-4 py-2 text-sm text-ink-faint opacity-50"
          >
            Connect LINE
          </button>
        </>
      ) : pairingOpen ? (
        <ChannelPairingPanel
          channel={channel}
          panelId={panelId}
          link={link}
          isGenerating={isGenerating}
          generationError={generationError}
          statusError={statusError}
          onRegenerate={onRegenerate}
          onRetryStatus={onRetryStatus}
          onSwitch={onSwitch}
          onClose={onClose}
        />
      ) : (
        <>
          {statusError ? (
            <div
              className="mt-3 rounded-panel border border-rose-border bg-rose-bg p-3"
              role="alert"
            >
              <p className="text-sm text-rose-text">
                We couldn&rsquo;t check your {copy.label} connection. {statusError}
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
          <button
            type="button"
            onClick={() => void onConnect()}
            disabled={!statusReady || Boolean(statusError) || isGenerating}
            aria-expanded={false}
            aria-controls={panelId}
            className={`mt-4 min-h-[44px] rounded-full border px-4 py-2 text-sm font-semibold transition active:scale-[0.98] disabled:cursor-not-allowed disabled:border-border disabled:text-ink-faint disabled:opacity-50 ${copy.actionClass}`}
          >
            {!statusReady ? "Checking…" : isGenerating ? "Generating…" : `Connect ${copy.label}`}
          </button>
        </>
      )}
    </article>
  );
}
