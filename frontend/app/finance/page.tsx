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

function PageHeader() {
  return (
    <div className="mb-8 sm:mb-12">
      <span className="text-accent text-[10px] font-bold uppercase tracking-[0.2em] mb-1 block">
        Personal Finance
      </span>
      <h1 className="font-headline text-4xl font-bold tracking-tight text-ink sm:text-5xl">
        Gravity
      </h1>
      <p className="mt-1 text-ink-muted italic">
        What keeps everything grounded.
      </p>
    </div>
  );
}

export default function FinancePage() {
  const { data, isLoading, error } = useFinanceDashboardQuery();

  if (isLoading) {
    return (
      <div>
        <PageHeader />
        <div className="grid gap-4 md:grid-cols-3 mb-8">
          <StatCardSkeleton />
          <StatCardSkeleton />
          <StatCardSkeleton />
        </div>
        <SectionCardSkeleton lines={4} />
      </div>
    );
  }

  if (error) {
    return (
      <div>
        <PageHeader />
        <div className="rounded-xl border border-rose-border bg-rose-bg p-4 text-sm text-rose-text">
          Failed to load Gravity.{" "}
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
      <div>
        <PageHeader />
        <div className="glass-card rounded-xl p-10 text-center animate-reveal">
          <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-accent/10">
            <svg viewBox="0 0 24 24" fill="none" className="h-8 w-8 text-accent" stroke="currentColor" strokeWidth="1.5">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v12m-3-2.818l.879.659c1.171.879 3.07.879 4.242 0 1.172-.879 1.172-2.303 0-3.182C13.536 12.219 12.768 12 12 12c-.725 0-1.45-.22-2.003-.659-1.106-.879-1.106-2.303 0-3.182s2.9-.879 4.006 0l.415.33M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          </div>
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
    <div>
      {/* Header */}
      <PageHeader />

      {/* KPI Stats */}
      <div
        className="grid gap-4 sm:gap-6 md:grid-cols-3 mb-10 sm:mb-16 animate-reveal"
        style={{ animationDelay: "100ms" }}
      >
        <StatCard
          label="Total Debt"
          value={formatCurrency(data.total_debt)}
          tone="error"
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
              : "\u2014"
          }
          tone="accent"
          hint={
            data.active_plan
              ? `${data.active_plan.strategy.charAt(0).toUpperCase() + data.active_plan.strategy.slice(1)} strategy`
              : "Ask your assistant to calculate"
          }
        />
      </div>

      {/* Payoff Timeline + Strategy — side by side on large screens */}
      {data.active_plan && (
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-6 sm:gap-8 mb-10 sm:mb-16">
          <div className="lg:col-span-8">
            <SectionCard
              title="Payoff Timeline"
              subtitle={`${data.active_plan.payoff_months} months to debt-free`}
              delay={200}
            >
              <PayoffChart plan={data.active_plan} />
            </SectionCard>
          </div>
          <div className="lg:col-span-4">
            <SectionCard
              title="Strategy"
              subtitle="Your active payoff plan"
              delay={300}
            >
              <StrategyComparison activePlan={data.active_plan} />
            </SectionCard>
          </div>
        </div>
      )}

      {/* Debt Accounts */}
      {debtAccounts.length > 0 && (
        <div className="mb-10 sm:mb-16 animate-reveal" style={{ animationDelay: "350ms" }}>
          <div className="glass-card rounded-xl border border-white/5 p-6 sm:p-8">
            <h2 className="font-headline text-2xl font-bold text-ink mb-6">Debt Accounts</h2>
            <div className="divide-y divide-white/5 space-y-6">
              {debtAccounts.map((account) => (
                <div key={account.id} className="pt-6 first:pt-0">
                  <AccountCard account={account} />
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Savings */}
      {savingsAccounts.length > 0 && (
        <div className="mb-10 sm:mb-16 animate-reveal" style={{ animationDelay: "400ms" }}>
          <div className="glass-card rounded-xl border border-white/5 p-6 sm:p-8">
            <h2 className="font-headline text-2xl font-bold text-ink mb-6">Savings</h2>
            <div className="divide-y divide-white/5 space-y-6">
              {savingsAccounts.map((account) => (
                <div key={account.id} className="pt-6 first:pt-0">
                  <AccountCard account={account} />
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Progress Over Time */}
      {data.snapshots.length >= 2 && (
        <div className="mb-10 sm:mb-16">
          <SectionCard
            title="Progress"
            subtitle="Monthly debt and savings trend"
            delay={500}
          >
            <ProgressChart snapshots={data.snapshots} />
          </SectionCard>
        </div>
      )}

      {/* Minimums Summary */}
      {parseFloat(data.total_minimum_payments) > 0 && (
        <div
          className="animate-reveal glass-card rounded-xl border border-white/5 p-4"
          style={{ animationDelay: "600ms" }}
        >
          <div className="flex items-center justify-between text-sm">
            <span className="text-ink-muted">Total monthly minimums</span>
            <span className="font-mono font-medium text-ink">
              {formatCurrency(data.total_minimum_payments)}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
