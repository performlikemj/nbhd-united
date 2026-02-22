"use client";

import clsx from "clsx";
import { useEffect, useState } from "react";

import { PersonaSelector } from "@/components/persona-selector";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import {
  useGenerateTelegramLinkMutation,
  useMeQuery,
  useOnboardMutation,
  usePersonasQuery,
  useProvisioningStatusQuery,
  useRetryProvisioningMutation,
  useTelegramStatusQuery,
} from "@/lib/queries";
import type { TelegramLinkResponse } from "@/lib/api";

type StepState = "completed" | "current" | "upcoming";

const STEP_LABELS = ["Account", "Persona", "Telegram", "Ready"] as const;

const STEP_TITLES = [
  "Create your account",
  "Choose your agent",
  "Connect Telegram",
  "Your agent is ready",
] as const;

const autoOnboardAttempted = new Set<string>();

function CheckIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
    </svg>
  );
}

export default function OnboardingPage() {
  const { data: me, isLoading, isFetching } = useMeQuery();
  const onboard = useOnboardMutation();
  const {
    mutateAsync: onboardTenant,
    isPending: onboardingPending,
    isSuccess: onboardingSuccess,
  } = onboard;
  const generateLink = useGenerateTelegramLinkMutation();
  const { data: personas } = usePersonasQuery();

  const [linkData, setLinkData] = useState<TelegramLinkResponse | null>(null);
  const [linkSecondsLeft, setLinkSecondsLeft] = useState(0);
  const [selectedPersona, setSelectedPersona] = useState("neighbor");
  const [showCta, setShowCta] = useState(false);

  const tenant = me?.tenant;
  const hasTenant = Boolean(tenant);
  const runtimeReady = tenant?.status === "active";
  const isTelegramLinkedInProfile = Boolean(tenant?.user.telegram_chat_id);

  const {
    data: provisioningStatus,
    isFetching: provisioningStatusFetching,
  } = useProvisioningStatusQuery(hasTenant && !runtimeReady);
  const retryProvisioningMutation = useRetryProvisioningMutation();

  const shouldPollTelegram = hasTenant;
  const { data: telegramStatus } = useTelegramStatusQuery(shouldPollTelegram);
  const telegramLinked = isTelegramLinkedInProfile || Boolean(telegramStatus?.linked);
  const ctaStorageKey = `nbhd:telegram-onboarding-cta:${me?.id ?? "anon"}`;
  const showTelegramCta = !isLoading && !isFetching && showCta && !telegramLinked;

  useEffect(() => {
    if (typeof window === "undefined" || !me?.id) {
      return;
    }
    if (telegramLinked) {
      window.localStorage.removeItem(ctaStorageKey);
      setShowCta(false);
      return;
    }

    const state = window.localStorage.getItem(ctaStorageKey);
    if (state === "dismissed" || state === "action" || state === "seen") {
      setShowCta(false);
      return;
    }

    setShowCta(true);
    window.localStorage.setItem(ctaStorageKey, "seen");
  }, [me?.id, telegramLinked, ctaStorageKey]);

  const persistCtaAction = (state: "dismissed" | "action") => {
    setShowCta(false);
    if (typeof window !== "undefined" && me?.id) {
      window.localStorage.setItem(ctaStorageKey, state);
    }
  };

  const jumpToTelegramStep = () => {
    persistCtaAction("action");
    if (hasTenant) {
      setActiveStep(2);
    } else {
      setActiveStep(1);
    }
  };
  // Compute step states (4 steps)
  const personaDone = hasTenant; // persona is selected when tenant is created
  const stepStates: StepState[] = [
    "completed", // Account — always done
    personaDone ? "completed" : "current",
    personaDone ? (telegramLinked ? "completed" : "current") : "upcoming",
    telegramLinked ? (runtimeReady ? "completed" : "current") : "upcoming",
  ];

  const currentStepIndex = stepStates.findIndex((s) => s === "current");
  const [activeStep, setActiveStep] = useState(currentStepIndex >= 0 ? currentStepIndex : 0);

  // Auto-clear expired link data and tick countdown
  useEffect(() => {
    if (!linkData) {
      setLinkSecondsLeft(0);
      return;
    }
    const expiresAt = new Date(linkData.expires_at).getTime();
    const tick = () => {
      const ms = expiresAt - Date.now();
      if (ms <= 0) {
        setLinkData(null);
        setLinkSecondsLeft(0);
      } else {
        setLinkSecondsLeft(Math.ceil(ms / 1000));
      }
    };
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, [linkData]);

  // Auto-advance to the current progress step when state changes
  useEffect(() => {
    const idx = stepStates.findIndex((s) => s === "current");
    if (idx >= 0) setActiveStep(idx);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stepStates.join(",")]);

  const handleSelectPersona = async () => {
    const userId = me?.id;
    if (!userId || hasTenant || onboardingPending || onboardingSuccess) {
      return;
    }
    if (autoOnboardAttempted.has(userId)) {
      return;
    }

    autoOnboardAttempted.add(userId);
    try {
      await onboardTenant({
        display_name: me.display_name,
        agent_persona: selectedPersona,
      });
    } catch {
      autoOnboardAttempted.delete(userId);
    }
  };

  const handleGenerateLink = async () => {
    try {
      const data = await generateLink.mutateAsync();
      setLinkData(data);
      persistCtaAction("action");
    } catch {
      // error handled by mutation state
    }
  };

  if (isLoading) {
    return (
      <div className="space-y-4">
        <SectionCardSkeleton lines={6} />
      </div>
    );
  }

  // Expanded content for each step
  function renderStepContent(stepIndex: number) {
    switch (stepIndex) {
      case 0:
        return (
          <p className="text-sm text-ink-muted">
            Signed in as {me?.display_name || me?.email || "you"}
          </p>
        );
      case 1:
        return (
          <>
            <p className="mt-1 text-sm text-ink-muted">
              Pick a personality for your AI assistant. You can change this later in settings.
            </p>
            {stepStates[1] !== "completed" && personas && (
              <div className="mt-4 space-y-4">
                <PersonaSelector
                  personas={personas}
                  selected={selectedPersona}
                  onSelect={setSelectedPersona}
                />
                <button
                  type="button"
                  onClick={handleSelectPersona}
                  disabled={onboardingPending}
                  className="rounded-full bg-accent px-5 py-2.5 text-sm font-medium text-white transition hover:bg-accent/85 disabled:cursor-not-allowed disabled:opacity-55"
                >
                  {onboardingPending ? "Setting up..." : "Continue"}
                </button>
              </div>
            )}
            {stepStates[1] === "completed" && (
              <p className="mt-1 text-sm text-signal">Persona selected</p>
            )}
          </>
        );
      case 2:
        return (
          <>
            <p className="mt-1 text-sm text-ink-muted">
              Link your Telegram account so the assistant can message you.
            </p>
            {stepStates[2] === "completed" && telegramStatus?.telegram_username && (
              <p className="mt-1 text-sm text-signal">
                Connected as @{telegramStatus.telegram_username}
              </p>
            )}
            {stepStates[2] === "current" && !linkData && (
              <button
                type="button"
                onClick={handleGenerateLink}
                disabled={generateLink.isPending}
                className="mt-4 rounded-full bg-accent px-5 py-2.5 text-sm font-medium text-white transition hover:bg-accent/85 disabled:cursor-not-allowed disabled:opacity-55"
              >
                {generateLink.isPending ? "Generating..." : "Connect Telegram"}
              </button>
            )}
            {stepStates[2] === "current" && linkData && (
              <div className="mt-4 space-y-3">
                <p className="text-sm text-ink-muted">
                  Scan the QR code or tap the link to connect your Telegram:
                </p>
                <div className="flex items-start gap-4">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={linkData.qr_code}
                    alt="Telegram QR Code"
                    className="h-40 w-40 rounded-panel border border-border"
                  />
                  <div className="space-y-2">
                    <a
                      href={linkData.deep_link}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-block rounded-full bg-[#0088cc] px-4 py-2 text-sm text-white transition hover:bg-[#0077b5]"
                    >
                      Open in Telegram
                    </a>
                    <div className="flex items-center gap-2">
                      <div className="h-2 w-2 rounded-full bg-accent animate-pulse" />
                      <p className="text-xs text-ink-faint">
                        Waiting for you to connect...
                      </p>
                    </div>
                    {linkSecondsLeft > 0 && (
                      <p className="text-xs text-ink-faint">
                        Link expires in {Math.floor(linkSecondsLeft / 60)}:{String(linkSecondsLeft % 60).padStart(2, "0")}
                      </p>
                    )}
                    <button
                      type="button"
                      onClick={handleGenerateLink}
                      disabled={generateLink.isPending}
                      className="text-xs text-accent underline hover:text-accent/75 disabled:opacity-50"
                    >
                      {generateLink.isPending ? "Generating..." : "Generate new link"}
                    </button>
                  </div>
                </div>
              </div>
            )}
            {stepStates[2] === "upcoming" && (
              <p className="mt-1 text-sm text-ink-faint">Complete your persona setup first.</p>
            )}
          </>
        );
      case 3:
        return (
          <>
            {stepStates[3] === "completed" ? (
              <>
                <p className="mt-1 text-sm text-signal">
                  Your assistant is active and ready to receive messages!
                </p>
                <p className="mt-3 rounded-panel border border-accent/20 bg-accent/5 px-3 py-2 text-sm text-ink-muted">
                  You&apos;re on a 7-day free trial. Subscribe anytime at Settings → Billing to keep your assistant after the trial.
                </p>
              </>
            ) : stepStates[3] === "current" ? (
              <>
                <div className="mt-3 flex items-center gap-3">
                  <div className="h-2.5 w-2.5 rounded-full bg-accent animate-pulse" />
                  <p className="text-sm text-ink-muted">
                    {tenant?.status === "provisioning"
                      ? "Your runtime is being provisioned. This usually takes a minute..."
                      : "Finalizing your agent setup..."}
                  </p>
                </div>
                {provisioningStatus && (
                  <p className="mt-2 text-xs text-ink-faint">
                    Status: <span className="font-mono">{provisioningStatus.status}</span>
                    {provisioningStatusFetching ? " · checking..." : ""}
                  </p>
                )}
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <button
                    type="button"
                    onClick={() => retryProvisioningMutation.mutate()}
                    disabled={retryProvisioningMutation.isPending}
                    className="rounded-full border border-accent/30 bg-accent/10 px-4 py-2 text-sm font-medium text-accent transition hover:bg-accent/15 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {retryProvisioningMutation.isPending ? "Retrying..." : "Retry provisioning"}
                  </button>
                  {retryProvisioningMutation.isSuccess && (
                    <span className="text-xs text-signal">Retry queued. We&apos;ll keep provisioning in the background.</span>
                  )}
                  {retryProvisioningMutation.isError && (
                    <span className="text-xs text-rose-500">Could not queue retry right now. Please try again shortly.</span>
                  )}
                </div>
                <p className="mt-3 rounded-panel border border-accent/20 bg-accent/5 px-3 py-2 text-sm text-ink-muted">
                  You&apos;re on a 7-day free trial. Subscribe anytime at Settings → Billing to keep your assistant after the trial.
                </p>
              </>
            ) : (
              <p className="mt-1 text-sm text-ink-faint">Complete the previous steps first.</p>
            )}
          </>
        );
      default:
        return null;
    }
  }

  return (
    <div className="space-y-4">
      {showTelegramCta && (
        <div className="relative rounded-panel border border-[#0088cc]/45 bg-gradient-to-r from-[#0088cc]/15 to-[#00a3ff]/10 px-4 py-3 shadow-[0_0_28px_rgba(0,136,204,0.35)]">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-ink">
            Your assistant is powered by Telegram
          </p>
          <p className="mt-1 text-sm text-ink-muted">
            Connect Telegram now to unlock messaging, reminders, and the upcoming feature set.
          </p>
          <button
            type="button"
            onClick={jumpToTelegramStep}
            className="mt-3 rounded-full bg-[#0088cc] px-4 py-2 text-sm font-medium text-white shadow-[0_0_20px_rgba(0,136,204,0.45)] transition hover:bg-[#0077b5] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0088cc]"
          >
            {hasTenant ? "Connect Telegram" : "Set up your assistant"}
          </button>
          <button
            type="button"
            onClick={() => persistCtaAction("dismissed")}
            className="ml-3 rounded-full border border-[#0088cc]/30 bg-surface/70 px-4 py-2 text-sm text-ink-muted transition hover:border-[#0088cc]/45 hover:text-ink"
          >
            Maybe later
          </button>
        </div>
      )}
      <SectionCard
        title="Onboarding"
        subtitle="Complete these steps to activate your private Telegram assistant"
      >
        {/* Horizontal stepper */}
        <div className="mb-8 flex items-start justify-between">
          {STEP_LABELS.map((label, i) => (
            <div key={label} className="flex flex-1 items-center">
              <button
                type="button"
                onClick={() => setActiveStep(i)}
                className="flex flex-col items-center cursor-pointer"
              >
                <div
                  className={clsx(
                    "flex h-10 w-10 items-center justify-center rounded-full border-2 font-mono text-sm font-semibold transition",
                    stepStates[i] === "completed" && "border-signal bg-signal text-white",
                    stepStates[i] === "current" && "border-accent bg-accent text-white",
                    stepStates[i] === "upcoming" && "border-border bg-surface text-ink-faint",
                    activeStep === i && "ring-2 ring-offset-2 ring-border",
                  )}
                >
                  {stepStates[i] === "completed" ? (
                    <CheckIcon className="h-5 w-5" />
                  ) : (
                    i + 1
                  )}
                </div>
                <span
                  className={clsx(
                    "mt-2 text-xs font-medium",
                    stepStates[i] === "completed" && "text-signal",
                    stepStates[i] === "current" && "text-accent",
                    stepStates[i] === "upcoming" && "text-ink-faint",
                  )}
                >
                  {label}
                </span>
              </button>

              {i < STEP_LABELS.length - 1 && (
                <div className="mx-2 mt-[-0.75rem] h-0.5 flex-1">
                  <div
                    className={clsx(
                      "h-full rounded-full transition-all",
                      stepStates[i + 1] !== "upcoming" ? "bg-signal" : "bg-border",
                    )}
                  />
                </div>
              )}
            </div>
          ))}
        </div>

        {/* Step detail cards */}
        <div className="space-y-3">
          {STEP_LABELS.map((_, i) => {
            const isExpanded = activeStep === i;
            const state = stepStates[i];

            if (isExpanded) {
              // Expanded card
              const borderColor =
                state === "completed" ? "border-signal/30" :
                state === "current" ? "border-accent/30" :
                "border-border";

              return (
                <div
                  key={i}
                  className={clsx(
                    "rounded-panel border bg-surface p-6 shadow-panel transition",
                    borderColor,
                  )}
                >
                  <div className="flex items-start gap-4">
                    <div
                      className={clsx(
                        "flex h-10 w-10 shrink-0 items-center justify-center rounded-full font-mono text-sm font-semibold text-white",
                        state === "completed" && "bg-signal",
                        state === "current" && "bg-accent",
                        state === "upcoming" && "bg-surface-hover",
                      )}
                    >
                      {state === "completed" ? (
                        <CheckIcon className="h-5 w-5" />
                      ) : (
                        i + 1
                      )}
                    </div>
                    <div className="flex-1">
                      <h3 className="text-lg font-semibold text-ink">{STEP_TITLES[i]}</h3>
                      {renderStepContent(i)}
                    </div>
                  </div>
                </div>
              );
            }

            // Compact card
            if (state === "completed") {
              return (
                <button
                  key={i}
                  type="button"
                  onClick={() => setActiveStep(i)}
                  className="flex w-full items-center gap-3 rounded-panel border border-signal/20 bg-signal-faint p-4 text-left cursor-pointer transition hover:border-signal/35"
                >
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-signal text-white">
                    <CheckIcon className="h-4 w-4" />
                  </div>
                  <div>
                    <p className="font-medium text-ink">{STEP_TITLES[i]}</p>
                    <p className="text-sm text-signal">
                      {i === 0 && `Signed in as ${me?.display_name || me?.email || "you"}`}
                      {i === 1 && "Persona selected"}
                      {i === 2 && `Connected ${telegramStatus?.telegram_username ? `as @${telegramStatus.telegram_username}` : ""}`}
                      {i === 3 && "Your agent is active and ready!"}
                    </p>
                  </div>
                </button>
              );
            }

            // Upcoming or non-active current
            return (
              <button
                key={i}
                type="button"
                onClick={() => setActiveStep(i)}
                className={clsx(
                  "flex w-full items-center gap-3 rounded-panel border p-4 text-left cursor-pointer transition",
                  state === "current"
                    ? "border-accent/20 bg-surface hover:border-accent/40"
                    : "border-border bg-surface/50 opacity-50 hover:opacity-70",
                )}
              >
                <div
                  className={clsx(
                    "flex h-8 w-8 shrink-0 items-center justify-center rounded-full border-2 font-mono text-sm",
                    state === "current"
                      ? "border-accent bg-accent text-white"
                      : "border-border text-ink-faint",
                  )}
                >
                  {i + 1}
                </div>
                <div>
                  <p className={clsx("font-medium", state === "current" ? "text-ink" : "text-ink-faint")}>
                    {STEP_TITLES[i]}
                  </p>
                  <p className={clsx("text-sm", state === "current" ? "text-accent" : "text-ink-faint")}>
                    {state === "current" ? "Click to expand" : "Complete the previous step first"}
                  </p>
                </div>
              </button>
            );
          })}
        </div>
      </SectionCard>
    </div>
  );
}
