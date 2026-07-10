"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { APP_STORE_URL } from "@/components/app-store-badge";

const SHARE_TEXT = "Join me on NBHD United — your AI-powered personal assistant.";

/**
 * Info affordance on the transparency page that turns the "platform share" line
 * into a growth nudge: it explains that the fixed cost of the shared machinery
 * is split evenly among active neighbors, shrinks as the neighborhood grows, and
 * that a smaller share means more of what you pay reaches the local food
 * initiatives NBHD United supports — then offers a "Share NBHD with a friend"
 * CTA (Web Share API with a copy-link fallback to the App Store listing).
 *
 * Accessible: the trigger is a labelled button (44px target) with
 * aria-haspopup/expanded; the card is a focus-managed, Esc-dismissible dialog;
 * entrance motion is gated behind motion-safe.
 */
export function PlatformShareInfo({
  amount,
  variant = "icon",
}: {
  amount?: number;
  variant?: "icon" | "inline";
}) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [mounted, setMounted] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const bodyId = useId();

  // Portal target is only available after mount (static export has no document
  // during prerender).
  useEffect(() => setMounted(true), []);

  const close = useCallback(() => {
    setOpen(false);
    // Return focus to the trigger so keyboard users aren't dropped at the top.
    triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        close();
        return;
      }
      if (e.key !== "Tab" || !cardRef.current) return;
      // Minimal focus trap: keep Tab / Shift+Tab within the dialog.
      const focusable = cardRef.current.querySelectorAll<HTMLElement>(
        'button, a[href], [tabindex]:not([tabindex="-1"])',
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    document.body.style.overflow = "hidden";
    // Move focus into the dialog on open.
    const raf = window.requestAnimationFrame(() => {
      cardRef.current?.querySelector<HTMLElement>("button, a[href]")?.focus();
    });
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      window.cancelAnimationFrame(raf);
      document.body.style.overflow = "";
    };
  }, [open, close]);

  const handleShare = useCallback(async () => {
    if (typeof navigator !== "undefined" && typeof navigator.share === "function") {
      try {
        await navigator.share({ title: "NBHD United", text: SHARE_TEXT, url: APP_STORE_URL });
        return;
      } catch {
        // User dismissed the share sheet, or it failed — leave the card open so
        // they can use the copy fallback below.
      }
    }
    try {
      await navigator.clipboard?.writeText(APP_STORE_URL);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard blocked (insecure context / denied) — the link is shown in the
      // card as a last-resort fallback.
    }
  }, []);

  const trigger =
    variant === "inline" ? (
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label="Learn how the platform share works and shrinks as the neighborhood grows"
        className="-my-2 inline-flex min-h-[44px] items-center gap-1 rounded-md px-1 align-middle text-accent underline decoration-dotted underline-offset-2 transition-colors hover:text-accent-hover motion-reduce:transition-none"
      >
        <InfoGlyph />
        <span className="sr-only">About the platform share</span>
      </button>
    ) : (
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label="What is the platform share?"
        className="-m-2 inline-flex h-11 w-11 items-center justify-center rounded-full text-ink-faint transition-colors hover:text-ink motion-reduce:transition-none"
      >
        <InfoGlyph />
      </button>
    );

  return (
    <>
      {trigger}
      {open &&
        mounted &&
        createPortal(
          <div className="fixed inset-0 z-[100] flex items-end justify-center p-0 sm:items-center sm:p-4">
          {/* Backdrop */}
          <button
            type="button"
            aria-label="Close"
            tabIndex={-1}
            onClick={close}
            className="absolute inset-0 cursor-default bg-overlay/60 backdrop-blur-sm"
          />

          <div
            ref={cardRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            aria-describedby={bodyId}
            className="relative z-10 w-full rounded-t-2xl border border-border bg-surface-elevated p-5 shadow-xl sm:max-w-sm sm:rounded-2xl motion-safe:animate-reveal"
          >
            <div className="flex items-start justify-between gap-3">
              <h3 id={titleId} className="font-headline text-base font-semibold text-ink">
                Your platform share
              </h3>
              <button
                type="button"
                onClick={close}
                aria-label="Close"
                className="-m-2 inline-flex h-11 w-11 items-center justify-center rounded-full text-ink-faint transition-colors hover:text-ink motion-reduce:transition-none"
              >
                <svg viewBox="0 0 20 20" fill="none" aria-hidden="true" className="h-4 w-4">
                  <path
                    d="M5 5l10 10M15 5L5 15"
                    stroke="currentColor"
                    strokeWidth="1.6"
                    strokeLinecap="round"
                  />
                </svg>
              </button>
            </div>

            <div id={bodyId} className="mt-3 space-y-3 text-sm leading-relaxed text-ink-muted">
              <p>
                Shared machinery — the control plane, container registry, and security services — keeps
                every assistant online. That cost is fixed, so we split it evenly among active neighbors.
                {typeof amount === "number" ? (
                  <>
                    {" "}
                    The <span className="font-mono text-ink">${amount.toFixed(2)}</span> here is your slice.
                  </>
                ) : (
                  " The amount shown here is your slice."
                )}
              </p>
              <p>As the neighborhood grows, that fixed cost splits more ways — so each neighbor&apos;s share gets smaller.</p>
              <p>
                A smaller platform share means more of what you pay goes to the local food initiatives
                NBHD United supports.
              </p>
            </div>

            <div className="mt-4 border-t border-border pt-4">
              <button
                type="button"
                onClick={handleShare}
                className="inline-flex min-h-[44px] w-full items-center justify-center gap-2 rounded-xl border border-accent/30 bg-accent/15 px-4 py-2.5 text-sm font-medium text-accent transition-colors hover:bg-accent/25 motion-reduce:transition-none"
              >
                {copied ? "Link copied" : "Share NBHD with a friend"}
              </button>
              <p className="mt-2 text-center text-xs text-ink-faint">
                Grow the neighborhood — everyone&apos;s share gets smaller.
              </p>
            </div>
          </div>
        </div>,
          document.body,
        )}
    </>
  );
}

function InfoGlyph() {
  return (
    <svg viewBox="0 0 20 20" fill="none" aria-hidden="true" className="h-4 w-4">
      <circle cx="10" cy="10" r="8" stroke="currentColor" strokeWidth="1.4" />
      <path d="M10 9v4.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      <circle cx="10" cy="6.4" r="0.95" fill="currentColor" />
    </svg>
  );
}
