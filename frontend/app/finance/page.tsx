"use client";

import { AccountCard } from "@/components/account-card";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton, StatCardSkeleton } from "@/components/skeleton";
import { StatCard } from "@/components/stat-card";
import { PayoffChart } from "@/components/finance/payoff-chart";
import { ProgressChart } from "@/components/finance/progress-chart";
import { StrategyComparison } from "@/components/finance/strategy-comparison";
import { useFinanceDashboardQuery } from "@/lib/queries";

function formatCurrency(value: string | number): string {
  const num = typeof value === "string" ? parseFloat(value) : value;
  return num.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  });
}

function formatPayoffDate(dateStr: string): string {
  const d = new Date(dateStr + "T00:00:00");
  return d.toLocaleDateString("en-US", { month: "short", year: "numeric" });
}

export default function FinancePage() {
  const { data, isLoading, error } = useFinanceDashboardQuery();

  if (isLoading) {
    return (
      <div className="space-y-4 sm:space-y-6">
        <div>
          <h1 className="font-headline text-3xl font-bold tracking-tight text-ink sm:text-4xl">
            Fuel
          </h1>
          <p className="mt-1 text-sm text-ink-muted">
            What powers the journey.
          </p>
        </div>
        <div className="grid gap-3 md:grid-cols-3">
          <StatCardSkeleton />
          <StatCardSkeleton />
          <StatCardSkeleton />
        </div>
        <SectionCardSkeleton lines={4} />
        <SectionCardSkeleton lines={3} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-4 sm:space-y-6">
        <div>
          <h1 className="font-headline text-3xl font-bold tracking-tight text-ink sm:text-4xl">
            Fuel
          </h1>
        </div>
        <div className="rounded-panel border border-rose-border bg-rose-bg p-4 text-sm text-rose-text">
          Failed to load Fuel.{" "}
          {error instanceof Error ? error.message : "Please try again."}
        </div>
      </div>
    );
  }

  if (!data) return null;

  const hasAccounts = data.accounts.length > 0;
  const debtAccounts = data.accounts.filter((a) => a.is_debt);
  const savingsAccounts = data.accounts.filter((a) => !a.is_debt);

  // Empty state
  if (!hasAccounts) {
    return (
      <div className="space-y-4 sm:space-y-6">
        <div>
          <h1 className="font-headline text-3xl font-bold tracking-tight text-ink sm:text-4xl">
            Fuel
          </h1>
          <p className="mt-1 text-sm text-ink-muted">
            What powers the journey.
          </p>
        </div>
        <div className="rounded-panel border border-border bg-card/95 p-8 text-center shadow-panel animate-reveal">
          <p className="font-headline text-xl font-bold text-ink">No accounts yet</p>
          <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-muted">
            Tell your assistant about your debts and savings. Say something like
            &ldquo;I have a credit card with $5,000 at 22% APR&rdquo; and it
            will start tracking for you.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4 sm:space-y-6">
      {/* Header */}
      <div>
        <h1 className="font-headline text-3xl font-bold tracking-tight text-ink sm:text-4xl">Fuel</h1>
        <p className="mt-1 text-sm text-ink-muted">
          What powers the journey.
        </p>
      </div>

      {/* Summary Stats */}
      <div
        className="grid gap-3 md:grid-cols-3 animate-reveal"
        style={{ animationDelay: "100ms" }}
      >
        <StatCard
          label="Total Debt"
          value={formatCurrency(data.total_debt)}
          tone="accent"
          hint={`${data.debt_account_count} account${data.debt_account_count !== 1 ? "s" : ""}`}
        />
        <StatCard
          label="Savings"
          value={formatCurrency(data.total_savings)}
          tone="signal"
          hint={`${data.savings_account_count} account${data.savings_account_count !== 1 ? "s" : ""}`}
        />
        <StatCard
          label="Est. Payoff"
          value={
            data.active_plan
              ? formatPayoffDate(data.active_plan.payoff_date)
              : "—"
          }
          tone="accent"
          hint={
            data.active_plan
              ? `${data.active_plan.strategy.charAt(0).toUpperCase() + data.active_plan.strategy.slice(1)} strategy`
              : "Ask your assistant to calculate"
          }
        />
      </div>

      {/* Payoff Timeline Chart */}
      {data.active_plan ? (
        <SectionCard
          title="Payoff Timeline"
          subtitle={`${data.active_plan.payoff_months} months to debt-free`}
          delay={200}
        >
          <PayoffChart plan={data.active_plan} />
        </SectionCard>
      ) : null}

      {/* Account Cards */}
      {debtAccounts.length > 0 ? (
        <div
          className="animate-reveal"
          style={{ animationDelay: "350ms" }}
        >
          <h2 className="font-headline text-xl font-bold text-ink">Debt Accounts</h2>
          <div className="mt-3 grid grid-cols-1 gap-3 sm:gap-4 md:grid-cols-2">
            {debtAccounts.map((account) => (
              <AccountCard key={account.id} account={account} />
            ))}
          </div>
        </div>
      ) : null}

      {savingsAccounts.length > 0 ? (
        <div
          className="animate-reveal"
          style={{ animationDelay: "400ms" }}
        >
          <h2 className="font-headline text-xl font-bold text-ink">Savings</h2>
          <div className="mt-3 grid grid-cols-1 gap-3 sm:gap-4 md:grid-cols-2">
            {savingsAccounts.map((account) => (
              <AccountCard key={account.id} account={account} />
            ))}
          </div>
        </div>
      ) : null}

      {/* Strategy Comparison */}
      {data.active_plan ? (
        <SectionCard
          title="Strategy"
          subtitle="Your active debt payoff plan"
          delay={500}
        >
          <StrategyComparison activePlan={data.active_plan} />
        </SectionCard>
      ) : null}

      {/* Progress Over Time */}
      {data.snapshots.length >= 2 ? (
        <SectionCard
          title="Progress"
          subtitle="Monthly debt and savings trend"
          delay={600}
        >
          <ProgressChart snapshots={data.snapshots} />
        </SectionCard>
      ) : null}

      {/* Minimums Summary */}
      {parseFloat(data.total_minimum_payments) > 0 ? (
        <div
          className="animate-reveal rounded-panel border border-border bg-card/95 p-4"
          style={{ animationDelay: "650ms" }}
        >
          <div className="flex items-center justify-between text-sm">
            <span className="text-ink-muted">Total monthly minimums</span>
            <span className="font-mono font-medium text-ink">
              {formatCurrency(data.total_minimum_payments)}
            </span>
          </div>
        </div>
      ) : null}
    </div>
  );
}
