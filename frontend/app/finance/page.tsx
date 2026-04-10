"use client";

import { useEffect, useState } from "react";

import { AccountCard } from "@/components/account-card";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton, StatCardSkeleton } from "@/components/skeleton";
import { StatCard } from "@/components/stat-card";
import { PayoffChart } from "@/components/finance/payoff-chart";
import { ProgressChart } from "@/components/finance/progress-chart";
import { StrategyComparison } from "@/components/finance/strategy-comparison";
import {
  useArchiveFinanceAccountMutation,
  useArchivedFinanceAccountsQuery,
  useFinanceDashboardQuery,
  useUnarchiveFinanceAccountMutation,
} from "@/lib/queries";
import type { FinanceAccount } from "@/lib/types";

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
  const [confirmArchive, setConfirmArchive] = useState<FinanceAccount | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const archiveMutation = useArchiveFinanceAccountMutation();
  const unarchiveMutation = useUnarchiveFinanceAccountMutation();
  const archivedQuery = useArchivedFinanceAccountsQuery(showArchived);

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
              <PayoffChart plan={data.active_plan} snapshots={data.snapshots} />
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
                  <AccountCard account={account} onArchive={setConfirmArchive} />
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
                  <AccountCard account={account} onArchive={setConfirmArchive} />
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

      {/* Archived Accounts */}
      <ArchivedAccountsSection
        expanded={showArchived}
        onToggle={() => setShowArchived((v) => !v)}
        isLoading={archivedQuery.isLoading}
        accounts={archivedQuery.data ?? []}
        onRestore={(account) => unarchiveMutation.mutate(account.id)}
        restoringId={
          unarchiveMutation.isPending
            ? (unarchiveMutation.variables as string | undefined)
            : undefined
        }
      />

      {/* Confirm Archive Dialog */}
      {confirmArchive && (
        <ConfirmArchiveDialog
          account={confirmArchive}
          onCancel={() => setConfirmArchive(null)}
          onConfirm={() => {
            const target = confirmArchive;
            archiveMutation.mutate(target.id, {
              onSuccess: () => setConfirmArchive(null),
            });
          }}
          isPending={archiveMutation.isPending}
          errorMessage={
            archiveMutation.isError
              ? archiveMutation.error instanceof Error
                ? archiveMutation.error.message
                : "Failed to archive. Please try again."
              : null
          }
        />
      )}
    </div>
  );
}

function ArchivedAccountsSection({
  expanded,
  onToggle,
  isLoading,
  accounts,
  onRestore,
  restoringId,
}: {
  expanded: boolean;
  onToggle: () => void;
  isLoading: boolean;
  accounts: FinanceAccount[];
  onRestore: (account: FinanceAccount) => void;
  restoringId: string | undefined;
}) {
  // Hide the whole section once we've loaded and there's nothing archived.
  if (expanded && !isLoading && accounts.length === 0) {
    return (
      <div className="mt-10 sm:mt-16">
        <button
          type="button"
          onClick={onToggle}
          className="flex w-full items-center justify-between text-left text-xs font-bold uppercase tracking-[0.2em] text-ink-faint transition hover:text-ink"
        >
          <span>Archived Accounts</span>
          <span aria-hidden="true">▴</span>
        </button>
        <p className="mt-3 text-sm text-ink-muted">No archived accounts.</p>
      </div>
    );
  }

  return (
    <div className="mt-10 sm:mt-16">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="flex w-full items-center justify-between text-left text-xs font-bold uppercase tracking-[0.2em] text-ink-faint transition hover:text-ink"
      >
        <span>Archived Accounts</span>
        <span aria-hidden="true">{expanded ? "▴" : "▾"}</span>
      </button>
      {expanded && (
        <div className="mt-4 glass-card rounded-xl border border-white/5 p-4 sm:p-6">
          {isLoading ? (
            <p className="text-sm text-ink-muted">Loading…</p>
          ) : (
            <ul className="divide-y divide-white/5">
              {accounts.map((account) => (
                <li
                  key={account.id}
                  className="flex flex-col gap-3 py-3 sm:flex-row sm:items-center sm:justify-between"
                >
                  <div>
                    <p className="font-bold text-ink">{account.nickname}</p>
                    <p className="text-xs text-ink-faint">
                      Last balance {formatCurrency(account.current_balance)}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => onRestore(account)}
                    disabled={restoringId === account.id}
                    className="inline-flex h-11 items-center justify-center rounded-lg border border-white/10 px-4 text-xs font-bold uppercase tracking-wide text-ink transition hover:border-accent/60 hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
                  >
                    {restoringId === account.id ? "Restoring…" : "Restore"}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function ConfirmArchiveDialog({
  account,
  onCancel,
  onConfirm,
  isPending,
  errorMessage,
}: {
  account: FinanceAccount;
  onCancel: () => void;
  onConfirm: () => void;
  isPending: boolean;
  errorMessage: string | null;
}) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onCancel]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onCancel();
      }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="archive-account-title"
    >
      <div className="w-full max-w-md rounded-panel border border-border bg-surface-elevated p-6 shadow-panel">
        <h2
          id="archive-account-title"
          className="font-headline text-xl font-bold text-ink"
        >
          Archive {account.nickname}?
        </h2>
        <p className="mt-2 text-sm text-ink-muted">
          It will be removed from your Gravity totals and payoff calculations but
          kept for your records. You can restore it later from the Archived
          section below.
        </p>
        {errorMessage && (
          <p className="mt-3 text-xs text-rose-400">{errorMessage}</p>
        )}
        <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            type="button"
            onClick={onCancel}
            disabled={isPending}
            className="inline-flex h-11 items-center justify-center rounded-lg border border-white/10 px-4 text-sm font-medium text-ink transition hover:border-white/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={isPending}
            className="inline-flex h-11 items-center justify-center rounded-lg bg-accent px-4 text-sm font-bold text-white transition hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
          >
            {isPending ? "Archiving…" : "Archive"}
          </button>
        </div>
      </div>
    </div>
  );
}
