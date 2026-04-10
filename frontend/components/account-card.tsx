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

const TYPE_EMOJI: Record<string, string> = {
  credit_card: "💳",
  student_loan: "🎓",
  personal_loan: "🏦",
  mortgage: "🏠",
  auto_loan: "🚗",
  medical_debt: "🏥",
  other_debt: "📄",
  savings: "💰",
  checking: "🏦",
  emergency_fund: "🛡️",
};

function formatCurrency(value: string | number): string {
  const num = typeof value === "string" ? parseFloat(value) : value;
  return num.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function AccountCard({
  account,
  onArchive,
}: {
  account: FinanceAccount;
  onArchive?: (account: FinanceAccount) => void;
}) {
  const typeLabel = TYPE_LABELS[account.account_type] ?? account.account_type;
  const emoji = TYPE_EMOJI[account.account_type] ?? "📄";
  const progress = account.payoff_progress;
  const isDebt = account.is_debt;
  const aprValue = account.interest_rate ? parseFloat(account.interest_rate).toFixed(1) : null;
  const originalBalance = account.original_balance ? parseFloat(account.original_balance) : null;
  const iconBg = isDebt ? "bg-accent/10" : "bg-signal/10";
  const barColor = isDebt ? "bg-accent" : "bg-signal";
  const barGlow = isDebt ? "shadow-[0_0_10px_rgba(124,107,240,0.5)]" : "shadow-[0_0_10px_rgba(78,205,196,0.5)]";

  return (
    <article className="group">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-3">
        <div className="flex items-center gap-3">
          <div className={`w-10 h-10 sm:w-12 sm:h-12 rounded-xl ${iconBg} flex items-center justify-center shrink-0 text-xl sm:text-2xl`}>
            {emoji}
          </div>
          <div>
            <h4 className="font-bold text-ink text-base sm:text-lg">{account.nickname}</h4>
            <p className="text-[10px] sm:text-xs text-ink-faint font-medium tracking-wide uppercase">
              {typeLabel}{aprValue ? ` \u2022 ${aprValue}% APR` : ""}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-6 sm:gap-10 text-right pl-13 sm:pl-0">
          {account.minimum_payment && (
            <div>
              <p className="text-[10px] text-ink-faint uppercase font-bold mb-0.5">Monthly</p>
              <p className="font-bold text-ink text-sm sm:text-base">{formatCurrency(account.minimum_payment)}</p>
            </div>
          )}
          <div>
            <p className="text-[10px] text-ink-faint uppercase font-bold mb-0.5">Balance</p>
            <p className="font-bold text-ink text-sm sm:text-base">{formatCurrency(account.current_balance)}</p>
          </div>
          {onArchive && (
            <button
              type="button"
              onClick={() => onArchive(account)}
              aria-label={`Archive ${account.nickname}`}
              className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg text-ink-faint transition-colors hover:bg-white/5 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100"
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="h-5 w-5"
                aria-hidden="true"
              >
                <path d="M20.54 5.23 19.15 3.55A1.99 1.99 0 0 0 17.62 3H6.38c-.62 0-1.18.28-1.54.75L3.46 5.23A2 2 0 0 0 3 6.5V19a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6.5c0-.45-.17-.88-.46-1.27Z" />
                <path d="M3 7h18" />
                <path d="M10 12h4" />
              </svg>
            </button>
          )}
        </div>
      </div>

      {/* Progress bar */}
      {progress !== null && progress !== undefined && isDebt && (
        <>
          <div
            className="w-full h-2 rounded-full overflow-hidden bg-surface-elevated"
            role="progressbar"
            aria-valuenow={Math.round(progress)}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label={`${Math.round(progress)}% paid off`}
          >
            <div
              className={`h-full rounded-full ${barColor} ${barGlow} transition-[width] duration-500`}
              style={{ width: `${Math.min(100, Math.max(0, progress))}%` }}
            />
          </div>
          <div className="flex justify-between mt-1.5">
            <span className="text-[10px] font-bold text-ink-faint uppercase">
              {Math.round(progress)}% Paid
            </span>
            {originalBalance && originalBalance > 0 && (
              <span className="text-[10px] font-bold text-ink-faint uppercase">
                {formatCurrency(originalBalance)} Original
              </span>
            )}
          </div>
        </>
      )}
    </article>
  );
}
