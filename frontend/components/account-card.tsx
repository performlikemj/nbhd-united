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

export function AccountCard({ account }: { account: FinanceAccount }) {
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
