"use client";

import Link from "next/link";
import { useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import { useCheckoutMutation, useStripePortalMutation, useTenantQuery } from "@/lib/queries";

const PLAN_OPTIONS = [
  { id: "starter", name: "Starter", price: "Free", description: "7-day free trial", available: false },
  { id: "premium", name: "Premium", price: "$25/mo", description: "Claude Sonnet & Opus (coming in March)", available: false },
  { id: "byok", name: "BYOK", price: "$8/mo", description: "Bring Your Own Key (coming in March)", available: false },
] as const;

const HAS_BILLABLE_PLANS = false;

const TIERS: Record<"starter" | "premium" | "byok", { label: string }> = {
  starter: { label: "Starter" },
  premium: { label: "Premium" },
  byok: { label: "BYOK" },
};

export default function SettingsBillingPage() {
  const { data: tenant, isLoading, error } = useTenantQuery();
  const portalMutation = useStripePortalMutation();
  const checkoutMutation = useCheckoutMutation();
  const [portalError, setPortalError] = useState("");
  const [checkoutError, setCheckoutError] = useState("");
  const [selectedTier, setSelectedTier] = useState<"starter" | "premium" | "byok">("starter");

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

  const trialDays = tenant?.trial_days_remaining ?? null;
  const isTrialActive = Boolean(tenant?.is_trial && trialDays !== null && trialDays > 0);
  const isTrialExpired = Boolean(tenant?.is_trial && tenant?.trial_days_remaining === 0);
  const isTelegramLinked = Boolean(tenant?.user.telegram_chat_id);
  const onboardingCta = isTelegramLinked
    ? { href: "/settings/integrations", label: "Review Telegram setup" }
    : { href: "/onboarding", label: "Connect Telegram" };

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
                  ? `🎉 You're on a free trial! ${trialDays} days remaining. Subscribe to keep your assistant.`
                  : "Your free trial has ended. Subscribe to reactivate your assistant."}
              </div>
            )}

            <dl className="grid gap-3 text-sm sm:grid-cols-2">
              <div className="rounded-panel border border-border bg-surface-elevated p-4 min-w-0 overflow-visible">
                <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Tier</dt>
                <dd className="mt-1 text-lg font-semibold uppercase">{tenant.model_tier}</dd>
              </div>
              <div className="rounded-panel border border-border bg-surface-elevated p-4 min-w-0 overflow-visible">
                <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Tenant State</dt>
                <dd className="mt-1"><StatusPill status={tenant.status} /></dd>
              </div>
              <div className="rounded-panel border border-border bg-surface-elevated p-4 sm:col-span-2">
                <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Subscription</dt>
                <dd className="mt-1"><StatusPill status={tenant.has_active_subscription ? "active" : "pending"} /></dd>
              </div>
            </dl>

            {!tenant.has_active_subscription && HAS_BILLABLE_PLANS && (
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
                  {checkoutMutation.isPending ? "Redirecting..." : `Subscribe to ${TIERS[selectedTier].label}`}
                </button>
              </div>
            )}

            {!HAS_BILLABLE_PLANS && (
              <div className="rounded-panel border border-dashed border-accent/30 bg-accent/5 p-5 text-sm text-ink">
                Billing is paused while we finalize launch prep. You&apos;re in the free week.
              </div>
            )}

            {HAS_BILLABLE_PLANS && (
              <div className="rounded-panel border border-border bg-surface-elevated p-4 min-w-0 overflow-visible">
                <p className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Select plan</p>
                <div className="mt-2 flex gap-2">
                  {(PLAN_OPTIONS as readonly { id: "starter" | "premium" | "byok"; name: string; price: string; description: string; available: boolean }[]).map((tier) => (
                    <button
                      key={tier.id}
                      type="button"
                      onClick={() => tier.available && setSelectedTier(tier.id)}
                      disabled={!tier.available}
                      className={`rounded-full px-4 py-1.5 text-sm transition ${
                        !tier.available
                          ? "border border-border bg-surface-elevated text-ink-faint cursor-not-allowed opacity-50"
                          : selectedTier === tier.id
                            ? "bg-accent text-white"
                            : "border border-border-strong bg-surface-elevated text-ink hover:border-accent/40"
                      }`}
                    >
                      {tier.name} ({tier.price}) — {tier.description}
                    </button>
                  ))}
                </div>
              </div>
            )}

            <div className="flex flex-wrap items-center gap-3">
              {tenant.has_active_subscription && HAS_BILLABLE_PLANS && (
                <button
                  className="rounded-full bg-accent px-4 py-2 text-sm text-white transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-55"
                  type="button"
                  onClick={openPortal}
                  disabled={portalMutation.isPending}
                >
                  {portalMutation.isPending ? "Opening..." : "Open Stripe Portal"}
                </button>
              )}
              {HAS_BILLABLE_PLANS && (
                <button
                  className="rounded-full border border-border-strong px-4 py-2 text-sm text-ink transition hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-55"
                  type="button"
                  onClick={handleCheckout}
                  disabled={checkoutMutation.isPending}
                >
                  {checkoutMutation.isPending ? "Redirecting..." : "Start/Change Plan"}
                </button>
              )}
            </div>
            <p className="text-xs text-ink-faint">
              {HAS_BILLABLE_PLANS ? (
                <>By subscribing, you agree to our{" "}
                <Link href="/legal/terms" className="underline hover:text-ink-muted">Terms</Link>,{" "}
                <Link href="/legal/privacy" className="underline hover:text-ink-muted">Privacy Policy</Link>, and{" "}
                <Link href="/legal/refund" className="underline hover:text-ink-muted">Refund Policy</Link>.
                </>
              ) : (
                <>Enjoy your free trial and check back in March for paid plans and invoice upgrades.</>
              )}
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
              You&apos;ll get a 7-day free setup trial — paid plans return in March.
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
              {HAS_BILLABLE_PLANS ? (
                <>By subscribing, you agree to our{" "}
                <Link href="/legal/terms" className="underline hover:text-ink-muted">Terms</Link>,{" "}
                <Link href="/legal/privacy" className="underline hover:text-ink-muted">Privacy Policy</Link>, and{" "}
                <Link href="/legal/refund" className="underline hover:text-ink-muted">Refund Policy</Link>.
                </>
              ) : (
                <>Enjoy your free trial and check back in March for paid plans and invoice upgrades.</>
              )}
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
