"use client";

import Link from "next/link";
import {
  Component,
  type ReactNode,
  useState,
  useSyncExternalStore,
} from "react";

import { APP_STORE_URL } from "@/components/app-store-badge";
import { useWelcomeMessageQuery } from "@/lib/queries";

const DISMISS_KEY = "nbhd_welcome_card_dismissed_v1";

function subscribeToDismissal(callback: () => void): () => void {
  const handleStorage = (event: StorageEvent) => {
    if (event.key === DISMISS_KEY || event.key === null) callback();
  };
  window.addEventListener("storage", handleStorage);
  return () => window.removeEventListener("storage", handleStorage);
}

function getDismissedSnapshot(): boolean {
  try {
    return window.localStorage.getItem(DISMISS_KEY) !== null;
  } catch {
    return false;
  }
}

interface SilentBoundaryState {
  failed: boolean;
}

class SilentWelcomeBoundary extends Component<
  { children: ReactNode },
  SilentBoundaryState
> {
  state: SilentBoundaryState = { failed: false };

  static getDerivedStateFromError(): SilentBoundaryState {
    return { failed: true };
  }

  render() {
    return this.state.failed ? null : this.props.children;
  }
}

function WelcomeMessageCardContent() {
  const { data: greeting } = useWelcomeMessageQuery();
  const storedDismissal = useSyncExternalStore(
    subscribeToDismissal,
    getDismissedSnapshot,
    () => true,
  );
  const [dismissedThisVisit, setDismissedThisVisit] = useState(false);

  if (!greeting || storedDismissal || dismissedThisVisit) return null;

  const dismiss = () => {
    setDismissedThisVisit(true);
    try {
      window.localStorage.setItem(DISMISS_KEY, "1");
    } catch {
      // Storage can be unavailable in private/locked-down browsing. The local
      // state above still dismisses the card for this mounted visit.
    }
  };

  return (
    <section
      aria-labelledby="welcome-message-title"
      className="relative mx-3 mt-3 shrink-0 animate-reveal rounded-panel border border-accent/20 bg-card/95 p-4 pr-14 shadow-panel backdrop-blur-md sm:mx-4 sm:mt-4 sm:p-5 sm:pr-16 lg:mx-6 lg:mt-6"
    >
      <button
        type="button"
        onClick={dismiss}
        aria-label="Dismiss welcome message"
        className="absolute right-2 top-2 flex min-h-[44px] min-w-[44px] items-center justify-center rounded-full text-xl leading-none text-ink-faint transition hover:bg-surface-hover hover:text-ink motion-safe:active:scale-95"
      >
        <span aria-hidden="true">×</span>
      </button>

      <h2
        id="welcome-message-title"
        className="font-headline text-sm font-semibold text-ink"
      >
        ✳️ Your assistant left you a message
      </h2>
      <p className="mt-3 max-h-48 overflow-y-auto whitespace-pre-wrap break-words pr-2 text-sm leading-relaxed text-ink-muted">
        {greeting}
      </p>

      <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:flex-wrap">
        <a
          href={APP_STORE_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="glow-purple inline-flex min-h-[44px] items-center justify-center rounded-full bg-accent px-4 py-2 text-center text-sm font-semibold text-white transition hover:brightness-110 motion-safe:active:scale-[0.98]"
        >
          Open the iOS app
        </a>
        <Link
          href="/settings/integrations"
          className="inline-flex min-h-[44px] items-center justify-center rounded-full border border-border-strong px-4 py-2 text-center text-sm font-medium text-ink-muted transition hover:bg-surface-hover hover:text-ink motion-safe:active:scale-[0.98]"
        >
          Connect Telegram or LINE
        </Link>
      </div>
    </section>
  );
}

export function WelcomeMessageCard() {
  return (
    <SilentWelcomeBoundary>
      <WelcomeMessageCardContent />
    </SilentWelcomeBoundary>
  );
}
