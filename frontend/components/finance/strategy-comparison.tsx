import clsx from "clsx";
import { PayoffPlan } from "@/lib/types";

const STRATEGY_LABELS: Record<string, string> = {
  snowball: "Snowball",
  avalanche: "Avalanche",
  hybrid: "Hybrid",
};

const STRATEGY_DESCRIPTIONS: Record<string, string> = {
  snowball: "Pays off smallest balances first for psychological wins.",
  avalanche: "Prioritizes highest APR to minimize total interest paid.",
  hybrid: "Balanced approach — 60% interest, 40% balance priority.",
};

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

export function StrategyComparison({
  activePlan,
  allPlans,
}: {
  activePlan: PayoffPlan | null;
  allPlans?: PayoffPlan[];
}) {
  const plans = allPlans?.length ? allPlans : activePlan ? [activePlan] : [];
  if (plans.length === 0) return null;

  const lowestInterest = Math.min(
    ...plans.map((p) => parseFloat(p.total_interest)),
  );

  return (
    <div className="space-y-3">
      {plans.map((plan) => {
        const isActive = activePlan?.id === plan.id;
        const interest = parseFloat(plan.total_interest);
        const savings = interest - lowestInterest;

        return (
          <article
            key={plan.id}
            className={clsx(
              "rounded-xl border p-4 transition-colors",
              isActive
                ? "border-accent/20 bg-accent/5"
                : "border-white/5 bg-white/5 opacity-60 hover:opacity-100",
            )}
          >
            <div className="flex items-center justify-between gap-2 mb-2">
              <h3 className="font-headline font-bold text-ink">
                {STRATEGY_LABELS[plan.strategy] ?? plan.strategy}
              </h3>
              {isActive && (
                <span className="flex items-center gap-1 text-[10px] font-bold text-accent">
                  <span className="inline-block h-1.5 w-1.5 rounded-full bg-accent" aria-hidden="true" />
                  Active
                </span>
              )}
            </div>

            <p className="text-xs text-ink-faint mb-3 leading-relaxed">
              {STRATEGY_DESCRIPTIONS[plan.strategy] ?? ""}
            </p>

            <div className="space-y-1.5 text-sm">
              <div className="flex justify-between">
                <span className="text-ink-muted">Payoff</span>
                <span className="font-mono font-medium text-ink">{plan.payoff_months} mo</span>
              </div>
              <div className="flex justify-between">
                <span className="text-ink-muted">Debt-free</span>
                <span className="font-mono font-medium text-ink">{formatPayoffDate(plan.payoff_date)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-ink-muted">Interest</span>
                <span className="font-mono font-medium text-ink">{formatCurrency(plan.total_interest)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-ink-muted">Budget</span>
                <span className="font-mono font-medium text-ink">{formatCurrency(plan.monthly_budget)}/mo</span>
              </div>
            </div>

            {savings > 0 && (
              <p className="mt-3 text-xs text-amber-text">
                +{formatCurrency(savings)} vs best strategy
              </p>
            )}
            {savings === 0 && plans.length > 1 && (
              <p className="mt-3 text-xs text-emerald-text">
                Lowest interest cost
              </p>
            )}
          </article>
        );
      })}
    </div>
  );
}
