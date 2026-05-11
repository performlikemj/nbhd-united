"use client";

import { useEffect, useRef, useState } from "react";

import { SectionCard } from "@/components/section-card";
import {
  useCancelPendingReminderMutation,
  usePendingRemindersQuery,
} from "@/lib/queries";
import type { PendingReminder } from "@/lib/types";

/** Inline cancel confirmation timeout — mirrors the recurring tasks section. */
const CANCEL_CONFIRM_TIMEOUT_MS = 3000;

interface PendingRemindersSectionProps {
  /** User's profile timezone, used to render absolute fire times in the
   * user's local wall-clock. Falls back to the browser TZ if missing. */
  userTz?: string;
}

interface FormattedTime {
  absolute: string;
  relative: string;
}

function formatFiresAt(firesAtMs: number | null, userTz: string, nowMs: number): FormattedTime {
  if (firesAtMs == null) {
    return { absolute: "Unknown time", relative: "" };
  }
  const fires = new Date(firesAtMs);
  // Absolute: same-day → "Today at 4:00 PM"; tomorrow → "Tomorrow at 10:00 AM";
  // further out → "Wed, May 14 at 9:00 AM".
  const sameDay = isSameDay(fires, new Date(nowMs), userTz);
  const tomorrow = isSameDay(fires, addDays(new Date(nowMs), 1), userTz);
  const timePart = new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
    timeZone: userTz,
  }).format(fires);
  let absolute: string;
  if (sameDay) absolute = `Today at ${timePart}`;
  else if (tomorrow) absolute = `Tomorrow at ${timePart}`;
  else {
    const datePart = new Intl.DateTimeFormat(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      timeZone: userTz,
    }).format(fires);
    absolute = `${datePart} at ${timePart}`;
  }
  return { absolute, relative: formatRelative(firesAtMs - nowMs) };
}

function formatRelative(deltaMs: number): string {
  if (deltaMs <= 0) return "any moment";
  const totalMin = Math.round(deltaMs / 60_000);
  if (totalMin < 1) return "in <1 min";
  if (totalMin < 60) return `in ${totalMin} min`;
  const totalHr = Math.round(deltaMs / 3_600_000);
  if (totalHr < 24) return totalHr === 1 ? "in 1 hour" : `in ${totalHr} hours`;
  const totalDay = Math.round(deltaMs / 86_400_000);
  return totalDay === 1 ? "in 1 day" : `in ${totalDay} days`;
}

function isSameDay(a: Date, b: Date, tz: string): boolean {
  const fmt = new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", timeZone: tz });
  return fmt.format(a) === fmt.format(b);
}

function addDays(d: Date, n: number): Date {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

export function PendingRemindersSection({ userTz }: PendingRemindersSectionProps) {
  const tz = userTz || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const { data, isLoading, error } = usePendingRemindersQuery();
  const cancelMutation = useCancelPendingReminderMutation();

  // Refresh relative times every 30s without a server round-trip.
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);

  // Inline cancel-confirm UX (matches the recurring tasks section pattern).
  const [confirmingName, setConfirmingName] = useState<string | null>(null);
  const [cancelError, setCancelError] = useState<Record<string, string>>({});
  const cancelTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (cancelTimer.current) clearTimeout(cancelTimer.current);
    };
  }, []);

  if (isLoading && !data) {
    return null; // Stay silent while the first fetch is in flight.
  }

  if (error) {
    return (
      <SectionCard
        title="Pending Reminders"
        subtitle="One-off reminders set during conversation"
      >
        <p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">
          Couldn&rsquo;t load pending reminders right now — try again in a moment.
        </p>
      </SectionCard>
    );
  }

  const jobs = data?.jobs ?? [];
  const softCap = data?.soft_cap ?? 20;
  const stale = data?.stale ?? false;
  const count = jobs.length;
  const capTone =
    count >= softCap
      ? "border-rose-border bg-rose-bg text-rose-text"
      : count >= softCap - 2
        ? "border-amber-border bg-amber-bg text-amber-text"
        : "border-border bg-surface-hover text-ink-muted";

  const handleCancelClick = (name: string) => {
    setCancelError((prev) => ({ ...prev, [name]: "" }));
    setConfirmingName(name);
    if (cancelTimer.current) clearTimeout(cancelTimer.current);
    cancelTimer.current = setTimeout(() => {
      setConfirmingName((prev) => (prev === name ? null : prev));
    }, CANCEL_CONFIRM_TIMEOUT_MS);
  };

  const handleCancelConfirm = (job: PendingReminder) => {
    setConfirmingName(null);
    if (cancelTimer.current) clearTimeout(cancelTimer.current);
    cancelMutation.mutate(job.name, {
      onError: (err) =>
        setCancelError((prev) => ({
          ...prev,
          [job.name]: err instanceof Error ? err.message : "Cancel failed.",
        })),
    });
  };

  return (
    <SectionCard
      title="Pending Reminders"
      subtitle="One-off reminders you set during conversation. They fire once and clean themselves up."
    >
      {/* Header: count badge + stale banner */}
      <div className="mb-3 flex items-center justify-between gap-3">
        <span
          className={`inline-flex items-center rounded-full border px-2.5 py-1 text-xs font-medium ${capTone}`}
        >
          {count} of {softCap}
        </span>
        {stale && (
          <span className="text-xs text-ink-faint" role="status">
            Container is asleep — list may be slightly out of date.
          </span>
        )}
      </div>

      {count === 0 ? (
        <p className="rounded-panel border border-border bg-surface-hover/40 p-4 text-sm text-ink-muted">
          No pending reminders. Ask your assistant anytime — e.g. &ldquo;remind me in
          20 minutes&rdquo; or &ldquo;ping me at 4pm.&rdquo;
        </p>
      ) : (
        <ul className="space-y-2">
          {jobs.map((job) => {
            const time = formatFiresAt(job.firesAtMs, tz, nowMs);
            const isConfirming = confirmingName === job.name;
            const isPending = cancelMutation.isPending && cancelMutation.variables === job.name;
            return (
              <li
                key={job.jobId ?? job.name}
                className="flex flex-col gap-2 rounded-panel border border-border bg-surface-elevated/40 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0">
                  <p className="truncate font-medium text-ink">{job.name}</p>
                  <p className="text-sm text-ink-muted">
                    {time.absolute}
                    {time.relative && <span className="text-ink-faint"> · {time.relative}</span>}
                  </p>
                  {cancelError[job.name] && (
                    <p className="mt-1 text-xs text-rose-text">{cancelError[job.name]}</p>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {isConfirming ? (
                    <>
                      <button
                        type="button"
                        onClick={() => handleCancelConfirm(job)}
                        disabled={isPending}
                        className="min-h-[44px] rounded-full border border-rose-border bg-rose-bg px-4 py-1.5 text-sm text-rose-text hover:bg-rose-bg/80 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {isPending ? "Cancelling…" : "Confirm cancel"}
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirmingName(null)}
                        className="min-h-[44px] rounded-full border border-border-strong px-3 py-1.5 text-sm text-ink-muted hover:text-ink"
                      >
                        Keep
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      onClick={() => handleCancelClick(job.name)}
                      className="min-h-[44px] rounded-full border border-border-strong px-4 py-1.5 text-sm text-ink-muted hover:bg-surface-hover hover:text-ink"
                    >
                      Cancel
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </SectionCard>
  );
}
