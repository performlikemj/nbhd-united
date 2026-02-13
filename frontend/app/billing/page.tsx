"use client";

import Link from "next/link";
import { useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import { useCheckoutMutation, useStripePortalMutation, useTenantQuery } from "@/lib/queries";

export default function BillingPage() {
  const { data: tenant, isLoading, error } = useTenantQuery();
  const portalMutation = useStripePortalMutation();
  const checkoutMutation = useCheckoutMutation();
  const [portalError, setPortalError] = useState("");
  const [checkoutError, setCheckoutError] = useState("");
  const [selectedTier, setSelectedTier] = useState<"basic" | "plus">("basic");

  const openPortal = async () => {
    setPortalError("");
    try {
      const result = await portalMutation.mutateAsync();
      window.location.assign(result.url);
    } catch {
      setPortalError("Could not open customer portal. Ensure a Stripe customer is linked.");
    }
  };

  const handleCheckout = async () => {
    setCheckoutError("");
    try {
      const result = await checkoutMutation.mutateAsync(selectedTier);
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

  return (
    <div className="space-y-4">
      <SectionCard
        title="Billing"
        subtitle="Stripe subscription and portal controls"
      >
        {hasTenant && tenant ? (
          <div className="space-y-4">
            <dl className="grid gap-3 text-sm sm:grid-cols-2">
              <div className="rounded-panel border border-ink/15 bg-white p-4">
                <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Tier</dt>
                <dd className="mt-1 text-lg font-semibold uppercase">{tenant.model_tier}</dd>
              </div>
              <div className="rounded-panel border border-ink/15 bg-white p-4">
                <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Tenant State</dt>
                <dd className="mt-1"><StatusPill status={tenant.status} /></dd>
              </div>
              <div className="rounded-panel border border-ink/15 bg-white p-4 sm:col-span-2">
                <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Subscription</dt>
                <dd className="mt-1"><StatusPill status={tenant.has_active_subscription ? "active" : "pending"} /></dd>
              </div>
            </dl>

            {!tenant.has_active_subscription && (
              <div className="rounded-panel border-2 border-dashed border-accent/30 bg-accent/5 p-5 text-center">
                <p className="text-sm font-medium text-ink">
                  Your tenant is provisioned but you need an active subscription to use your agent.
                </p>
                <button
                  type="button"
                  onClick={handleCheckout}
                  disabled={checkoutMutation.isPending}
                  className="mt-3 rounded-full bg-accent px-5 py-2.5 text-sm font-medium text-white transition hover:bg-accent/85 disabled:cursor-not-allowed disabled:opacity-55"
                >
                  {checkoutMutation.isPending ? "Redirecting..." : `Subscribe to ${selectedTier === "basic" ? "Basic" : "Plus"}`}
                </button>
              </div>
            )}

            <div className="rounded-panel border border-ink/15 bg-white p-4">
              <p className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Select plan</p>
              <div className="mt-2 flex gap-2">
                {(["basic", "plus"] as const).map((tier) => (
                  <button
                    key={tier}
                    type="button"
                    onClick={() => setSelectedTier(tier)}
                    className={`rounded-full px-4 py-1.5 text-sm transition ${
                      selectedTier === tier
                        ? "bg-ink text-white"
                        : "border border-ink/20 text-ink/75 hover:border-ink/40"
                    }`}
                  >
                    {tier === "basic" ? "Basic (Sonnet)" : "Plus (Opus)"}
                  </button>
                ))}
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-3">
              {tenant.has_active_subscription && (
                <button
                  className="rounded-full bg-ink px-4 py-2 text-sm text-white transition hover:bg-ink/85 disabled:cursor-not-allowed disabled:opacity-55"
                  type="button"
                  onClick={openPortal}
                  disabled={portalMutation.isPending}
                >
                  {portalMutation.isPending ? "Opening..." : "Open Stripe Portal"}
                </button>
              )}
              <button
                className="rounded-full border border-ink/20 px-4 py-2 text-sm text-ink transition hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-55"
                type="button"
                onClick={handleCheckout}
                disabled={checkoutMutation.isPending}
              >
                {checkoutMutation.isPending ? "Redirecting..." : "Start/Change Plan"}
              </button>
            </div>
            <p className="text-xs text-ink/45">
              By subscribing, you agree to our{" "}
              <Link href="/legal/terms" className="underline hover:text-ink/70">Terms</Link>,{" "}
              <Link href="/legal/privacy" className="underline hover:text-ink/70">Privacy Policy</Link>, and{" "}
              <Link href="/legal/refund" className="underline hover:text-ink/70">Refund Policy</Link>.
            </p>

            {portalError && (
              <p className="rounded-panel border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
                {portalError}
              </p>
            )}
            {checkoutError && (
              <p className="rounded-panel border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900">
                {checkoutError}
              </p>
            )}
          </div>
        ) : (
          <div className="flex flex-col items-center py-8 text-center">
            <div className="flex h-16 w-16 items-center justify-center rounded-full bg-ink/5">
              <svg className="h-8 w-8 text-ink/30" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 8.25h19.5M2.25 9h19.5m-16.5 5.25h6m-6 2.25h3m-3.75 3h15a2.25 2.25 0 002.25-2.25V6.75A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25v10.5A2.25 2.25 0 004.5 19.5z" />
              </svg>
            </div>
            <h3 className="mt-4 text-lg font-semibold text-ink">No subscription yet</h3>
            <p className="mt-2 max-w-sm text-sm text-ink/65">
              Complete onboarding to create your tenant and start a subscription.
              You can choose between our Basic (Sonnet) and Plus (Opus) plans.
            </p>
            <div className="mt-6 flex gap-3">
              <Link
                href="/onboarding"
                className="rounded-full bg-ink px-5 py-2.5 text-sm font-medium text-white transition hover:bg-ink/85"
              >
                Start onboarding
              </Link>
              <button
                type="button"
                onClick={handleCheckout}
                disabled={checkoutMutation.isPending}
                className="rounded-full border border-ink/20 px-5 py-2.5 text-sm text-ink transition hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-55"
              >
                {checkoutMutation.isPending ? "Redirecting..." : "Subscribe now"}
              </button>
            </div>
            <p className="mt-3 text-xs text-ink/45">
              By subscribing, you agree to our{" "}
              <Link href="/legal/terms" className="underline hover:text-ink/70">Terms</Link>,{" "}
              <Link href="/legal/privacy" className="underline hover:text-ink/70">Privacy Policy</Link>, and{" "}
              <Link href="/legal/refund" className="underline hover:text-ink/70">Refund Policy</Link>.
            </p>
            {checkoutError && (
              <p className="mt-4 rounded-panel border border-rose-200 bg-rose-50 px-4 py-2.5 text-sm text-rose-900">
                {checkoutError}
              </p>
            )}
          </div>
        )}
      </SectionCard>
    </div>
  );
}
