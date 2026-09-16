"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import type { ChannelOutcome } from "@/components/onboarding/channel-outcome";
import { PausedScene } from "@/components/onboarding/paused-scene";
import {
  useLineStatusQuery,
  useMeQuery,
  useProvisioningStatusQuery,
  usePushStatusQuery,
  useRetryProvisioningMutation,
  useTelegramStatusQuery,
} from "@/lib/queries";

import {
  MeasureMargin,
  MeasureRail,
  MeasureScreen,
  measureStyles as s,
  settle,
  type MeasureCell,
} from "./measure-screen";

type ConnectedSurface = Exclude<ChannelOutcome, "skipped">;

// Elapsed-time affordance only. The first four ticks advance on the clock as
// an honest "about a minute" progress cue; only the last one ("Ready") is a
// real signal, driven by provisioning-status `ready`.
const STEP_TIMINGS = [0, 8, 18, 30, 48];
const STEP_LABELS = ["Private space", "AI model", "Secure links", "Warming up", "Ready"];
const STEP_COUNT = STEP_LABELS.length;
const CREEP_PERCENT = 17;
const STALL_SECONDS = 90;

function readyNote(surfaces: ConnectedSurface[]): string {
  if (surfaces.length > 1) {
    return "Open NBHD or a connected messenger whenever you’re ready to talk.";
  }
  if (surfaces[0] === "ios") {
    return "Open NBHD on your iPhone whenever you’re ready to talk.";
  }
  if (surfaces[0] === "telegram") {
    return "Open Telegram whenever you’re ready to talk.";
  }
  if (surfaces[0] === "line") {
    return "Open LINE whenever you’re ready to talk.";
  }
  return "Connect a channel anytime in Settings → Integrations.";
}

function isRetryUnavailable(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  const status = (error as Error & { status?: number }).status;
  return status === 400 && /unavailable for this tenant state/i.test(error.message);
}

export function LaunchSequence({ outcome }: { outcome: ChannelOutcome }) {
  const router = useRouter();
  const retryMutation = useRetryProvisioningMutation();
  // The retry endpoint answers 400 "unavailable for this tenant state" for a
  // suspended tenant — that is the paused case, not a stall.
  const retryUnavailable = isRetryUnavailable(retryMutation.error);
  const { data: me } = useMeQuery();
  const { data: provisioningStatus } = useProvisioningStatusQuery(!retryUnavailable);
  const suspended =
    provisioningStatus?.status === "suspended" || retryUnavailable;

  const pushStatus = usePushStatusQuery();
  const telegramStatus = useTelegramStatusQuery();
  const lineStatus = useLineStatusQuery();
  const [elapsed, setElapsed] = useState(0);
  // The tick position the rule is currently creeping away from; null = holding.
  const [creepFrom, setCreepFrom] = useState<number | null>(null);

  const isReady =
    provisioningStatus?.ready ??
    (provisioningStatus?.status === "active" &&
      Boolean(provisioningStatus?.container_id) &&
      Boolean(provisioningStatus?.container_fqdn));
  const isRetryingAutomatically = provisioningStatus?.status === "pending";
  const stalled = !isReady && (isRetryingAutomatically || elapsed > STALL_SECONDS);

  useEffect(() => {
    if (isReady) return;
    const interval = window.setInterval(() => setElapsed((value) => value + 1), 1000);
    return () => window.clearInterval(interval);
  }, [isReady]);

  // Ticks completed on the clock (0–4); Ready is never claimed by the clock.
  const timedDone = Math.min(
    STEP_TIMINGS.filter((time) => elapsed >= time).length - 1,
    STEP_COUNT - 1,
  );
  const done = isReady ? STEP_COUNT : timedDone;
  const base = (done / STEP_COUNT) * 100;

  // The rule keeps creeping through the current step: snap to the last tick,
  // then ease toward the next one over 40 s. Reduced motion holds the snap.
  useEffect(() => {
    if (isReady || stalled) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let second = 0;
    const first = window.requestAnimationFrame(() => {
      second = window.requestAnimationFrame(() => setCreepFrom(base));
    });
    return () => {
      window.cancelAnimationFrame(first);
      window.cancelAnimationFrame(second);
    };
  }, [base, isReady, stalled]);
  const creeping = !isReady && !stalled && creepFrom === base;
  const fill = creeping ? Math.min(base + CREEP_PERCENT, 100) : base;

  if (suspended) {
    return <PausedScene tenant={me?.tenant ?? null} />;
  }

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

  const cells: MeasureCell[] = STEP_LABELS.map((label, index) => ({
    label,
    state:
      index < done ? "done" : index === done && !stalled ? "current" : "todo",
  }));

  const state = isReady ? "ready" : stalled ? "retry" : "building";
  const tagStatus = isReady
    ? "Ready → Start"
    : stalled
      ? "Retry (rare)"
      : "In progress";
  const marginTitle = isReady ? "Ready" : stalled ? "Stopped" : "Building";

  return (
    <MeasureScreen
      state={state}
      tag="01 · Building"
      tagStatus={tagStatus}
      labelledBy="h-measure-building"
    >
      <MeasureMargin
        title={marginTitle}
        count={String(done)}
        countOf={String(STEP_COUNT)}
        live
      />

      <div className={s.content}>
        <div className={s.lead}>
          <h1
            className={s.display}
            id="h-measure-building"
            data-settle
            style={settle(2)}
          >
            {stalled ? (
              "That stalled. Nothing was lost."
            ) : (
              <>
                Making a space that belongs to{" "}
                <span className={s.key}>{isReady ? "you." : "one person."}</span>
              </>
            )}
          </h1>

          {isReady ? (
            <div className={s.actions} data-settle style={settle(3)}>
              <button
                type="button"
                className={s.btn}
                onClick={() => router.push("/journal")}
              >
                Start exploring
              </button>
            </div>
          ) : stalled ? (
            <div className={s.actions} data-settle style={settle(3)}>
              <button
                type="button"
                className={`${s.btn} ${s.btnLine}`}
                onClick={() => retryMutation.mutate()}
                disabled={retryMutation.isPending}
              >
                {retryMutation.isPending ? "Retrying…" : "Try again"}
              </button>
            </div>
          ) : null}
        </div>

        {isReady ? (
          <p className={`${s.status} ${s.green}`} data-settle style={settle(4)}>
            Ready.
          </p>
        ) : null}

        <MeasureRail
          percent={fill}
          cells={cells}
          creeping={creeping}
          stopped={stalled}
          ariaLabel={stalled ? "Build progress, stopped" : "Build progress"}
          valueNow={done}
          settleIndex={5}
        />

        {isReady ? (
          <p className={s.note} data-settle style={settle(6)}>
            {readyNote(connectedSurfaces)}
          </p>
        ) : stalled ? (
          <p
            className={`${s.label} ${s.labelDim} ${s.footLabel}`}
            data-settle
            style={settle(6)}
          >
            {retryMutation.isError && !retryUnavailable ? (
              <span className={s.alert} role="alert">
                Could not retry right now. Try again shortly.
              </span>
            ) : isRetryingAutomatically ? (
              "Retrying automatically · Nothing lost"
            ) : (
              "Nothing lost"
            )}
          </p>
        ) : (
          <p className={s.note} data-settle style={settle(6)}>
            About a minute.
          </p>
        )}
      </div>
    </MeasureScreen>
  );
}
