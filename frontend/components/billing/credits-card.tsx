"use client";

import { useEffect, useState } from "react";

import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { useCreditCheckoutMutation, useCreditsQuery } from "@/lib/queries";
import { useQueryClient } from "@tanstack/react-query";

export function CreditsCard() {
  const { data, isLoading } = useCreditsQuery();
  const checkout = useCreditCheckoutMutation();
  const queryClient = useQueryClient();
  // Read ?topup= client-side (avoids the static-export useSearchParams/Suspense
  // requirement — this is a static export with no SSR). Lazy initializer runs
  // once on mount; this card only mounts client-side (behind the tenant query),
  // so window is always defined here.
  const [topup] = useState<string | null>(() =>
    typeof window === "undefined" ? null : new URLSearchParams(window.location.search).get("topup"),
  );
  const [error, setError] = useState("");

  // The success redirect can beat the webhook that actually grants the credit,
  // so never trust the redirect — just re-fetch the server balance a couple of
  // times and show a "processing" note until it lands.
  useEffect(() => {
    if (topup !== "success") return;
    const refetch = () => void queryClient.invalidateQueries({ queryKey: ["credits"] });
    refetch();
    const t1 = setTimeout(refetch, 2500);
    const t2 = setTimeout(refetch, 6000);
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
    };
  }, [topup, queryClient]);

  const buy = async (packId: string) => {
    setError("");
    try {
      const { url } = await checkout.mutateAsync(packId);
      window.location.assign(url);
    } catch (err) {
      const raw = err instanceof Error ? err.message : "";
      try {
        setError(JSON.parse(raw).detail || "Couldn't start checkout. Please try again.");
      } catch {
        setError("Couldn't start checkout. Please try again.");
      }
    }
  };

  if (isLoading) {
    return <SectionCardSkeleton lines={4} />;
  }
  if (!data) return null;

  return (
    <SectionCard
      title="Prepaid credit"
      subtitle="Top up to keep your assistant going past your monthly included usage"
    >
      <div className="space-y-4">
        {topup === "success" && (
          <div className="rounded-panel border border-accent/30 bg-accent/5 px-4 py-3 text-sm text-ink-muted">
            Payment received — your credit balance updates within a few seconds.
          </div>
        )}
        {topup === "cancelled" && (
          <div className="rounded-panel border border-border bg-surface-elevated px-4 py-3 text-sm text-ink-muted">
            Checkout cancelled — no charge was made.
          </div>
        )}

        <dl className="grid gap-3 text-sm sm:grid-cols-2">
          <div className="rounded-panel border border-border bg-surface-elevated p-4">
            <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Prepaid balance</dt>
            <dd className="mt-1 text-lg font-semibold text-ink">${data.purchased_credit}</dd>
            <p className="mt-1 text-xs text-ink-muted">Rolls over month to month. Never expires.</p>
          </div>
          <div className="rounded-panel border border-border bg-surface-elevated p-4">
            <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Included this month</dt>
            <dd className="mt-1 text-lg font-semibold text-ink">
              ${data.included_used}
              <span className="text-sm font-normal text-ink-muted"> / ${data.included_budget}</span>
            </dd>
            <p className="mt-1 text-xs text-ink-muted">
              {data.included_remaining === null
                ? "Unlimited included usage."
                : `$${data.included_remaining} included usage left, then prepaid credit is used.`}
            </p>
          </div>
        </dl>

        <div>
          <p className="mb-2 text-xs font-medium text-ink-muted">Add credit</p>
          <div className="grid gap-3 sm:grid-cols-3">
            {data.packs.map((pack) => (
              <button
                key={pack.id}
                type="button"
                onClick={() => void buy(pack.id)}
                disabled={checkout.isPending}
                className="min-h-[44px] rounded-panel border-2 border-border p-4 text-left transition hover:border-accent/50 disabled:cursor-not-allowed disabled:opacity-55"
              >
                <p className="text-base font-semibold text-ink">{pack.credit_display} credit</p>
                <p className="mt-0.5 text-xs text-ink-muted">{pack.price_display}</p>
              </button>
            ))}
          </div>
          <p className="mt-2 text-xs text-ink-faint">
            Credit is applied automatically once your included monthly usage is spent.
          </p>
        </div>

        {error && (
          <p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">{error}</p>
        )}

        {data.recent_entries.length > 0 && (
          <div>
            <p className="mb-2 text-xs font-medium text-ink-muted">Recent activity</p>
            <ul className="divide-y divide-border rounded-panel border border-border">
              {data.recent_entries.slice(0, 6).map((entry, i) => (
                <li key={i} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
                  <span className="truncate text-ink-muted">{entry.description || entry.kind}</span>
                  <span
                    className={`shrink-0 font-mono ${
                      entry.amount.startsWith("-") ? "text-ink-faint" : "text-accent"
                    }`}
                  >
                    {entry.amount.startsWith("-") ? "" : "+"}${entry.amount.replace("-", "")}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </SectionCard>
  );
}
