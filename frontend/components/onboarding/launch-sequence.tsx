"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import type { ChannelOutcome } from "@/components/onboarding/channel-outcome";
import {
  useLineStatusQuery,
  useProvisioningStatusQuery,
  usePushStatusQuery,
  useRetryProvisioningMutation,
  useTelegramStatusQuery,
} from "@/lib/queries";

import { ConstellationProgress } from "./constellation-progress";

type ConnectedSurface = Exclude<ChannelOutcome, "skipped">;

const STEP_TIMINGS = [0, 8, 18, 30, 48];
const PRIVATE_SPACE_TIP =
  "Your assistant runs in its own private container — nobody else can see your conversations or data.";

function readySubtitle(surfaces: ConnectedSurface[]): string {
  if (surfaces.length > 1) {
    return "Your private AI assistant is active. Open NBHD or a connected messenger whenever you’re ready to talk.";
  }
  if (surfaces[0] === "ios") {
    return "Your private AI assistant is active. Open NBHD on your iPhone whenever you’re ready to talk.";
  }
  if (surfaces[0] === "telegram") {
    return "Your private AI assistant is active. Open Telegram whenever you’re ready to talk.";
  }
  if (surfaces[0] === "line") {
    return "Your private AI assistant is active. Open LINE whenever you’re ready to talk.";
  }
  return "Your private AI space is ready. Connect a channel anytime in Settings.";
}

function provisioningTips(surfaces: ConnectedSurface[]): string[] {
  if (surfaces.length === 0) {
    return [
      PRIVATE_SPACE_TIP,
      "You can connect the iPhone app, Telegram, or LINE later in Settings → Integrations.",
      "Your journal and settings will be ready here when provisioning finishes.",
    ];
  }

  const destination = surfaces.length > 1
    ? "NBHD or a connected messenger"
    : surfaces[0] === "ios"
      ? "NBHD on your iPhone"
      : surfaces[0] === "telegram"
        ? "Telegram"
        : "LINE";

  return [
    PRIVATE_SPACE_TIP,
    `Once launch finishes, open ${destination} whenever you’re ready to talk.`,
    "You can add or remove channels anytime in Settings → Integrations.",
  ];
}

export function LaunchSequence({ outcome }: { outcome: ChannelOutcome }) {
  const router = useRouter();
  const { data: provisioningStatus } = useProvisioningStatusQuery(true);
  const pushStatus = usePushStatusQuery();
  const telegramStatus = useTelegramStatusQuery();
  const lineStatus = useLineStatusQuery();
  const retryMutation = useRetryProvisioningMutation();
  const [elapsed, setElapsed] = useState(0);
  const [tipIndex, setTipIndex] = useState(0);
  const isReady =
    provisioningStatus?.status === "active" &&
    Boolean(provisioningStatus?.container_id);

  useEffect(() => {
    const interval = window.setInterval(() => setElapsed((value) => value + 1), 1000);
    return () => window.clearInterval(interval);
  }, []);

  useEffect(() => {
    const interval = window.setInterval(() => {
      setTipIndex((index) => (index + 1) % 3);
    }, 8000);
    return () => window.clearInterval(interval);
  }, []);

  const actualSurfaces: ConnectedSurface[] = [];
  if (pushStatus.data?.registered) actualSurfaces.push("ios");
  if (telegramStatus.data?.linked) actualSurfaces.push("telegram");
  if (lineStatus.data?.linked) actualSurfaces.push("line");

  const statusesResolved = Boolean(
    pushStatus.data && telegramStatus.data && lineStatus.data,
  );
  const fallbackSurfaces: ConnectedSurface[] =
    outcome === "skipped" ? [] : [outcome];
  const connectedSurfaces =
    actualSurfaces.length > 0 || statusesResolved
      ? actualSurfaces
      : fallbackSurfaces;
  const tips = provisioningTips(connectedSurfaces);

  const timedSteps = STEP_TIMINGS.filter((time) => elapsed >= time).length;
  const completedSteps = isReady ? 5 : Math.min(timedSteps, 4);

  return (
    <div className="flex w-full max-w-[640px] flex-col items-center text-center">
      <span className="mb-4 font-mono text-[11px] uppercase tracking-[0.2em] text-signal-text">
        STEP 3 OF 3
      </span>

      <h1 className="mb-4 whitespace-pre-line font-display text-3xl font-extrabold leading-tight tracking-tight text-ink sm:text-5xl">
        {isReady ? "Your universe is ready" : "Launching your\nuniverse"}
      </h1>

      <p className="mb-16 max-w-[440px] text-[15px] leading-relaxed text-ink-muted sm:mb-20">
        {isReady
          ? readySubtitle(connectedSurfaces)
          : "We’re building your private AI space. This takes about a minute."}
      </p>

      <ConstellationProgress completedSteps={completedSteps} />

      {isReady ? (
        <div className="mt-12 flex flex-col items-center gap-3">
          <button
            type="button"
            onClick={() => router.push("/journal")}
            className="glow-purple min-h-[44px] rounded-full bg-accent px-10 py-3 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98]"
          >
            Start exploring
          </button>
          {connectedSurfaces.length === 0 ? (
            <Link
              href="/settings/integrations"
              className="inline-flex min-h-[44px] items-center rounded-full px-4 py-2 text-sm font-medium text-accent transition hover:bg-accent/10 hover:text-accent-hover"
            >
              Settings → Integrations
            </Link>
          ) : (
            <p className="text-xs text-ink-faint">
              Channel controls stay available in Settings → Integrations.
            </p>
          )}
        </div>
      ) : (
        <>
          {elapsed > 90 ? (
            <div className="mt-6 flex flex-col items-center gap-2">
              <button
                type="button"
                onClick={() => retryMutation.mutate()}
                disabled={retryMutation.isPending}
                className="min-h-[44px] rounded-full border border-accent/30 bg-accent/10 px-5 py-2 text-sm font-medium text-accent-hover transition hover:bg-accent/15 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {retryMutation.isPending ? "Retrying…" : "Retry provisioning"}
              </button>
              {retryMutation.isError ? (
                <span className="text-xs text-rose-text" role="alert">
                  Could not retry right now. Try again shortly.
                </span>
              ) : null}
            </div>
          ) : null}

          <div className="mt-12 flex w-full flex-col items-center gap-4 rounded-panel border border-border bg-surface/60 p-5 backdrop-blur-xl sm:mt-16 sm:p-6">
            <div className="flex items-start gap-3 text-left sm:gap-4">
              <div className="shrink-0 rounded-full bg-signal-faint p-2.5">
                <svg
                  className="h-5 w-5 text-signal-text"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth={1.5}
                  aria-hidden="true"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456z" />
                </svg>
              </div>
              <p className="pt-0.5 text-sm leading-relaxed text-ink-muted">
                {tips[tipIndex]}
              </p>
            </div>
            <div className="mt-1 flex gap-2" aria-hidden="true">
              {tips.map((tip, index) => (
                <span
                  key={tip}
                  className={`h-1.5 w-1.5 rounded-full transition-colors duration-300 ${
                    index === tipIndex ? "bg-signal" : "bg-border-strong"
                  }`}
                />
              ))}
            </div>
          </div>

          <p className="mt-10 font-mono text-[10px] uppercase tracking-[0.15em] text-ink-faint">
            This usually takes under a minute
          </p>
        </>
      )}
    </div>
  );
}
