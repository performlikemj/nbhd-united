"use client";

import clsx from "clsx";
import { useCallback, useEffect, useRef, useState } from "react";

import { useConnectByoMutation } from "@/lib/queries";

type Props = {
  open: boolean;
  onClose: () => void;
};

type Step = "info" | "paste";

export function ConnectAnthropicModal({ open, onClose }: Props) {
  const connect = useConnectByoMutation();
  const [step, setStep] = useState<Step>("info");
  const [token, setToken] = useState("");
  const [copied, setCopied] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const containerRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // AbortController for the in-flight connect request. The user can dismiss
  // the modal at any time; if a request is in flight we abort it so the
  // promise rejects locally and the modal closes cleanly. The server-side
  // write may still complete — that's fine, `onSettled` invalidates the
  // credentials query either way.
  const abortRef = useRef<AbortController | null>(null);

  const reset = useCallback(() => {
    setStep("info");
    setToken("");
    setCopied(false);
    setErrorMsg(null);
  }, []);

  // Close handler — always succeeds. If a mutation is in flight, abort it
  // first so the user is never trapped behind a hung verify call.
  const requestClose = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    onClose();
  }, [onClose]);

  useEffect(() => {
    if (!open) {
      const t = setTimeout(reset, 200);
      return () => clearTimeout(t);
    }
  }, [open, reset]);

  // Focus management + esc-to-close. Escape always closes — pending state
  // does not trap.
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

  // When transitioning to paste, focus the textarea
  useEffect(() => {
    if (step === "paste" && open) {
      const t = setTimeout(() => textareaRef.current?.focus(), 50);
      return () => clearTimeout(t);
    }
  }, [step, open]);

  // Cleanup on unmount: if the modal disappears entirely, abort any
  // request still in flight.
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, []);

  const copyCommand = async () => {
    try {
      await navigator.clipboard.writeText("claude setup-token");
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    } catch {
      /* clipboard blocked — user copies manually */
    }
  };

  const submitToken = async () => {
    setErrorMsg(null);
    const trimmed = token.trim();
    if (trimmed.length < 32) {
      setErrorMsg(
        "Token format looks invalid. Make sure you copied the entire output (it should be at least 32 characters).",
      );
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await connect.mutateAsync({
        data: {
          provider: "anthropic",
          mode: "cli_subscription",
          token: trimmed,
        },
        signal: controller.signal,
      });
      abortRef.current = null;
      onClose();
    } catch (err) {
      // If the user cancelled, the modal is already closing — don't
      // resurrect it with an error message.
      if (controller.signal.aborted) return;
      abortRef.current = null;
      const status = (err as Error & { status?: number }).status;
      if (status === 400) {
        setErrorMsg("Token couldn't be saved — check the format and try again.");
      } else if (status === 502) {
        setErrorMsg("Credential storage failed. Try again in a moment.");
      } else {
        setErrorMsg(
          err instanceof Error ? err.message : "Could not connect. Please try again.",
        );
      }
    }
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[80] flex items-end justify-center p-0 sm:items-center sm:p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="connect-anthropic-title"
      onClick={requestClose}
    >
      <div className="absolute inset-0 bg-overlay backdrop-blur-md" aria-hidden="true" />
      <div
        ref={containerRef}
        onClick={(e) => e.stopPropagation()}
        className={clsx(
          "glass-card-horizons relative flex w-full flex-col rounded-t-2xl shadow-panel animate-reveal",
          "max-h-[92vh] overflow-y-auto",
          "sm:max-w-lg sm:rounded-2xl sm:max-h-[85vh]",
        )}
      >
        {/* Mobile drag handle */}
        <div className="sm:hidden flex justify-center pt-2.5 pb-1">
          <span className="h-1 w-9 rounded-full bg-white/20" aria-hidden="true" />
        </div>

        {/* Sticky close button — always reachable, even when content
            scrolls (PR #426 callout makes step 1 taller; step 2 with an
            error is taller still). Sits in the scroll container so it
            stays visible above the header on tall content. */}
        <button
          type="button"
          onClick={requestClose}
          className="absolute right-3 top-3 z-10 flex min-h-[44px] min-w-[44px] items-center justify-center rounded-full bg-surface/80 text-ink-muted backdrop-blur-md transition hover:bg-surface-hover hover:text-ink sm:right-4 sm:top-4"
          aria-label="Close"
        >
          <span aria-hidden="true">✕</span>
        </button>

        <div className="px-6 pt-4 pb-2 sm:p-8 sm:pb-2">
          {/* Header — pad right so it doesn't collide with the sticky X */}
          <div className="pr-12 sm:pr-14">
            <h2
              id="connect-anthropic-title"
              className="font-headline text-xl font-bold text-ink sm:text-2xl"
            >
              Connect Anthropic
            </h2>
            <p className="mt-1 text-sm text-ink-muted">
              {step === "info"
                ? "Step 1 of 2 — generate a token from your Claude Code CLI."
                : "Step 2 of 2 — paste the token from your terminal."}
            </p>
          </div>

          {/* Step indicator */}
          <div className="mt-4 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.18em]">
            <span
              className={clsx(
                "flex h-6 w-6 items-center justify-center rounded-full",
                step === "info" ? "bg-accent text-white" : "bg-accent/30 text-accent",
              )}
            >
              1
            </span>
            <span className="h-px flex-1 bg-border" aria-hidden="true" />
            <span
              className={clsx(
                "flex h-6 w-6 items-center justify-center rounded-full",
                step === "paste"
                  ? "bg-accent text-white"
                  : "border border-border bg-transparent text-ink-faint",
              )}
            >
              2
            </span>
          </div>
        </div>

        <div className="flex-1 px-6 pb-6 sm:p-8 sm:pt-4">
          {step === "info" ? (
            <div className="space-y-5 sm:space-y-6">
              <p className="text-sm text-ink-muted sm:text-base">
                Bring your own Claude Pro or Max subscription. We&apos;ll route Claude
                Sonnet 4.6 through your account, and your conversations never charge
                tokens to your platform plan.
              </p>

              <div>
                <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-ink-faint">
                  Step 1
                </p>
                <p className="mt-1.5 text-sm text-ink-muted sm:text-base">
                  On any computer with the Claude Code CLI installed, run:
                </p>
                <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-stretch">
                  <code className="flex-1 rounded-xl border border-white/10 bg-white/[0.04] px-4 py-3 font-mono text-sm text-ink">
                    claude setup-token
                  </code>
                  <button
                    type="button"
                    onClick={copyCommand}
                    className="rounded-xl border border-border bg-surface px-4 py-3 text-sm font-medium text-ink-muted transition hover:bg-surface-hover hover:text-ink min-h-[44px] sm:min-w-[100px]"
                  >
                    {copied ? "Copied" : "Copy"}
                  </button>
                </div>
                <p className="mt-3 text-sm text-ink-muted">
                  The CLI walks you through OAuth and prints a 1-year token to your
                  terminal. Copy it; in step 2 you&apos;ll paste it here.
                </p>
              </div>

              <section
                aria-labelledby="byo-privacy-heading"
                className="rounded-xl border border-border bg-surface/40 p-4"
              >
                <p
                  id="byo-privacy-heading"
                  className="text-sm font-semibold text-ink"
                >
                  What we store
                </p>
                <ul className="mt-2 space-y-1.5 text-sm text-ink-muted">
                  <li className="flex gap-2">
                    <span aria-hidden="true" className="text-ink-faint">·</span>
                    <span>Encrypted at rest in your private Azure Key Vault</span>
                  </li>
                  <li className="flex gap-2">
                    <span aria-hidden="true" className="text-ink-faint">·</span>
                    <span>Read by your isolated container only — we never proxy your prompts</span>
                  </li>
                  <li className="flex gap-2">
                    <span aria-hidden="true" className="text-ink-faint">·</span>
                    <span>Revoke any time at console.anthropic.com</span>
                  </li>
                </ul>
              </section>
            </div>
          ) : (
            <div className="space-y-5 sm:space-y-6">
              <p className="text-sm text-ink-muted sm:text-base">
                Paste the entire token output from your terminal below. We never display it again after this.
              </p>

              <div>
                <label
                  htmlFor="byo-anthropic-token"
                  className="block font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint"
                >
                  Anthropic OAuth Token
                </label>
                <textarea
                  id="byo-anthropic-token"
                  ref={textareaRef}
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  rows={5}
                  spellCheck={false}
                  autoCorrect="off"
                  autoCapitalize="off"
                  disabled={connect.isPending}
                  placeholder="Paste the token from `claude setup-token`"
                  className="mt-1.5 w-full resize-y rounded-xl border border-white/10 bg-white/[0.05] px-4 py-3 font-mono text-sm text-[#e0e3e8] placeholder:text-white/25 focus:border-[#5dd9d0]/50 focus:shadow-[0_0_8px_rgba(93,217,208,0.15)] outline-none transition"
                />
                <p className="mt-2 text-xs text-ink-muted">
                  Tokens last about a year. We&apos;ll show an expiry banner ~14 days before.
                </p>
              </div>

              {errorMsg ? (
                <p
                  role="alert"
                  className="rounded-xl border border-rose-border bg-rose-bg px-4 py-2.5 text-sm text-rose-text"
                >
                  {errorMsg}
                </p>
              ) : null}

              <section
                aria-labelledby="byo-next-heading"
                className="rounded-xl border border-border bg-surface/40 p-4"
              >
                <p id="byo-next-heading" className="text-sm font-semibold text-ink">
                  What happens next
                </p>
                <ol className="mt-2 space-y-2 text-sm text-ink-muted">
                  {[
                    "Token written to your private Azure Key Vault",
                    "Your container restarts to pick up the new credential (~30s)",
                    "First message confirms it works — and routes through your subscription",
                  ].map((line, idx) => (
                    <li key={idx} className="flex gap-2.5">
                      <span
                        aria-hidden="true"
                        className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accent/15 font-mono text-[10px] text-accent"
                      >
                        {idx + 1}
                      </span>
                      <span>{line}</span>
                    </li>
                  ))}
                </ol>
              </section>
            </div>
          )}
        </div>

        {/* Sticky footer. Cancel is always enabled and never trapped — even
            during a pending verify, it cancels the request and dismisses
            the modal. Only the destructive/forward CTAs disable on
            pending. */}
        <div className="sticky bottom-0 border-t border-border bg-surface/95 px-6 py-4 backdrop-blur-md sm:px-8">
          {step === "info" ? (
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
                onClick={() => setStep("paste")}
                className="glow-purple rounded-full bg-accent px-5 py-2.5 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98] min-h-[44px]"
              >
                I have my token →
              </button>
            </div>
          ) : (
            <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-between sm:gap-3">
              <div className="flex flex-col-reverse gap-2 sm:flex-row sm:gap-3">
                <button
                  type="button"
                  onClick={requestClose}
                  className="rounded-full border border-border px-5 py-2.5 text-sm font-medium text-ink-muted transition hover:bg-surface-hover min-h-[44px]"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => setStep("info")}
                  disabled={connect.isPending}
                  className="rounded-full border border-border px-5 py-2.5 text-sm font-medium text-ink-muted transition hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
                >
                  ← Back
                </button>
              </div>
              <button
                type="button"
                onClick={submitToken}
                disabled={connect.isPending || token.trim().length < 32}
                className="glow-purple rounded-full bg-accent px-5 py-2.5 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
              >
                {connect.isPending ? "Verifying…" : "Connect"}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
