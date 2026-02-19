"use client";

import Link from "next/link";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton, StatCardSkeleton } from "@/components/skeleton";
import { StatCard } from "@/components/stat-card";
import { useTenantQuery, useTransparencyQuery, useUsageHistoryQuery, useUsageSummaryQuery } from "@/lib/queries";

export default function SettingsUsagePage() {
  const { data: tenant, isLoading } = useTenantQuery();
  const { data: usageData, isLoading: usageLoading } = useUsageHistoryQuery();
  const { data: usageSummary, isLoading: summaryLoading } = useUsageSummaryQuery();
  const { data: transparency, isLoading: transparencyLoading } = useTransparencyQuery();

  const tokenBudget = tenant?.monthly_token_budget ?? 0;
  const tokenUsed = tenant?.tokens_this_month ?? 0;
  const budgetUsage = usageSummary?.budget;
  const effectiveUsed = budgetUsage?.tenant_tokens_used ?? tokenUsed;
  const effectiveBudget = budgetUsage?.tenant_token_budget ?? tokenBudget;
  const budgetPct = effectiveBudget > 0 ? Math.min(100, Math.round((effectiveUsed / effectiveBudget) * 100)) : 0;
  const isOverQuota = effectiveUsed >= effectiveBudget && effectiveBudget > 0;
  const budgetRemaining = Math.max(0, effectiveBudget - effectiveUsed);
  const modelBreakdown = usageSummary?.by_model ?? [];

  const subscriptionPrice = transparency?.subscription_price ?? 0;
  const aiActualCost = transparency?.your_actual_cost ?? 0;
  const platformCost = transparency?.platform_margin ?? 0;
  const splitTotal = Math.max(subscriptionPrice, aiActualCost + platformCost, 0.01);
  const aiPercent = Math.min(100, (aiActualCost / splitTotal) * 100);
  const platformPercent = Math.min(100 - aiPercent, (platformCost / splitTotal) * 100);

  if (isLoading || summaryLoading || transparencyLoading) {
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
                <div
                  className="h-full rounded-full bg-gradient-to-r from-accent to-signal"
                  style={{ width: `${budgetPct}%` }}
                />
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

      <SectionCard title="Where Your Money Goes" subtitle="A clear split of your monthly subscription">
        <div className="space-y-4">
          <div className="grid gap-3 md:grid-cols-3">
            <StatCard label="AI Model Usage" value={`$${aiActualCost.toFixed(2)}`} tone="accent" />
            <StatCard
              label="Platform & Infrastructure"
              value={`$${platformCost.toFixed(2)}`}
              hint={transparency ? `For ${transparency.message_count} messages this period` : undefined}
            />
            <StatCard label="Your Subscription" value={`$${subscriptionPrice.toFixed(2)}`} tone="signal" />
          </div>

          <article className="rounded-panel border border-border bg-surface-elevated p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="font-medium">Monthly cost split</p>
              <p className="text-xs font-mono text-ink-muted">
                {transparency?.period.start} → {transparency?.period.end}
              </p>
            </div>

            <div className="mt-3 h-3 overflow-hidden rounded-full border border-border bg-border">
              <div className="relative h-full w-full">
                <div className="absolute left-0 top-0 h-full rounded-l-full bg-accent" style={{ width: `${aiPercent}%` }} />
                <div
                  className="absolute top-0 h-full bg-muted-foreground/30"
                  style={{ left: `${aiPercent}%`, width: `${platformPercent}%` }}
                />
              </div>
            </div>

            <div className="mt-3 grid gap-2 sm:grid-cols-2 text-xs text-ink-muted">
              <div className="flex items-center justify-between rounded-panel border border-accent/25 bg-accent/8 p-2">
                <div className="flex items-center gap-2">
                  <span className="h-2 w-2 rounded-full bg-accent" />
                  <span>AI Model Usage</span>
                </div>
                <span className="font-mono">${aiActualCost.toFixed(2)}</span>
              </div>
              <div className="flex items-center justify-between rounded-panel border border-muted/35 bg-muted/10 p-2">
                <div className="flex items-center gap-2">
                  <span className="h-2 w-2 rounded-full bg-muted" />
                  <span>Platform & Infrastructure</span>
                </div>
                <span className="font-mono">${platformCost.toFixed(2)}</span>
              </div>
            </div>

            {transparency ? (
              <>
                <p className="mt-3 text-sm leading-relaxed text-ink-muted">{transparency.explanation}</p>

                <p className="mt-2 text-xs text-ink-muted">
                  Infrastructure estimate:
                  container ${transparency.infra_breakdown.container.toFixed(2)} •
                  database ${transparency.infra_breakdown.database_share.toFixed(2)} •
                  storage ${transparency.infra_breakdown.storage_share.toFixed(2)}
                  {" • total "}
                  ${transparency.infra_breakdown.total.toFixed(2)}
                </p>

                <details className="mt-4 rounded-panel border border-border bg-surface p-3">
                  <summary className="cursor-pointer select-none text-sm font-medium text-ink">Model pricing</summary>
                  <div className="mt-3 overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="border-b border-border text-left text-ink-muted">
                          <th className="pb-2 pr-4 font-mono uppercase tracking-[0.14em]">Model</th>
                          <th className="pb-2 pr-4 font-mono uppercase tracking-[0.14em]">Input / 1M</th>
                          <th className="pb-2 font-mono uppercase tracking-[0.14em]">Output / 1M</th>
                        </tr>
                      </thead>
                      <tbody>
                        {transparency.model_rates.map((rate) => (
                          <tr key={rate.model} className="border-b border-border/60">
                            <td className="py-2 pr-4 text-ink">{rate.display_name}</td>
                            <td className="py-2 pr-4 font-mono text-ink-muted">${rate.input_per_million.toFixed(2)}</td>
                            <td className="py-2 font-mono text-ink-muted">${rate.output_per_million.toFixed(2)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
              </>
            ) : (
              <p className="mt-3 text-sm text-ink-muted">Transparency data unavailable.</p>
            )}
          </article>
        </div>
      </SectionCard>
    </div>
  );
}
