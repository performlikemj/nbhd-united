"use client";

import { FormEvent, useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import { useCheckoutMutation, useMeQuery, useOnboardMutation } from "@/lib/queries";

export default function OnboardingPage() {
  const { data: me, isLoading } = useMeQuery();
  const onboard = useOnboardMutation();
  const checkout = useCheckoutMutation();

  const [chatId, setChatId] = useState("");
  const [onboardError, setOnboardError] = useState("");
  const [checkoutError, setCheckoutError] = useState("");

  const tenant = me?.tenant;
  const telegramConnected = Boolean(tenant?.user.telegram_chat_id ?? me?.telegram_chat_id);
  const subscriptionConnected = tenant?.has_active_subscription ?? false;
  const runtimeReady = tenant?.status === "active";

  const handleOnboard = async (e: FormEvent) => {
    e.preventDefault();
    setOnboardError("");
    const parsed = parseInt(chatId, 10);
    if (isNaN(parsed)) {
      setOnboardError("Please enter a valid numeric chat ID.");
      return;
    }
    try {
      await onboard.mutateAsync({ telegram_chat_id: parsed });
    } catch (err) {
      setOnboardError(err instanceof Error ? err.message : "Onboarding failed.");
    }
  };

  const handleCheckout = async () => {
    setCheckoutError("");
    try {
      const result = await checkout.mutateAsync("basic");
      window.location.assign(result.url);
    } catch (err) {
      setCheckoutError(err instanceof Error ? err.message : "Checkout failed.");
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
        subtitle="Complete three steps to activate your private Telegram assistant"
      >
        <ol className="space-y-3">
          <li className="rounded-panel border border-ink/15 bg-white p-4">
            <p className="font-medium">1. Connect Telegram identity</p>
            <p className="mt-1 text-sm text-ink/70">
              Attach your Telegram chat_id so the router can map you to your container.
            </p>
            <p className="mt-2 text-sm">
              Status: <StatusPill status={telegramConnected ? "active" : "pending"} />
            </p>

            {!telegramConnected && (
              <form onSubmit={handleOnboard} className="mt-3 flex items-end gap-2">
                <div className="flex-1">
                  <label
                    htmlFor="chatId"
                    className="block font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60"
                  >
                    Telegram Chat ID
                  </label>
                  <input
                    id="chatId"
                    type="text"
                    required
                    value={chatId}
                    onChange={(e) => setChatId(e.target.value)}
                    className="mt-1 w-full rounded-panel border border-ink/15 bg-white px-4 py-2 text-sm text-ink outline-none focus:border-ink/40"
                    placeholder="e.g. 123456789"
                  />
                </div>
                <button
                  type="submit"
                  disabled={onboard.isPending}
                  className="rounded-full bg-ink px-4 py-2 text-sm text-white transition hover:bg-ink/85 disabled:cursor-not-allowed disabled:opacity-55"
                >
                  {onboard.isPending ? "Saving..." : "Save"}
                </button>
              </form>
            )}

            {onboardError && (
              <p className="mt-2 rounded-panel border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-900">
                {onboardError}
              </p>
            )}
          </li>

          <li className="rounded-panel border border-ink/15 bg-white p-4">
            <p className="font-medium">2. Start subscription</p>
            <p className="mt-1 text-sm text-ink/70">
              Complete Stripe checkout to trigger provisioning of your dedicated OpenClaw runtime.
            </p>
            <p className="mt-2 text-sm">
              Status: <StatusPill status={subscriptionConnected ? "active" : "pending"} />
            </p>

            {telegramConnected && !subscriptionConnected && (
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

          <li className="rounded-panel border border-ink/15 bg-white p-4">
            <p className="font-medium">3. Wait for runtime provisioning</p>
            <p className="mt-1 text-sm text-ink/70">
              Container status must become active before messages are forwarded.
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
