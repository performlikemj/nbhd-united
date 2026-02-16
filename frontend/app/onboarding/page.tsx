"use client";

import clsx from "clsx";
import { useEffect, useState } from "react";

import { PersonaSelector } from "@/components/persona-selector";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import {
  useCheckoutMutation,
  useGenerateTelegramLinkMutation,
  useMeQuery,
  useOnboardMutation,
  usePersonasQuery,
  useTelegramStatusQuery,
} from "@/lib/queries";
import type { TelegramLinkResponse } from "@/lib/api";

type StepState = "completed" | "current" | "upcoming";

const STEP_LABELS = ["Account", "Persona", "Subscribe", "Telegram", "Ready"] as const;

const STEP_TITLES = [
  "Create your account",
  "Choose your agent",
  "Start your subscription",
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
  const { data: me, isLoading } = useMeQuery();
  const onboard = useOnboardMutation();
  const {
    mutateAsync: onboardTenant,
    isPending: onboardingPending,
    isSuccess: onboardingSuccess,
  } = onboard;
  const checkout = useCheckoutMutation();
  const generateLink = useGenerateTelegramLinkMutation();
  const { data: personas } = usePersonasQuery();

  const [checkoutError, setCheckoutError] = useState("");
  const [linkData, setLinkData] = useState<TelegramLinkResponse | null>(null);
  const [linkSecondsLeft, setLinkSecondsLeft] = useState(0);
  const [selectedPersona, setSelectedPersona] = useState("neighbor");
  const [selectedTier, setSelectedTier] = useState<"starter" | "premium" | "byok">("starter");

  const tenant = me?.tenant;
  const hasTenant = Boolean(tenant);
  const subscriptionActive = tenant?.has_active_subscription ?? false;
  const runtimeReady = tenant?.status === "active";

  const shouldPollTelegram = subscriptionActive;
  const { data: telegramStatus } = useTelegramStatusQuery(shouldPollTelegram);
  const telegramLinked = telegramStatus?.linked ?? false;

  // Compute step states (5 steps now)
  const personaDone = hasTenant; // persona is selected when tenant is created
  const stepStates: StepState[] = [
    "completed", // Account — always done
    personaDone ? "completed" : "current",
    subscriptionActive ? "completed" : personaDone ? "current" : "upcoming",
    telegramLinked ? "completed" : subscriptionActive && !telegramLinked ? "current" : "upcoming",
    runtimeReady ? "completed" : telegramLinked && !runtimeReady ? "current" : "upcoming",
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

  const handleCheckout = async () => {
    setCheckoutError("");
    try {
      const result = await checkout.mutateAsync(selectedTier);
      window.location.assign(result.url);
    } catch (err) {
      setCheckoutError(err instanceof Error ? err.message : "Checkout failed.");
    }
  };

  const handleGenerateLink = async () => {
    try {
      const data = await generateLink.mutateAsync();
      setLinkData(data);
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
          <p className="text-sm text-ink/60">
            Signed in as {me?.display_name || me?.email || "you"}
          </p>
        );
      case 1:
        return (
          <>
            <p className="mt-1 text-sm text-ink/65">
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
            <p className="mt-1 text-sm text-ink/65">
              Choose a plan and complete Stripe checkout to provision your dedicated runtime.
            </p>
            {stepStates[2] !== "completed" && (
              <>
                <div className="mt-4 grid gap-3 sm:grid-cols-3">
                  {([
                    {
                      tier: "starter" as const,
                      name: "Starter",
                      price: "$8/mo",
                      model: "Kimi K2.5",
                      features: ["50 messages/day", "Basic assistant"],
                    },
                    {
                      tier: "premium" as const,
                      name: "Premium",
                      price: "$25/mo",
                      model: "Claude Sonnet & Opus",
                      features: ["200 messages/day", "Best-in-class AI models"],
                    },
                    {
                      tier: "byok" as const,
                      name: "Bring Your Own Key",
                      price: "$8/mo",
                      model: "Your choice",
                      features: ["200 messages/day", "Use your own API key"],
                    },
                  ]).map((plan) => (
                    <button
                      key={plan.tier}
                      type="button"
                      onClick={() => setSelectedTier(plan.tier)}
                      className={`rounded-panel border-2 p-4 text-left transition ${
                        selectedTier === plan.tier
                          ? "border-accent bg-accent/5"
                          : "border-ink/15 bg-white hover:border-ink/30"
                      }`}
                    >
                      <div className="flex items-baseline justify-between">
                        <p className="text-sm font-semibold text-ink">{plan.name}</p>
                        <p className="text-sm font-bold text-ink">{plan.price}</p>
                      </div>
                      <p className="mt-1 text-xs font-medium text-accent">{plan.model}</p>
                      <ul className="mt-2 space-y-1">
                        {plan.features.map((f) => (
                          <li key={f} className="text-xs text-ink/60">• {f}</li>
                        ))}
                      </ul>
                      <div className="mt-3">
                        <span
                          className={`inline-block rounded-full px-3 py-1 text-xs font-medium transition ${
                            selectedTier === plan.tier
                              ? "bg-accent text-white"
                              : "bg-ink/5 text-ink/50"
                          }`}
                        >
                          {selectedTier === plan.tier ? "Selected" : "Select"}
                        </span>
                      </div>
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={handleCheckout}
                  disabled={checkout.isPending}
                  className="mt-4 rounded-full bg-accent px-5 py-2.5 text-sm font-medium text-white transition hover:bg-accent/85 disabled:cursor-not-allowed disabled:opacity-55"
                >
                  {checkout.isPending ? "Redirecting to Stripe..." : `Subscribe to ${selectedTier.charAt(0).toUpperCase() + selectedTier.slice(1)}`}
                </button>
                <p className="mt-3 text-xs text-ink/45">
                  By subscribing, you agree to our{" "}
                  <a href="/legal/terms" className="underline hover:text-ink/70">Terms</a>,{" "}
                  <a href="/legal/privacy" className="underline hover:text-ink/70">Privacy Policy</a>, and{" "}
                  <a href="/legal/refund" className="underline hover:text-ink/70">Refund Policy</a>.
                </p>
              </>
            )}
            {stepStates[2] === "completed" && (
              <p className="mt-1 text-sm text-signal">Subscription active</p>
            )}
            {checkoutError && (
              <p className="mt-3 rounded-panel border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900">
                {checkoutError}
              </p>
            )}
          </>
        );
      case 3:
        return (
          <>
            <p className="mt-1 text-sm text-ink/65">
              Link your Telegram account so the assistant can message you.
            </p>
            {stepStates[3] === "completed" && telegramStatus?.telegram_username && (
              <p className="mt-1 text-sm text-signal">
                Connected as @{telegramStatus.telegram_username}
              </p>
            )}
            {stepStates[3] === "current" && !linkData && (
              <button
                type="button"
                onClick={handleGenerateLink}
                disabled={generateLink.isPending}
                className="mt-4 rounded-full bg-accent px-5 py-2.5 text-sm font-medium text-white transition hover:bg-accent/85 disabled:cursor-not-allowed disabled:opacity-55"
              >
                {generateLink.isPending ? "Generating..." : "Connect Telegram"}
              </button>
            )}
            {stepStates[3] === "current" && linkData && (
              <div className="mt-4 space-y-3">
                <p className="text-sm text-ink/70">
                  Scan the QR code or tap the link to connect your Telegram:
                </p>
                <div className="flex items-start gap-4">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={linkData.qr_code}
                    alt="Telegram QR Code"
                    className="h-40 w-40 rounded-panel border border-ink/15"
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
                      <p className="text-xs text-ink/50">
                        Waiting for you to connect...
                      </p>
                    </div>
                    {linkSecondsLeft > 0 && (
                      <p className="text-xs text-ink/40">
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
            {stepStates[3] === "upcoming" && (
              <p className="mt-1 text-sm text-ink/35">Complete the subscription step first</p>
            )}
          </>
        );
      case 4:
        return (
          <>
            {stepStates[4] === "completed" ? (
              <p className="mt-1 text-sm text-signal">
                Your assistant is active and ready to receive messages!
              </p>
            ) : stepStates[4] === "current" ? (
              <div className="mt-3 flex items-center gap-3">
                <div className="h-2.5 w-2.5 rounded-full bg-accent animate-pulse" />
                <p className="text-sm text-ink/65">
                  {tenant?.status === "provisioning"
                    ? "Your runtime is being provisioned. This usually takes a minute..."
                    : "Finalizing your agent setup..."}
                </p>
              </div>
            ) : (
              <p className="mt-1 text-sm text-ink/35">Complete the previous steps first</p>
            )}
          </>
        );
      default:
        return null;
    }
  }

  return (
    <div className="space-y-4">
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
                    stepStates[i] === "upcoming" && "border-ink/15 bg-white text-ink/35",
                    activeStep === i && "ring-2 ring-offset-2 ring-ink/20",
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
                    stepStates[i] === "upcoming" && "text-ink/40",
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
                      stepStates[i + 1] !== "upcoming" ? "bg-signal" : "bg-ink/10",
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
                "border-ink/15";

              return (
                <div
                  key={i}
                  className={clsx(
                    "rounded-panel border bg-white p-6 shadow-panel transition",
                    borderColor,
                  )}
                >
                  <div className="flex items-start gap-4">
                    <div
                      className={clsx(
                        "flex h-10 w-10 shrink-0 items-center justify-center rounded-full font-mono text-sm font-semibold text-white",
                        state === "completed" && "bg-signal",
                        state === "current" && "bg-accent",
                        state === "upcoming" && "bg-ink/20",
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
                  className="flex w-full items-center gap-3 rounded-panel border border-signal/20 bg-signal/5 p-4 text-left cursor-pointer transition hover:border-signal/35"
                >
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-signal text-white">
                    <CheckIcon className="h-4 w-4" />
                  </div>
                  <div>
                    <p className="font-medium text-ink">{STEP_TITLES[i]}</p>
                    <p className="text-sm text-signal">
                      {i === 0 && `Signed in as ${me?.display_name || me?.email || "you"}`}
                      {i === 1 && "Persona selected"}
                      {i === 2 && "Subscription active"}
                      {i === 3 && (telegramStatus?.telegram_username ? `Connected as @${telegramStatus.telegram_username}` : "Connected")}
                      {i === 4 && "Your assistant is active and ready!"}
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
                    ? "border-accent/20 bg-white hover:border-accent/40"
                    : "border-ink/10 bg-white/50 opacity-50 hover:opacity-70",
                )}
              >
                <div
                  className={clsx(
                    "flex h-8 w-8 shrink-0 items-center justify-center rounded-full border-2 font-mono text-sm",
                    state === "current"
                      ? "border-accent bg-accent text-white"
                      : "border-ink/15 text-ink/30",
                  )}
                >
                  {i + 1}
                </div>
                <div>
                  <p className={clsx("font-medium", state === "current" ? "text-ink" : "text-ink/50")}>
                    {STEP_TITLES[i]}
                  </p>
                  <p className={clsx("text-sm", state === "current" ? "text-accent" : "text-ink/35")}>
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
