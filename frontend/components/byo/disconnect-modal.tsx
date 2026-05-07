"use client";

import clsx from "clsx";
import { useCallback, useEffect, useRef, useState } from "react";

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
  // AbortController for the in-flight disconnect request. The user must
  // always be able to dismiss the modal, even if the DELETE call hangs
  // (e.g. slow Key Vault soft-delete). Aborting here just stops the UI
  // from waiting; the server-side delete may still complete and
  // `onSettled` invalidates the credentials query either way.
  const abortRef = useRef<AbortController | null>(null);

  const requestClose = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    onClose();
  }, [onClose]);

  useEffect(() => {
    if (!open) {
      const t = setTimeout(() => setErrorMsg(null), 200);
      return () => clearTimeout(t);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") requestClose();
    };
    window.addEventListener("keydown", handler);

    const first = containerRef.current?.querySelector<HTMLElement>(
      "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])",
    );
    first?.focus();
    return () => window.removeEventListener("keydown", handler);
  }, [open, requestClose]);

  // Cleanup on unmount: abort any request still in flight.
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, []);

  const submit = async () => {
    if (!cred) return;
    setErrorMsg(null);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await disconnect.mutateAsync({ id: cred.id, signal: controller.signal });
      abortRef.current = null;
      onClose();
    } catch (err) {
      // User cancelled — modal is already closing, don't surface an error.
      if (controller.signal.aborted) return;
      abortRef.current = null;
      setErrorMsg(
        err instanceof Error ? err.message : "Could not disconnect. Please try again.",
      );
    }
  };

  if (!open || !cred) return null;

  return (
    <div
      className="fixed inset-0 z-[80] flex items-end justify-center p-0 sm:items-center sm:p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="byo-disconnect-title"
      onClick={requestClose}
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

        {/* Always-visible close button — pinned in the scroll container so
            it stays reachable even if the body grows (error state). */}
        <button
          type="button"
          onClick={requestClose}
          className="absolute right-3 top-3 z-10 flex min-h-[44px] min-w-[44px] items-center justify-center rounded-full bg-surface/80 text-ink-muted backdrop-blur-md transition hover:bg-surface-hover hover:text-ink sm:right-4 sm:top-4"
          aria-label="Close"
        >
          <span aria-hidden="true">✕</span>
        </button>

        <div className="px-6 pt-4 sm:p-8 sm:pb-2">
          {/* Pad right so the title doesn't collide with the sticky X */}
          <div className="flex items-start gap-3 pr-12 sm:pr-14">
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

        {/* Sticky footer. Cancel is always enabled — even during a pending
            disconnect, it aborts and dismisses. Only the destructive
            button disables on pending. */}
        <div className="sticky bottom-0 border-t border-border bg-surface/95 px-6 py-4 backdrop-blur-md sm:px-8">
          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end sm:gap-3">
            <button
              type="button"
              onClick={requestClose}
              className="rounded-full border border-border px-5 py-2.5 text-sm font-medium text-ink-muted transition hover:bg-surface-hover min-h-[44px]"
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
