"use client";

/**
 * PendingConfigChip — small eyebrow-style indicator that surfaces when the
 * tenant has settings changes that are queued for the next time the
 * assistant wakes. Triggered by `pending_config_version > config_version`
 * on the tenant detail payload.
 *
 * Backed by the deferred-gateway-call flow: when a settings PATCH runs
 * while the container is hibernated, the API returns `applied: "pending"`
 * and bumps `pending_config_version`. This chip gives the user a quiet
 * confirmation that the change landed in the queue, without implying
 * anything is broken.
 *
 * Styling follows DESIGN.md eyebrow pattern: monospace, uppercase, wide
 * tracking, muted ink. No hardcoded hex; only Tailwind tokens.
 */

interface PendingConfigChipProps {
  pendingVersion: number | null | undefined;
  version: number | null | undefined;
  className?: string;
  /** Override default copy. */
  label?: string;
}

export function PendingConfigChip({
  pendingVersion,
  version,
  className = "",
  label = "Applies when assistant wakes",
}: PendingConfigChipProps) {
  const pending = typeof pendingVersion === "number" ? pendingVersion : 0;
  const current = typeof version === "number" ? version : 0;

  if (pending <= current) {
    return null;
  }

  return (
    <span
      role="status"
      className={[
        "inline-flex items-center gap-1.5 rounded-full",
        "border border-border bg-surface/60 px-2.5 py-1",
        "font-mono text-[10px] uppercase tracking-[0.14em] text-ink-muted",
        "backdrop-blur-sm",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <span
        aria-hidden="true"
        className="inline-block h-1.5 w-1.5 rounded-full bg-accent"
      />
      {label}
    </span>
  );
}

export default PendingConfigChip;
