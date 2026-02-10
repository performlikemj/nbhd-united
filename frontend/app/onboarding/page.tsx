"use client";

import { useEffect, useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import {
  useCheckoutMutation,
  useGenerateTelegramLinkMutation,
  useMeQuery,
  useOnboardMutation,
  useTelegramStatusQuery,
} from "@/lib/queries";
import type { TelegramLinkResponse } from "@/lib/api";

const autoOnboardAttempted = new Set<string>();

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

  const [checkoutError, setCheckoutError] = useState("");
  const [linkData, setLinkData] = useState<TelegramLinkResponse | null>(null);

  const tenant = me?.tenant;
  const hasTenant = Boolean(tenant);
  const subscriptionActive = tenant?.has_active_subscription ?? false;
  const runtimeReady = tenant?.status === "active";

  // Poll telegram status after payment
  const shouldPollTelegram = subscriptionActive;
  const { data: telegramStatus } = useTelegramStatusQuery(shouldPollTelegram);
  const telegramLinked = telegramStatus?.linked ?? false;

  // Auto-create tenant once per user when onboarding starts.
  useEffect(() => {
    const userId = me?.id;
    if (!userId || hasTenant || onboardingPending || onboardingSuccess) {
      return;
    }
    if (autoOnboardAttempted.has(userId)) {
      return;
    }

    autoOnboardAttempted.add(userId);
    void onboardTenant({ display_name: me.display_name }).catch(() => {
      // Allow a retry on subsequent renders if the request failed.
      autoOnboardAttempted.delete(userId);
    });
  }, [hasTenant, me, onboardTenant, onboardingPending, onboardingSuccess]);

  const handleCheckout = async () => {
    setCheckoutError("");
    try {
      const result = await checkout.mutateAsync("basic");
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

  return (
    <div className="space-y-4">
      <SectionCard
        title="Onboarding"
        subtitle="Complete these steps to activate your private Telegram assistant"
      >
        <ol className="space-y-3">
          {/* Step 1: Account created */}
          <li className="rounded-panel border border-ink/15 bg-white p-4">
            <p className="font-medium">1. Create your account</p>
            <p className="mt-1 text-sm text-ink/70">
              Your account has been created{me?.display_name ? ` as ${me.display_name}` : ""}.
            </p>
            <p className="mt-2 text-sm">
              Status: <StatusPill status="active" />
            </p>
          </li>

          {/* Step 2: Subscription */}
          <li className="rounded-panel border border-ink/15 bg-white p-4">
            <p className="font-medium">2. Start subscription</p>
            <p className="mt-1 text-sm text-ink/70">
              Complete Stripe checkout to trigger provisioning of your dedicated OpenClaw runtime.
            </p>
            <p className="mt-2 text-sm">
              Status: <StatusPill status={subscriptionActive ? "active" : "pending"} />
            </p>

            {hasTenant && !subscriptionActive && (
              <button
                type="button"
                onClick={handleCheckout}
                disabled={checkout.isPending}
                className="mt-3 rounded-full bg-ink px-4 py-2 text-sm text-white transition hover:bg-ink/85 disabled:cursor-not-allowed disabled:opacity-55"
              >
                {checkout.isPending ? "Redirecting..." : "Start subscription"}
              </button>
            )}

            {checkoutError && (
              <p className="mt-2 rounded-panel border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900">
                {checkoutError}
              </p>
            )}
          </li>

          {/* Step 3: Connect Telegram */}
          <li className="rounded-panel border border-ink/15 bg-white p-4">
            <p className="font-medium">3. Connect Telegram</p>
            <p className="mt-1 text-sm text-ink/70">
              Link your Telegram account so the assistant can message you.
            </p>
            <p className="mt-2 text-sm">
              Status: <StatusPill status={telegramLinked ? "active" : "pending"} />
            </p>

            {subscriptionActive && !telegramLinked && !linkData && (
              <button
                type="button"
                onClick={handleGenerateLink}
                disabled={generateLink.isPending}
                className="mt-3 rounded-full bg-ink px-4 py-2 text-sm text-white transition hover:bg-ink/85 disabled:cursor-not-allowed disabled:opacity-55"
              >
                {generateLink.isPending ? "Generating..." : "Connect Telegram"}
              </button>
            )}

            {subscriptionActive && !telegramLinked && linkData && (
              <div className="mt-3 space-y-3">
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
                    <p className="text-xs text-ink/50">
                      Waiting for you to connect...
                    </p>
                  </div>
                </div>
              </div>
            )}

            {telegramLinked && telegramStatus?.telegram_username && (
              <p className="mt-2 text-sm text-emerald-700">
                Connected as @{telegramStatus.telegram_username}
              </p>
            )}
          </li>

          {/* Step 4: Agent ready */}
          <li className="rounded-panel border border-ink/15 bg-white p-4">
            <p className="font-medium">4. Your agent is ready</p>
            <p className="mt-1 text-sm text-ink/70">
              {runtimeReady
                ? "Your assistant is active and ready to receive messages!"
                : tenant?.status === "provisioning"
                  ? "Your runtime is being provisioned. This usually takes a minute..."
                  : "Complete the steps above to activate your agent."}
            </p>
            <p className="mt-2 text-sm">
              Status: <StatusPill status={runtimeReady ? "active" : (tenant?.status ?? "pending")} />
            </p>
          </li>
        </ol>
      </SectionCard>
    </div>
  );
}
