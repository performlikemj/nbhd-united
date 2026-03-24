import clsx from "clsx";
import { PayoffPlan } from "@/lib/types";

const STRATEGY_LABELS: Record<string, string> = {
  snowball: "Snowball",
  avalanche: "Avalanche",
  hybrid: "Hybrid",
};

const STRATEGY_DESCRIPTIONS: Record<string, string> = {
  snowball: "Lowest balance first",
  avalanche: "Highest interest first",
  hybrid: "Balanced approach",
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
  // If we only have the active plan, show a single card
  const plans = allPlans?.length ? allPlans : activePlan ? [activePlan] : [];
  if (plans.length === 0) return null;

  // Find the best (lowest interest) for comparison
  const lowestInterest = Math.min(
    ...plans.map((p) => parseFloat(p.total_interest)),
  );

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
      {plans.map((plan) => {
        const isActive = activePlan?.id === plan.id;
        const interest = parseFloat(plan.total_interest);
        const savings = interest - lowestInterest;

        return (
          <article
            key={plan.id}
            className={clsx(
              "rounded-panel border p-4 transition-colors",
              isActive
                ? "border-accent/30 bg-accent/5"
                : "border-border bg-card/95",
            )}
          >
            <div className="flex items-center justify-between gap-2">
              <h3 className="font-display text-lg text-ink">
                {STRATEGY_LABELS[plan.strategy] ?? plan.strategy}
              </h3>
              {isActive ? (
                <span className="flex items-center gap-1 rounded-full bg-accent/15 px-2 py-0.5 text-xs font-medium text-accent">
                  <span
                    className="inline-block h-1.5 w-1.5 rounded-full bg-accent"
                    aria-hidden="true"
                  />
                  Active
                </span>
              ) : null}
            </div>

            <p className="mt-1 text-xs text-ink-faint">
              {STRATEGY_DESCRIPTIONS[plan.strategy] ?? ""}
            </p>

            <div className="mt-3 space-y-1.5">
              <div className="flex justify-between text-sm">
                <span className="text-ink-muted">Payoff in</span>
                <span className="font-mono font-medium text-ink">
                  {plan.payoff_months} months
                </span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-ink-muted">Free by</span>
                <span className="font-mono font-medium text-ink">
                  {formatPayoffDate(plan.payoff_date)}
                </span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-ink-muted">Total interest</span>
                <span className="font-mono font-medium text-ink">
                  {formatCurrency(plan.total_interest)}
                </span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-ink-muted">Monthly budget</span>
                <span className="font-mono font-medium text-ink">
                  {formatCurrency(plan.monthly_budget)}
                </span>
              </div>
            </div>

            {savings > 0 ? (
              <p className="mt-3 text-xs text-amber-text">
                +{formatCurrency(savings)} vs best strategy
              </p>
            ) : plans.length > 1 ? (
              <p className="mt-3 text-xs text-emerald-text">
                Lowest interest cost
              </p>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}
