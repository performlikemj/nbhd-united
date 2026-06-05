"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { Component, type ReactNode, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { isPlayEnabled } from "@/lib/constellation-game/flag";
import { useGalaxyQuery } from "@/lib/queries";

// Phaser is ~1MB — lazy-load the whole game so it never touches the main bundle.
// If the chunk stalls or 404s (common after a deploy when a tab is holding stale
// HTML), the loading screen below upgrades to a Reload affordance, and a hard
// failure is caught by <GameBoundary> — never a silent black screen.
const ConstellationGame = dynamic(
  () => import("@/components/constellation-game/constellation-game").then((m) => m.ConstellationGame),
  { ssr: false, loading: () => <ChartingScreen /> },
);

// How long a "still loading" state waits before it surfaces a way out. The
// animated loader keeps the screen alive throughout, so we can afford to wait
// out a cold-started backend before showing anything that reads as a problem.
const SLOW_MS = 20_000;

// ── shared full-bleed status UI ──────────────────────────────────────────────

function Screen({ children }: { children: ReactNode }) {
  return (
    <div className="fixed inset-0 z-[40] flex flex-col items-center justify-center gap-5 bg-bg px-6 text-center">
      {children}
    </div>
  );
}

function Headline({ children }: { children: ReactNode }) {
  return <p className="font-headline text-sm uppercase tracking-wider text-ink-muted">{children}</p>;
}

function Sub({ children }: { children: ReactNode }) {
  return <p className="max-w-xs text-[13px] leading-relaxed text-ink-faint">{children}</p>;
}

function RetryButton({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex min-h-[44px] items-center rounded-full bg-accent px-5 font-headline text-[13px] font-semibold text-white transition hover:brightness-110 focus-visible:ring-2 focus-visible:ring-accent/50"
    >
      {children}
    </button>
  );
}

/** Inline "back" affordance for the status screens (the fixed corner Exit sits
 *  behind the opaque Screen backdrop, so these states carry their own). */
function BackLink() {
  return (
    <Link
      href="/constellation"
      className="inline-flex min-h-[44px] items-center rounded-full border border-border bg-black/40 px-5 font-headline text-[13px] text-ink-muted backdrop-blur-xl transition hover:text-ink"
    >
      Back to constellation
    </Link>
  );
}

/** Fixed-corner exit shown over the live game. */
function ExitLink() {
  return (
    <Link
      href="/constellation"
      className="fixed right-4 top-[calc(env(safe-area-inset-top,0px)+12px)] z-[30] rounded-full border border-border bg-black/50 px-3.5 py-1.5 font-headline text-[11px] uppercase tracking-wider text-ink-muted backdrop-blur-xl hover:text-ink"
    >
      ✕ Exit
    </Link>
  );
}

function ActionRow({ children }: { children: ReactNode }) {
  return <div className="flex flex-wrap items-center justify-center gap-3">{children}</div>;
}

/** True once `ms` has elapsed since mount — used to upgrade a loading screen
 *  into a recoverable one only once it's clearly stuck, so the fast path never
 *  flashes retry UI. */
function useElapsed(ms: number): boolean {
  const [past, setPast] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setPast(true), ms);
    return () => clearTimeout(t);
  }, [ms]);
  return past;
}

/** A one-shot "warp in" reveal: an accent glow that dissolves to expose the galaxy. */
function WarpIn() {
  const [gone, setGone] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setGone(true), 50);
    return () => clearTimeout(t);
  }, []);
  return (
    <div
      aria-hidden
      className="pointer-events-none fixed inset-0 z-[50] transition-opacity duration-700 ease-out"
      style={{ background: "radial-gradient(circle at 50% 45%, rgba(124,107,240,0.45), var(--bg) 70%)", opacity: gone ? 0 : 1 }}
    />
  );
}

// ── loading states ───────────────────────────────────────────────────────────

const GALAXY_LOADER_CSS = `
.gl { position: relative; width: 132px; height: 132px; }
.gl-core { position:absolute; top:50%; left:50%; width:16px; height:16px; margin:-8px; border-radius:50%;
  background: radial-gradient(circle, #d8c9ff 0%, #7c6bf0 48%, rgba(124,107,240,0) 74%); }
.gl-ring { position:absolute; inset:6px; border-radius:50%; border:1px dashed rgba(124,107,240,0.30); }
.gl-arm { position:absolute; inset:0; }
.gl-ship { position:absolute; top:2px; left:50%; transform: translate(-50%,-50%);
  filter: drop-shadow(0 0 7px rgba(124,107,240,0.85)); }
.gl-star { position:absolute; width:2px; height:2px; border-radius:50%; background:#cdd9f5; opacity:.4; }
.gl-s1 { top:4%; left:80%; } .gl-s2 { top:86%; left:16%; } .gl-s3 { top:22%; left:6%; } .gl-s4 { top:72%; left:92%; }
@media (prefers-reduced-motion: no-preference) {
  .gl-arm { animation: gl-spin 3s linear infinite; }
  .gl-ring { animation: gl-spin 16s linear infinite reverse; }
  .gl-core { animation: gl-pulse 2.2s ease-in-out infinite; }
  .gl-star { animation: gl-tw 2.4s ease-in-out infinite; }
  .gl-s2 { animation-delay: .6s; } .gl-s3 { animation-delay: 1.1s; } .gl-s4 { animation-delay: 1.7s; }
}
@keyframes gl-spin { to { transform: rotate(360deg); } }
@keyframes gl-pulse { 0%,100% { opacity:.65; transform: scale(1); } 50% { opacity:1; transform: scale(1.25); } }
@keyframes gl-tw { 0%,100% { opacity:.2; } 50% { opacity:.75; } }
`;

/** On-brand loader: the player's ship flying its orbit around a glowing core, in
 *  a faint starfield. Keeps the screen alive while a cold backend wakes, so a long
 *  wait reads as motion, not a stall. Respects prefers-reduced-motion. */
function GalaxyLoader() {
  return (
    <div className="gl" aria-hidden>
      <div className="gl-core" />
      <div className="gl-ring" />
      <div className="gl-arm">
        <svg className="gl-ship" width="26" height="18" viewBox="0 0 34 24">
          <path d="M31 12 5 3 5 21Z" fill="#a5b4ff" />
          <path d="M31 12 9 7 9 17Z" fill="#7c6bf0" />
        </svg>
      </div>
      <span className="gl-star gl-s1" />
      <span className="gl-star gl-s2" />
      <span className="gl-star gl-s3" />
      <span className="gl-star gl-s4" />
      <style>{GALAXY_LOADER_CSS}</style>
    </div>
  );
}

/** The lazy Phaser chunk is loading. After SLOW_MS, offer a reload — a stalled
 *  or stale (404) chunk can't recover on its own. */
function ChartingScreen() {
  const stuck = useElapsed(SLOW_MS);
  return (
    <Screen>
      <GalaxyLoader />
      <Headline>Charting the galaxy…</Headline>
      {stuck && (
        <>
          <Sub>Taking longer than usual. If this tab was left open across an update, a reload usually sorts it out.</Sub>
          <ActionRow>
            <RetryButton onClick={() => window.location.reload()}>Reload</RetryButton>
            <BackLink />
          </ActionRow>
        </>
      )}
    </Screen>
  );
}

/** The galaxy data fetch is in flight. After SLOW_MS, offer a retry (covers a
 *  hung request the network never resolves, and a cold-started backend). */
function FetchingScreen({ onRetry }: { onRetry: () => void }) {
  const stuck = useElapsed(SLOW_MS);
  return (
    <Screen>
      <GalaxyLoader />
      <Headline>Charting the galaxy…</Headline>
      {stuck && (
        <>
          <Sub>Still charting — the first visit can take a moment while everything warms up. Hang tight, or give it a nudge.</Sub>
          <ActionRow>
            <RetryButton onClick={onRetry}>Try again</RetryButton>
            <BackLink />
          </ActionRow>
        </>
      )}
    </Screen>
  );
}

// ── lazy-game error boundary ─────────────────────────────────────────────────

/** Catches a failed chunk import or a mount-time failure (e.g. the dynamic
 *  import rejecting, or Phaser failing to init) and offers a reload, which
 *  re-fetches the chunk and clears a stale-cache state. Without this, those
 *  failures surface as a bare black screen. */
class GameBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(error: unknown) {
    console.error("Constellation game failed to start:", error);
  }
  render() {
    if (this.state.failed) {
      return (
        <Screen>
          <Headline>Couldn&apos;t start the game</Headline>
          <Sub>The galaxy didn&apos;t load — often a stale tab after an update. A reload usually fixes it.</Sub>
          <ActionRow>
            <RetryButton onClick={() => window.location.reload()}>Reload</RetryButton>
            <BackLink />
          </ActionRow>
        </Screen>
      );
    }
    return this.props.children;
  }
}

// ── error surfacing ──────────────────────────────────────────────────────────

/** Surfaces the first uncaught error / promise rejection so a silent failure —
 *  e.g. the Phaser scene throwing inside its animation loop, which a React error
 *  boundary can't catch — becomes a visible, reportable message instead of a
 *  black screen. */
function useCapturedError(): string | null {
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    const onError = (e: ErrorEvent) => setMsg((prev) => prev ?? (e.message || String(e.error) || "Unknown error"));
    const onReject = (e: PromiseRejectionEvent) =>
      setMsg((prev) => prev ?? (e.reason?.message || String(e.reason) || "Unhandled rejection"));
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onReject);
    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onReject);
    };
  }, []);
  return msg;
}

function ErrorBanner({ msg }: { msg: string }) {
  const [hidden, setHidden] = useState(false);
  if (hidden) return null;
  return (
    <div className="fixed inset-x-0 bottom-0 z-[60] border-t border-rose-border bg-rose-bg/95 px-4 py-3 backdrop-blur-xl">
      <p className="font-headline text-[11px] uppercase tracking-wider text-rose-text">Galaxy hit an error</p>
      <p className="mt-1 break-words font-mono text-[11px] leading-snug text-rose-text/90">{msg}</p>
      <div className="mt-2 flex items-center gap-3">
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="inline-flex min-h-[40px] items-center rounded-full bg-rose-text/15 px-4 font-headline text-[12px] text-rose-text transition hover:bg-rose-text/25"
        >
          Reload
        </button>
        <button type="button" onClick={() => setHidden(true)} className="font-headline text-[12px] text-rose-text/70 hover:text-rose-text">
          Dismiss
        </button>
      </div>
    </div>
  );
}

// ── page ─────────────────────────────────────────────────────────────────────

export default function ConstellationPlayPage() {
  const router = useRouter();
  const [allowed, setAllowed] = useState<boolean | null>(null);
  const bootError = useCapturedError();

  useEffect(() => {
    // Reading a client-only flag (env / localStorage) once after mount, then redirecting if
    // off — intentional and hydration-safe (matches the project pattern in journal/document-view).
    const ok = isPlayEnabled();
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setAllowed(ok);
    if (!ok) router.replace("/constellation");
  }, [router]);

  const { data, isLoading, isFetching, error, refetch } = useGalaxyQuery();

  // gating: render nothing while deciding / redirecting out
  if (allowed === null || !allowed) return null;

  let content: ReactNode;
  if (isLoading) {
    content = <FetchingScreen onRetry={() => void refetch()} />;
  } else if (error) {
    content = (
      <Screen>
        <Headline>Couldn&apos;t load your galaxy</Headline>
        <Sub>{isFetching ? "Retrying…" : "The server may be waking up, or your connection dropped."}</Sub>
        {!isFetching && (
          <ActionRow>
            <RetryButton onClick={() => void refetch()}>Try again</RetryButton>
            <BackLink />
          </ActionRow>
        )}
      </Screen>
    );
  } else if (!data || data.stars.length === 0) {
    content = (
      <Screen>
        <Headline>Your galaxy is still forming</Headline>
        <Sub>Approve a few lessons first, then come back and fly.</Sub>
        <ActionRow>
          <BackLink />
        </ActionRow>
      </Screen>
    );
  } else {
    content = (
      <GameBoundary>
        <ExitLink />
        <ConstellationGame galaxy={data} />
        <WarpIn />
      </GameBoundary>
    );
  }

  // The banner overlays whatever's showing — so an error thrown inside Phaser's
  // render loop (uncatchable by <GameBoundary>) still surfaces over the canvas.
  return (
    <>
      {content}
      {bootError && <ErrorBanner msg={bootError} />}
    </>
  );
}
