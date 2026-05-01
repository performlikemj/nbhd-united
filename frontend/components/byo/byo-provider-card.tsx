"use client";

import clsx from "clsx";

import { StatusPill } from "@/components/status-pill";
import type { BYOCredential, BYOProvider } from "@/lib/types";

type Props = {
  provider: BYOProvider;
  cred: BYOCredential | undefined;
  onConnect: () => void;
  onDisconnect: () => void;
  disabled?: boolean;
};

const PROVIDER_META: Record<
  BYOProvider,
  {
    name: string;
    plan: string;
    glyph: string;
    glyphTone: string; // tailwind class
    body: string;
    eyebrow: string;
  }
> = {
  anthropic: {
    name: "Anthropic",
    plan: "Claude Pro / Max",
    glyph: "★",
    glyphTone: "bg-accent/15 text-accent",
    body: "Bring your own subscription. We'll route Claude Sonnet 4.6 through your account.",
    eyebrow: "SAME TIER PRICE · YOU PAY ANTHROPIC FOR TOKENS",
  },
  openai: {
    name: "OpenAI",
    plan: "ChatGPT Plus / Pro",
    glyph: "◎",
    glyphTone: "bg-signal/15 text-signal-text",
    body: "Codex CLI integration is in the next phase.",
    eyebrow: "AVAILABLE NEXT PHASE",
  },
};

function statusFor(cred: BYOCredential | undefined): string {
  if (!cred) return "Not connected";
  return cred.status;
}

function ctaLabel(cred: BYOCredential | undefined): { label: string; tone: "primary" | "ghost" | "rose" } {
  if (!cred) return { label: "Connect →", tone: "primary" };
  if (cred.status === "pending") return { label: "Verifying…", tone: "ghost" };
  if (cred.status === "verified") return { label: "Disconnect", tone: "ghost" };
  if (cred.status === "expired" || cred.status === "error") {
    return { label: "Reconnect →", tone: "rose" };
  }
  return { label: "Connect →", tone: "primary" };
}

export function BYOProviderCard({ provider, cred, onConnect, onDisconnect, disabled }: Props) {
  const meta = PROVIDER_META[provider];
  const cta = ctaLabel(cred);
  const isVerified = cred?.status === "verified";
  const isErrored = cred?.status === "error" || cred?.status === "expired";

  const handleClick = () => {
    if (disabled) return;
    if (isVerified) {
      onDisconnect();
    } else {
      onConnect();
    }
  };

  return (
    <article
      className={clsx(
        "rounded-panel border border-border bg-surface-elevated p-5 transition",
        disabled && "opacity-55",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span
            aria-hidden="true"
            className={clsx(
              "flex h-9 w-9 shrink-0 items-center justify-center rounded-full font-headline text-base",
              meta.glyphTone,
            )}
          >
            {meta.glyph}
          </span>
          <div className="min-w-0">
            <p className="font-headline text-base font-semibold text-ink">{meta.name}</p>
            <p className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">{meta.plan}</p>
          </div>
        </div>
        <StatusPill status={statusFor(cred)} size="sm" />
      </div>

      <p className="mt-4 text-sm text-ink-muted">{meta.body}</p>

      {isErrored && cred?.last_error ? (
        <p className="mt-3 rounded-lg border border-rose-border/50 bg-rose-bg/40 px-3 py-2 text-xs text-rose-text">
          {cred.last_error}
        </p>
      ) : null}

      <div className="mt-5 flex flex-col-reverse gap-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
          {meta.eyebrow}
        </p>
        {disabled ? (
          <span className="rounded-full border border-border bg-surface px-3 py-2 text-xs font-medium text-ink-faint">
            Coming soon
          </span>
        ) : (
          <button
            type="button"
            onClick={handleClick}
            disabled={cred?.status === "pending"}
            className={clsx(
              "rounded-full px-5 text-sm font-semibold transition-all min-h-[44px] min-w-[120px]",
              "disabled:cursor-not-allowed disabled:opacity-50",
              cta.tone === "primary" &&
                "glow-purple-hover bg-accent text-white hover:brightness-110 active:scale-[0.98]",
              cta.tone === "ghost" &&
                "border border-border bg-transparent text-ink-muted hover:bg-surface-hover hover:text-ink",
              cta.tone === "rose" &&
                "border border-rose-border bg-rose-bg/60 text-rose-text hover:bg-rose-bg",
            )}
          >
            {cta.label}
          </button>
        )}
      </div>
    </article>
  );
}
