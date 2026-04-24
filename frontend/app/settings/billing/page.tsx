"use client";

import Link from "next/link";
import { useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import { useCheckoutMutation, useStripePortalMutation, useTenantQuery } from "@/lib/queries";
import { modelSummary } from "@/lib/models";

export default function SettingsBillingPage() {
  const { data: tenant, isLoading, error } = useTenantQuery();
  const portalMutation = useStripePortalMutation();
  const checkoutMutation = useCheckoutMutation();
  const [portalError, setPortalError] = useState("");
  const [checkoutError, setCheckoutError] = useState("");

  const openPortal = async () => {
    setPortalError("");
    try {
      const result = await portalMutation.mutateAsync();
      window.location.assign(result.url);
    } catch (err) {
      const raw = err instanceof Error ? err.message : "";
      try {
        const body = JSON.parse(raw);
        setPortalError(body.detail || "Something went wrong. Please try again.");
      } catch {
        setPortalError("Something went wrong. Please try again.");
      }
    }
  };

  const handleCheckout = async () => {
    setCheckoutError("");
    try {
      const result = await checkoutMutation.mutateAsync();
      window.location.assign(result.url);
    } catch (err) {
      setCheckoutError(err instanceof Error ? err.message : "Checkout failed.");
    }
  };

  if (isLoading) {
    return (
      <div className="space-y-4">
        <SectionCardSkeleton lines={5} />
      </div>
    );
  }

  const hasTenant = Boolean(tenant) && !error;

  const trialDays = tenant?.trial_days_remaining ?? null;
  const isTrialActive = Boolean(tenant?.is_trial && trialDays !== null && trialDays > 0);
  const isTrialExpired = Boolean(tenant?.is_trial && tenant?.trial_days_remaining === 0);
  const isTelegramLinked = Boolean(tenant?.user.telegram_chat_id);
  const onboardingCta = isTelegramLinked
    ? { href: "/settings/integrations", label: "Review messaging setup" }
    : { href: "/onboarding", label: "Connect messaging" };

  return (
    <div className="space-y-4">
      <SectionCard
        title="Billing"
        subtitle="Stripe subscription and portal controls"
      >
        {hasTenant && tenant ? (
          <div className="space-y-4">
            {tenant.is_trial && (
              <div
                className={`rounded-panel border px-4 py-3 text-sm ${
                  isTrialActive
                    ? "border-accent/25 bg-accent/5 text-ink-muted"
                    : "border-rose-border bg-rose-bg text-rose-text"
                }`}
              >
                {isTrialActive
                  ? `You're on a free trial! ${trialDays} days remaining. Subscribe to keep your assistant.`
                  : "Your free trial has ended. Subscribe to reactivate your assistant."}
              </div>
            )}

            <dl className="grid gap-3 text-sm sm:grid-cols-2">
              <div className="rounded-panel border border-border bg-surface-elevated p-4 min-w-0 overflow-visible">
                <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Plan</dt>
                <dd className="mt-1 text-lg font-semibold">$12/mo</dd>
                <p className="mt-1 text-xs text-ink-muted">{modelSummary()}</p>
              </div>
              <div className="rounded-panel border border-border bg-surface-elevated p-4 min-w-0 overflow-visible">
                <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Agent Status</dt>
                <dd className="mt-1"><StatusPill status={tenant.status} /></dd>
                {tenant.status === "suspended" && (
                  <p className="mt-2 text-xs text-ink-faint leading-relaxed">
                    Your agent is paused because there&apos;s no active subscription.
                    Running your agent requires cloud servers on Azure and AI model
                    tokens — every reply has a real cost. We&apos;re upfront about this
                    because you deserve to know where your money goes.
                  </p>
                )}
              </div>
              <div className="rounded-panel border border-border bg-surface-elevated p-4 sm:col-span-2">
                <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Subscription</dt>
                <dd className="mt-1"><StatusPill status={tenant.has_active_subscription ? "active" : "pending"} /></dd>
              </div>
            </dl>

            {!tenant.has_active_subscription && (
              <div className={`rounded-panel border border-dashed p-5 text-center ${
                isTrialActive
                  ? "border-accent/30 bg-accent/5"
                  : isTrialExpired
                    ? "border-rose-border bg-rose-bg"
                    : "border-amber-border bg-amber-bg"
              }`}>
                <p className="text-sm font-medium text-ink">
                  {isTrialActive
                    ? "Trial mode is active. You can still subscribe now to keep your assistant after 7 days."
                    : isTrialExpired
                      ? "Your trial has ended. Subscribe to reactivate your assistant."
                      : "Your tenant is provisioned but you need an active subscription to use your agent."}
                </p>
                <button
                  type="button"
                  onClick={handleCheckout}
                  disabled={checkoutMutation.isPending}
                  className="mt-3 rounded-full bg-accent px-5 py-2.5 text-sm font-medium text-white transition hover:bg-accent/85 disabled:cursor-not-allowed disabled:opacity-55"
                >
                  {checkoutMutation.isPending ? "Redirecting..." : "Subscribe — $12/mo"}
                </button>
              </div>
            )}

            <div className="flex flex-wrap items-center gap-3">
              {tenant.has_active_subscription && (
                <button
                  className="rounded-full bg-accent px-4 py-2 text-sm text-white transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-55"
                  type="button"
                  onClick={openPortal}
                  disabled={portalMutation.isPending}
                >
                  {portalMutation.isPending ? "Opening..." : "Open Stripe Portal"}
                </button>
              )}
            </div>
            <p className="text-xs text-ink-faint">
              By subscribing, you agree to our{" "}
              <Link href="/legal/terms" className="underline hover:text-ink-muted">Terms</Link>,{" "}
              <Link href="/legal/privacy" className="underline hover:text-ink-muted">Privacy Policy</Link>, and{" "}
              <Link href="/legal/refund" className="underline hover:text-ink-muted">Refund Policy</Link>.
            </p>

            {portalError && (
              <p className="rounded-panel border border-amber-border bg-amber-bg p-3 text-sm text-amber-text">
                {portalError}
              </p>
            )}
            {checkoutError && (
              <p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">
                {checkoutError}
              </p>
            )}
          </div>
        ) : (
          <div className="flex flex-col items-center py-8 text-center">
            <div className="flex h-16 w-16 items-center justify-center rounded-full bg-surface-hover">
              <svg className="h-8 w-8 text-ink-faint" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 8.25h19.5M2.25 9h19.5m-16.5 5.25h6m-6 2.25h3m-3.75 3h15a2.25 2.25 0 002.25-2.25V6.75A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25v10.5A2.25 2.25 0 004.5 19.5z" />
              </svg>
            </div>
            <h3 className="mt-4 text-lg font-semibold text-ink">No subscription yet</h3>
            <p className="mt-2 max-w-sm text-sm text-ink-muted">
              Complete onboarding to start your 7-day free trial, then subscribe for $12/mo.
            </p>
            <div className="mt-6">
              <Link
                href={onboardingCta.href}
                className="inline-block rounded-full bg-accent px-5 py-2.5 text-sm font-medium text-white transition hover:bg-accent-hover"
              >
                {onboardingCta.label}
              </Link>
            </div>
            <p className="mt-3 text-xs text-ink-faint">
              By subscribing, you agree to our{" "}
              <Link href="/legal/terms" className="underline hover:text-ink-muted">Terms</Link>,{" "}
              <Link href="/legal/privacy" className="underline hover:text-ink-muted">Privacy Policy</Link>, and{" "}
              <Link href="/legal/refund" className="underline hover:text-ink-muted">Refund Policy</Link>.
            </p>
            {checkoutError && (
              <p className="mt-4 rounded-panel border border-rose-border bg-rose-bg px-4 py-2.5 text-sm text-rose-text">
                {checkoutError}
              </p>
            )}
          </div>
        )}
      </SectionCard>
    </div>
  );
}
