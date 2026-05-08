"use client";

import clsx from "clsx";

import { useTenantQuery } from "@/lib/queries";

interface PendingConfigChipProps {
  className?: string;
  /** Override the default copy. */
  label?: string;
}

/**
 * Quiet eyebrow indicator that surfaces when a settings change has been
 * queued because the assistant is hibernated. Gated on
 * `hibernated_at != null` so awake tenants — whose synchronous applies
 * have already been pushed and will be reconciled on the next
 * `apply_pending_configs` tick — don't see "applies when wakes" copy.
 */
export function PendingConfigChip({
  className,
  label = "Applies when assistant wakes",
}: PendingConfigChipProps) {
  const { data: tenant } = useTenantQuery();

  if (!tenant?.hibernated_at) return null;
  if ((tenant.pending_config_version ?? 0) <= (tenant.config_version ?? 0)) {
    return null;
  }

  return (
    <span
      role="status"
      className={clsx(
        "mb-3 inline-flex items-center gap-1.5 rounded-full",
        "border border-border bg-surface/60 px-2.5 py-1",
        "font-mono text-[10px] uppercase tracking-[0.14em] text-ink-muted",
        "backdrop-blur-sm",
        className,
      )}
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
