"use client";

import Link from "next/link";
import { useState } from "react";

import { AppStoreBadge } from "@/components/app-store-badge";
import { MessagingChannelCard } from "@/components/channel/messaging-channel-card";
import type { MessagingChannel } from "@/components/channel/types";
import {
  useLineConnection,
  useTelegramConnection,
} from "@/components/channel/use-channel-connection";
import type { ChannelOutcome } from "@/components/onboarding/channel-outcome";
import { StatusPill } from "@/components/status-pill";
import { getErrorMessage } from "@/lib/errors";
import { usePushStatusQuery } from "@/lib/queries";

export function ChannelScene({
  onContinue,
}: {
  onContinue: (outcome: ChannelOutcome) => void;
}) {
  const [activeChannel, setActiveChannel] = useState<MessagingChannel | null>(null);
  const telegram = useTelegramConnection(activeChannel === "telegram");
  const line = useLineConnection(activeChannel === "line");
  const pushStatus = usePushStatusQuery();

  const telegramLinked = telegram.status?.linked ?? false;
  const lineLinked = line.status?.linked ?? false;
  const iosLinked = pushStatus.data?.registered ?? false;
  const effectiveActiveChannel =
    activeChannel === "telegram" && !telegramLinked
      ? "telegram"
      : activeChannel === "line" && !lineLinked
        ? "line"
        : null;

  const connectedOutcome: ChannelOutcome | null = iosLinked
    ? "ios"
    : telegramLinked
      ? "telegram"
      : lineLinked
        ? "line"
        : null;

  const openTelegram = async () => {
    setActiveChannel("telegram");
    if (!telegram.link) await telegram.generate();
  };

  const openLine = async () => {
    setActiveChannel("line");
    if (!line.link) await line.generate();
  };

  const switchToTelegram = () => {
    setActiveChannel("telegram");
    if (!telegram.link) void telegram.generate();
  };

  const switchToLine = () => {
    setActiveChannel("line");
    if (!line.link && !line.status?.quota?.exhausted) void line.generate();
  };

  return (
    <div className="flex w-full max-w-[580px] flex-col items-center text-center">
      <span className="mb-4 font-mono text-[11px] uppercase tracking-[0.2em] text-signal-text">
        STEP 2 OF 3
      </span>

      <h1 className="mb-3 font-display text-3xl font-extrabold leading-tight tracking-tight text-ink sm:text-5xl">
        Start talking with NBHD
      </h1>

      <p className="mb-8 max-w-[470px] text-[15px] leading-relaxed text-ink-muted">
        The iPhone app gives you voice, photos, and check-ins. Telegram and LINE
        are fully supported if you prefer a messenger.
      </p>

      <article className="w-full rounded-panel border border-accent/40 bg-accent/5 p-5 text-left shadow-panel backdrop-blur-md sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.18em] text-accent-hover">
              RECOMMENDED ON IPHONE
            </p>
            <div className="mt-3 flex items-center gap-2">
              <span className="text-lg text-accent" aria-hidden="true">
                ◇
              </span>
              <h2 className="font-headline text-xl font-bold text-ink">NBHD for iPhone</h2>
            </div>
          </div>
          {iosLinked ? <StatusPill status="active" /> : null}
        </div>

        <p className="mt-3 max-w-[460px] text-sm leading-relaxed text-ink-muted">
          Text, voice, photos, and daily check-ins in one private place.
        </p>

        <div className="mt-5 flex flex-wrap items-center gap-4">
          <AppStoreBadge height={48} />
          {iosLinked ? (
            <span
              className="inline-flex items-center gap-2 text-sm font-medium text-emerald-text"
              role="status"
            >
              <svg
                className="h-4 w-4"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth={2.5}
                aria-hidden="true"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
              </svg>
              Connected on iPhone
            </span>
          ) : null}
        </div>

        {pushStatus.isError ? (
          <div
            className="mt-4 rounded-panel border border-rose-border bg-rose-bg p-3"
            role="alert"
          >
            <p className="text-sm text-rose-text">
              We couldn&rsquo;t check your iPhone connection. {getErrorMessage(pushStatus.error)}
            </p>
            <button
              type="button"
              onClick={() => void pushStatus.refetch()}
              className="mt-2 min-h-[44px] rounded-full border border-rose-border px-4 py-2 text-sm font-medium text-rose-text transition hover:bg-rose-bg"
            >
              Check again
            </button>
          </div>
        ) : null}
      </article>

      <div className="my-6 flex w-full items-center gap-3" aria-hidden="true">
        <span className="h-px flex-1 bg-border" />
        <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.2em] text-ink-faint">
          ALSO CHAT IN
        </span>
        <span className="h-px flex-1 bg-border" />
      </div>

      <div className="grid w-full gap-3 text-left sm:grid-cols-2">
        <MessagingChannelCard
          channel="telegram"
          description="Chat with your assistant in Telegram."
          panelId="onboarding-telegram-pairing"
          linked={telegramLinked}
          connectedIdentity={
            telegram.status?.telegram_username
              ? `@${telegram.status.telegram_username}`
              : undefined
          }
          statusReady={telegram.statusReady}
          statusError={telegram.statusError}
          pairingOpen={effectiveActiveChannel === "telegram"}
          link={telegram.link}
          isGenerating={telegram.isGenerating}
          generationError={telegram.generationError}
          onConnect={openTelegram}
          onRegenerate={telegram.generate}
          onRetryStatus={telegram.retryStatus}
          onSwitch={line.statusReady ? switchToLine : undefined}
          onClose={() => setActiveChannel(null)}
          className={effectiveActiveChannel ? "sm:col-span-2" : ""}
        />

        <MessagingChannelCard
          channel="line"
          description="Chat with your assistant in LINE."
          panelId="onboarding-line-pairing"
          linked={lineLinked}
          connectedIdentity={line.status?.line_display_name}
          statusReady={line.statusReady}
          statusError={line.statusError}
          quotaExhausted={line.status?.quota?.exhausted ?? false}
          pairingOpen={effectiveActiveChannel === "line"}
          link={line.link}
          isGenerating={line.isGenerating}
          generationError={line.generationError}
          onConnect={openLine}
          onRegenerate={line.generate}
          onRetryStatus={line.retryStatus}
          onSwitch={telegram.statusReady ? switchToTelegram : undefined}
          onClose={() => setActiveChannel(null)}
          className={effectiveActiveChannel ? "sm:col-span-2" : ""}
        />
      </div>

      {connectedOutcome ? (
        <div className="mt-7 flex flex-col items-center">
          <button
            type="button"
            onClick={() => onContinue(connectedOutcome)}
            className="glow-purple min-h-[44px] rounded-full bg-accent px-10 py-3 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98]"
          >
            Continue
          </button>
          <p className="mt-3 text-xs text-ink-faint">
            You can add or remove channels later in Settings.
          </p>
        </div>
      ) : null}

      <button
        type="button"
        onClick={() => onContinue("skipped")}
        className="mt-5 min-h-[44px] rounded-full px-5 py-2 text-sm font-medium text-ink-muted transition hover:bg-surface-hover hover:text-ink"
      >
        Set up later
      </button>
      <p className="mt-1 text-xs text-ink-faint">
        You can connect anytime in{" "}
        <Link
          href="/settings/integrations"
          className="inline-flex min-h-[44px] items-center py-3 text-accent transition hover:text-accent-hover"
        >
          Settings → Integrations
        </Link>
        .
      </p>
    </div>
  );
}
