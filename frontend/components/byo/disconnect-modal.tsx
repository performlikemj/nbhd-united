"use client";

import clsx from "clsx";
import { useEffect, useRef, useState } from "react";

import { useDisconnectByoMutation } from "@/lib/queries";
import type { BYOCredential } from "@/lib/types";

type Props = {
  open: boolean;
  cred: BYOCredential | undefined;
  fallbackModelName: string;
  onClose: () => void;
};

export function DisconnectModal({ open, cred, fallbackModelName, onClose }: Props) {
  const disconnect = useDisconnectByoMutation();
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) {
      const t = setTimeout(() => setErrorMsg(null), 200);
      return () => clearTimeout(t);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !disconnect.isPending) onClose();
    };
    window.addEventListener("keydown", handler);

    const first = containerRef.current?.querySelector<HTMLElement>(
      "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])",
    );
    first?.focus();
    return () => window.removeEventListener("keydown", handler);
  }, [open, disconnect.isPending, onClose]);

  const submit = async () => {
    if (!cred) return;
    setErrorMsg(null);
    try {
      await disconnect.mutateAsync(cred.id);
      onClose();
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Could not disconnect. Please try again.",
      );
    }
  };

  if (!open || !cred) return null;

  const closable = !disconnect.isPending;

  return (
    <div
      className="fixed inset-0 z-[80] flex items-end justify-center p-0 sm:items-center sm:p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="byo-disconnect-title"
      onClick={() => closable && onClose()}
    >
      <div className="absolute inset-0 bg-overlay backdrop-blur-md" aria-hidden="true" />
      <div
        ref={containerRef}
        onClick={(e) => e.stopPropagation()}
        className={clsx(
          "glass-card-horizons relative flex w-full flex-col rounded-t-2xl shadow-panel animate-reveal",
          "max-h-[90vh] overflow-y-auto",
          "sm:max-w-md sm:rounded-2xl",
        )}
      >
        {/* Mobile drag handle */}
        <div className="sm:hidden flex justify-center pt-2.5 pb-1">
          <span className="h-1 w-9 rounded-full bg-white/20" aria-hidden="true" />
        </div>

        <div className="px-6 pt-4 sm:p-8 sm:pb-2">
          <div className="flex items-start gap-3">
            <span
              aria-hidden="true"
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-rose-bg text-rose-text"
            >
              ⚠
            </span>
            <div className="min-w-0 flex-1">
              <h2
                id="byo-disconnect-title"
                className="font-headline text-xl font-bold text-ink sm:text-2xl"
              >
                Disconnect Anthropic?
              </h2>
              <p className="mt-1 text-sm text-ink-muted">
                You&apos;ll fall back to {fallbackModelName}.
              </p>
            </div>
          </div>
        </div>

        <div className="flex-1 px-6 pb-6 sm:p-8 sm:pt-4">
          <p className="text-sm text-ink-muted sm:text-base">
            We&apos;ll remove your saved Claude Pro / Max credential from secure storage and stop routing inference through your subscription. Conversations continue using the platform&apos;s default model on your tier.
          </p>

          <ul className="mt-4 space-y-2 text-sm text-ink-muted">
            <li className="flex gap-2">
              <span aria-hidden="true" className="text-ink-faint">·</span>
              <span>Anthropic credential deleted from Azure Key Vault</span>
            </li>
            <li className="flex gap-2">
              <span aria-hidden="true" className="text-ink-faint">·</span>
              <span>Container restarts (~30s) to re-bind the platform key</span>
            </li>
            <li className="flex gap-2">
              <span aria-hidden="true" className="text-ink-faint">·</span>
              <span>Claude Sonnet 4.6 disappears from the picker until you reconnect</span>
            </li>
          </ul>

          <div className="mt-5 flex items-center justify-between rounded-xl border border-border bg-surface/40 px-4 py-3">
            <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
              Will revert to
            </span>
            <span className="text-sm font-semibold text-ink">{fallbackModelName}</span>
          </div>

          {errorMsg ? (
            <p
              role="alert"
              className="mt-4 rounded-xl border border-rose-border bg-rose-bg px-4 py-2.5 text-sm text-rose-text"
            >
              {errorMsg}
            </p>
          ) : null}
        </div>

        <div className="sticky bottom-0 border-t border-border bg-surface/95 px-6 py-4 backdrop-blur-md sm:px-8">
          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end sm:gap-3">
            <button
              type="button"
              onClick={onClose}
              disabled={disconnect.isPending}
              className="rounded-full border border-border px-5 py-2.5 text-sm font-medium text-ink-muted transition hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={submit}
              disabled={disconnect.isPending}
              className="rounded-full border border-rose-border bg-rose-bg/60 px-5 py-2.5 text-sm font-semibold text-rose-text transition hover:bg-rose-bg disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
            >
              {disconnect.isPending ? "Disconnecting…" : "Disconnect"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
