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
              <div className="rounded-panel border border-signal/30 bg-signal-faint p-4 text-sm text-ink">
                <p className="font-medium">Token quota reached.</p>
                <p className="mt-2 text-ink-muted">
                  You cannot go over the token budget. Upgrade your plan or wait until next month.
                </p>
                <Link href="/settings/billing" className="mt-3 inline-flex underline">
                  Go to Billing
                </Link>
              </div>
            )}

            <article className="rounded-panel border border-border bg-surface-elevated p-4">
              <div className="flex items-center justify-between gap-2 text-sm">
                <p className="font-medium">Token budget</p>
                <p className="font-mono text-xs tracking-[0.1em] text-ink-muted">
                  {effectiveUsed.toLocaleString()} / {effectiveBudget.toLocaleString()}
                </p>
              </div>

              <div className="mt-3 h-3 overflow-hidden rounded-full bg-border">
                <div className="h-full rounded-full bg-gradient-to-r from-accent to-signal" style={{ width: `${budgetPct}%` }} />
              </div>
              <p className="mt-2 text-xs text-ink-muted">
                {budgetPct}% of monthly budget consumed.{" "}
                {budgetRemaining > 0
                  ? `${budgetRemaining.toLocaleString()} tokens remaining this month.`
                  : "Nothing remaining this month."}
              </p>
            </article>
          </div>
        ) : (
          <p className="text-sm text-ink-muted">No usage data available yet.</p>
        )}
      </SectionCard>

      <SectionCard title="Usage by Model" subtitle="Model-level usage this month">
        {modelBreakdown.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border">
                  <th className="pb-2 pr-4 text-left font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Model</th>
                  <th className="pb-2 pr-4 text-right font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Inputs</th>
                  <th className="pb-2 pr-4 text-right font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Outputs</th>
                  <th className="pb-2 pr-4 text-right font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Calls</th>
                  <th className="pb-2 text-right font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Cost</th>
                </tr>
              </thead>
              <tbody>
                {modelBreakdown.map((entry) => (
                  <tr key={entry.model} className="border-b border-border">
                    <td className="py-2 pr-4 text-ink-muted">
                      {entry.display_name || entry.model}
                    </td>
                    <td className="py-2 pr-4 text-right text-ink-muted">{entry.input_tokens.toLocaleString()}</td>
                    <td className="py-2 pr-4 text-right text-ink-muted">{entry.output_tokens.toLocaleString()}</td>
                    <td className="py-2 pr-4 text-right text-ink-muted">{entry.count}</td>
                    <td className="py-2 text-right text-ink-muted">${entry.cost.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-ink-muted">No usage by model yet.</p>
        )}
      </SectionCard>

      <SectionCard title="Usage History" subtitle="Recent API usage records">
        {usageLoading ? (
          <SectionCardSkeleton lines={5} />
        ) : usageData?.results && usageData.results.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border">
                  <th className="pb-2 pr-4 text-left font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Date</th>
                  <th className="pb-2 pr-4 text-left font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Event Type</th>
                  <th className="pb-2 pr-4 text-left font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Model</th>
                  <th className="pb-2 pr-4 text-right font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Tokens</th>
                  <th className="pb-2 text-right font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">Cost</th>
                </tr>
              </thead>
              <tbody>
                {usageData.results.map((record) => (
                  <tr key={record.id} className="border-b border-border">
                    <td className="py-2 pr-4 text-ink-muted">
                      {new Date(record.created_at).toLocaleDateString()}
                    </td>
                    <td className="py-2 pr-4 text-ink-muted">{record.event_type}</td>
                    <td className="py-2 pr-4 text-ink-muted">{record.model_used || "-"}</td>
                    <td className="py-2 pr-4 text-right font-mono text-ink-muted">
                      {(record.input_tokens + record.output_tokens).toLocaleString()}
                    </td>
                    <td className="py-2 text-right font-mono text-ink-muted">
                      ${Number(record.cost_estimate).toFixed(4)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-ink-muted">No usage records yet.</p>
        )}
      </SectionCard>
    </div>
  );
}
