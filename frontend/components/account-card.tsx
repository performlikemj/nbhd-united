import { FinanceAccount } from "@/lib/types";

const TYPE_LABELS: Record<string, string> = {
  credit_card: "Credit Card",
  student_loan: "Student Loan",
  personal_loan: "Personal Loan",
  mortgage: "Mortgage",
  auto_loan: "Auto Loan",
  medical_debt: "Medical Debt",
  other_debt: "Debt",
  savings: "Savings",
  checking: "Checking",
  emergency_fund: "Emergency Fund",
};

const TYPE_TONES: Record<string, string> = {
  credit_card: "bg-status-rose text-status-rose-text",
  student_loan: "bg-status-indigo text-status-indigo-text",
  personal_loan: "bg-status-violet text-status-violet-text",
  mortgage: "bg-status-slate text-status-slate-text",
  auto_loan: "bg-status-sky text-status-sky-text",
  medical_debt: "bg-status-orange text-status-orange-text",
  other_debt: "bg-status-amber text-status-amber-text",
  savings: "bg-status-emerald text-status-emerald-text",
  checking: "bg-status-emerald text-status-emerald-text",
  emergency_fund: "bg-status-emerald text-status-emerald-text",
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

export function AccountCard({ account }: { account: FinanceAccount }) {
  const typeLabel = TYPE_LABELS[account.account_type] ?? account.account_type;
  const toneCls = TYPE_TONES[account.account_type] ?? "bg-status-slate text-status-slate-text";
  const progress = account.payoff_progress;

  return (
    <article className="rounded-panel border border-border bg-card/95 p-4 transition-colors hover:border-border-strong">
      <div className="flex items-start justify-between gap-2">
        <h3 className="font-display text-lg text-ink">{account.nickname}</h3>
        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${toneCls}`}>
          {typeLabel}
        </span>
      </div>

      <div className="mt-3 flex items-baseline gap-2">
        <span className="font-mono text-xl font-semibold text-ink">
          {formatCurrency(account.current_balance)}
        </span>
        {account.interest_rate ? (
          <span className="rounded bg-amber-bg px-1.5 py-0.5 font-mono text-xs text-amber-text">
            {parseFloat(account.interest_rate).toFixed(1)}% APR
          </span>
        ) : null}
      </div>

      {account.minimum_payment ? (
        <p className="mt-2 text-sm text-ink-muted">
          Min: {formatCurrency(account.minimum_payment)}/mo
        </p>
      ) : null}

      {progress !== null && progress !== undefined && account.is_debt ? (
        <div className="mt-3">
          <div
            className="h-2 overflow-hidden rounded-full bg-border"
            role="progressbar"
            aria-valuenow={Math.round(progress)}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label={`${Math.round(progress)}% paid off`}
          >
            <div
              className="h-full rounded-full bg-gradient-to-r from-accent to-signal transition-[width] duration-300"
              style={{ width: `${Math.min(100, Math.max(0, progress))}%` }}
            />
          </div>
          <p className="mt-1 font-mono text-xs text-ink-faint">
            {Math.round(progress)}% paid off
          </p>
        </div>
      ) : null}

      {account.due_day ? (
        <p className="mt-2 font-mono text-xs text-ink-faint">
          Due: {account.due_day}{ordinalSuffix(account.due_day)} of each month
        </p>
      ) : null}
    </article>
  );
}

function ordinalSuffix(n: number): string {
  const s = ["th", "st", "nd", "rd"];
  const v = n % 100;
  return s[(v - 20) % 10] || s[v] || s[0];
}
