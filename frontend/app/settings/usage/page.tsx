"use client";

import Link from "next/link";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton, StatCardSkeleton } from "@/components/skeleton";
import { StatCard } from "@/components/stat-card";
import { useTenantQuery, useUsageHistoryQuery, useUsageSummaryQuery } from "@/lib/queries";

export default function SettingsUsagePage() {
  const { data: tenant, isLoading } = useTenantQuery();
  const { data: usageData, isLoading: usageLoading } = useUsageHistoryQuery();
  const { data: usageSummary, isLoading: summaryLoading } = useUsageSummaryQuery();

  const tokenBudget = tenant?.monthly_token_budget ?? 0;
  const tokenUsed = tenant?.tokens_this_month ?? 0;
  const budgetUsage = usageSummary?.budget;
  const effectiveUsed = budgetUsage?.tenant_tokens_used ?? tokenUsed;
  const effectiveBudget = budgetUsage?.tenant_token_budget ?? tokenBudget;
  const budgetPct = effectiveBudget > 0 ? Math.min(100, Math.round((effectiveUsed / effectiveBudget) * 100)) : 0;
  const isOverQuota = effectiveUsed >= effectiveBudget && effectiveBudget > 0;
  const budgetRemaining = Math.max(0, effectiveBudget - effectiveUsed);
  const modelBreakdown = usageSummary?.by_model ?? [];

  if (isLoading || summaryLoading) {
    return (
      <div className="space-y-4">
        <SectionCard title="Usage" subtitle="Monthly token and message burn for your tenant runtime">
          <div className="grid gap-3 md:grid-cols-3">
            <StatCardSkeleton />
            <StatCardSkeleton />
            <StatCardSkeleton />
          </div>
        </SectionCard>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <SectionCard title="Usage" subtitle="Monthly token and message burn for your tenant runtime">
        {tenant ? (
          <div className="space-y-4">
            <div className="grid gap-3 md:grid-cols-3">
              <StatCard label="Messages Today" value={tenant.messages_today.toLocaleString()} />
              <StatCard label="Messages This Month" value={tenant.messages_this_month.toLocaleString()} />
              <StatCard
                label="Estimated Cost"
                value={`$${Number(tenant.estimated_cost_this_month).toFixed(2)}`}
                tone="signal"
              />
            </div>
            {isOverQuota && (
              <div className="rounded-panel border border-signal/30 bg-signal/5 p-4 text-sm text-ink">
                <p className="font-medium">Token quota reached.</p>
                <p className="mt-2 text-ink/75">
                  You cannot go over the token budget. Upgrade your plan or wait until next month.
                </p>
                <Link href="/settings/billing" className="mt-3 inline-flex underline">
                  Go to Billing
                </Link>
              </div>
            )}

            <article className="rounded-panel border border-ink/15 bg-white p-4">
              <div className="flex items-center justify-between gap-2 text-sm">
                <p className="font-medium">Token budget</p>
                <p className="font-mono text-xs tracking-[0.1em] text-ink/65">
                  {effectiveUsed.toLocaleString()} / {effectiveBudget.toLocaleString()}
                </p>
              </div>

              <div className="mt-3 h-3 overflow-hidden rounded-full bg-ink/10">
                <div className="h-full rounded-full bg-gradient-to-r from-accent to-signal" style={{ width: `${budgetPct}%` }} />
              </div>
              <p className="mt-2 text-xs text-ink/65">
                {budgetPct}% of monthly budget consumed.{" "}
                {budgetRemaining > 0
                  ? `${budgetRemaining.toLocaleString()} tokens remaining this month.`
                  : "Nothing remaining this month."}
              </p>
            </article>
          </div>
        ) : (
          <p className="text-sm text-ink/70">No usage data available yet.</p>
        )}
      </SectionCard>

      <SectionCard title="Usage by Model" subtitle="Model-level usage this month">
        {modelBreakdown.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-ink/10">
                  <th className="pb-2 pr-4 text-left font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Model</th>
                  <th className="pb-2 pr-4 text-right font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Inputs</th>
                  <th className="pb-2 pr-4 text-right font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Outputs</th>
                  <th className="pb-2 pr-4 text-right font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Calls</th>
                  <th className="pb-2 text-right font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Cost</th>
                </tr>
              </thead>
              <tbody>
                {modelBreakdown.map((entry) => (
                  <tr key={entry.model} className="border-b border-ink/5">
                    <td className="py-2 pr-4 text-ink/80">
                      {entry.display_name || entry.model}
                    </td>
                    <td className="py-2 pr-4 text-right text-ink/70">{entry.input_tokens.toLocaleString()}</td>
                    <td className="py-2 pr-4 text-right text-ink/70">{entry.output_tokens.toLocaleString()}</td>
                    <td className="py-2 pr-4 text-right text-ink/70">{entry.count}</td>
                    <td className="py-2 text-right text-ink/70">${entry.cost.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-ink/70">No usage by model yet.</p>
        )}
      </SectionCard>

      <SectionCard title="Usage History" subtitle="Recent API usage records">
        {usageLoading ? (
          <SectionCardSkeleton lines={5} />
        ) : usageData?.results && usageData.results.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-ink/10">
                  <th className="pb-2 pr-4 text-left font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Date</th>
                  <th className="pb-2 pr-4 text-left font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Event Type</th>
                  <th className="pb-2 pr-4 text-left font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Model</th>
                  <th className="pb-2 pr-4 text-right font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Tokens</th>
                  <th className="pb-2 text-right font-mono text-[11px] uppercase tracking-[0.14em] text-ink/60">Cost</th>
                </tr>
              </thead>
              <tbody>
                {usageData.results.map((record) => (
                  <tr key={record.id} className="border-b border-ink/5">
                    <td className="py-2 pr-4 text-ink/80">
                      {new Date(record.created_at).toLocaleDateString()}
                    </td>
                    <td className="py-2 pr-4 text-ink/80">{record.event_type}</td>
                    <td className="py-2 pr-4 text-ink/70">{record.model_used || "-"}</td>
                    <td className="py-2 pr-4 text-right font-mono text-ink/80">
                      {(record.input_tokens + record.output_tokens).toLocaleString()}
                    </td>
                    <td className="py-2 text-right font-mono text-ink/80">
                      ${Number(record.cost_estimate).toFixed(4)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-ink/70">No usage records yet.</p>
        )}
      </SectionCard>
    </div>
  );
}
