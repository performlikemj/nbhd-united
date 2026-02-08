"use client";

import { useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import { useCheckoutMutation, useStripePortalMutation, useTenantQuery } from "@/lib/queries";

export default function BillingPage() {
  const { data: tenant, isLoading } = useTenantQuery();
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

  return (
    <div className="space-y-4">
      <SectionCard
        title="Billing"
        subtitle="Stripe subscription and portal controls"
      >
        {tenant ? (
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
              <button
                className="rounded-full bg-ink px-4 py-2 text-sm text-white transition hover:bg-ink/85 disabled:cursor-not-allowed disabled:opacity-55"
                type="button"
                onClick={openPortal}
                disabled={portalMutation.isPending}
              >
                {portalMutation.isPending ? "Opening..." : "Open Stripe Portal"}
              </button>
              <button
                className="rounded-full border border-ink/20 px-4 py-2 text-sm text-ink transition hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-55"
                type="button"
                onClick={handleCheckout}
                disabled={checkoutMutation.isPending}
              >
                {checkoutMutation.isPending ? "Redirecting..." : "Start/Change Plan"}
              </button>
            </div>

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
        ) : null}
      </SectionCard>
    </div>
  );
}
