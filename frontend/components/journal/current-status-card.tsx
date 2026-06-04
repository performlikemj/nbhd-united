"use client";

import clsx from "clsx";

import { useJournalStatusQuery } from "@/lib/queries";
import type { JournalObligation, ObligationStatus } from "@/lib/types";

function money(value: string): string {
  const n = Number(value);
  if (Number.isNaN(n)) return value;
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

// Parse a YYYY-MM-DD date as *local* (avoids the UTC-midnight off-by-one
// that `new Date("2026-06-05")` introduces in negative-offset timezones).
function shortDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso;
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const STATUS_LABEL: Record<ObligationStatus, string> = {
  paid: "Paid",
  partial: "Partial",
  unpaid: "Not yet paid",
};

function statusPillClass(ob: JournalObligation): string {
  if (ob.period_status === "paid") return "bg-signal-faint text-signal-text";
  if (ob.period_status === "partial") return "border border-amber-border bg-amber-bg text-amber-text";
  return "border border-rose-border bg-rose-bg text-rose-text"; // unpaid / overdue
}

/**
 * Read-only "Current status" surface for the Journal page.
 *
 * Renders live state from the typed-status projection (GET
 * /api/v1/journal/status/) — open tasks, active goals, and recurring
 * obligations with this-cycle payment status. It sits *beside* the daily
 * note's editable markdown (never merged into it), so the log stays a
 * historical record while this shows truth "as of now".
 */
export function CurrentStatusCard() {
  const { data, isLoading } = useJournalStatusQuery();
  if (isLoading || !data) return null;

  const { obligations, open_tasks: openTasks, active_goals: activeGoals } = data;
  if (!obligations.length && !openTasks.length && !activeGoals.length) return null;

  return (
    <div className="mx-4 mt-4 rounded-2xl border border-white/[0.06] bg-white/[0.02] p-4 lg:mx-6 lg:mt-6 lg:p-5">
      <div className="mb-3 flex items-center gap-2 text-[11px] uppercase tracking-[0.12em] text-ink-faint/60">
        Current status
        <span className="normal-case tracking-normal text-ink-faint/40">· as of now</span>
      </div>

      {obligations.length > 0 && (
        <div className="mb-3 space-y-1.5">
          {obligations.map((ob) => (
            <div key={ob.account_id} className="flex items-center justify-between gap-3 text-sm">
              <span className="min-w-0 truncate text-ink-muted">
                {ob.nickname}
                <span className="text-ink-faint">
                  {" "}
                  · {money(ob.minimum_payment)} due {shortDate(ob.due_date)}
                </span>
              </span>
              <span className={clsx("shrink-0 rounded-full px-2 py-0.5 text-xs font-medium", statusPillClass(ob))}>
                {ob.overdue && ob.period_status !== "paid" ? "Overdue" : STATUS_LABEL[ob.period_status]}
              </span>
            </div>
          ))}
        </div>
      )}

      {openTasks.length > 0 && (
        <div className="mb-2">
          <div className="mb-1 text-[11px] uppercase tracking-[0.12em] text-ink-faint/50">Open tasks</div>
          <ul className="space-y-1 text-sm text-ink-muted">
            {openTasks.map((t) => (
              <li key={t.id} className="flex items-center justify-between gap-3">
                <span className="min-w-0 truncate">{t.title}</span>
                {t.due_date && <span className="shrink-0 text-xs text-ink-faint">{shortDate(t.due_date)}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {activeGoals.length > 0 && (
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-[0.12em] text-ink-faint/50">Active goals</div>
          <ul className="space-y-1 text-sm text-ink-muted">
            {activeGoals.map((g) => (
              <li key={g.id} className="flex items-center justify-between gap-3">
                <span className="min-w-0 truncate">{g.title}</span>
                {g.target_date && <span className="shrink-0 text-xs text-ink-faint">{shortDate(g.target_date)}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
