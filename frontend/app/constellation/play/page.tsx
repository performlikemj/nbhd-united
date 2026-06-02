"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { isPlayEnabled } from "@/lib/constellation-game/flag";
import { useGalaxyQuery } from "@/lib/queries";

// Phaser is ~1MB — lazy-load the whole game so it never touches the main bundle.
const ConstellationGame = dynamic(
  () => import("@/components/constellation-game/constellation-game").then((m) => m.ConstellationGame),
  { ssr: false, loading: () => <Centered>Charting the galaxy…</Centered> },
);

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 flex items-center justify-center bg-bg px-6 text-center">
      <p className="font-headline text-sm uppercase tracking-wider text-ink-muted">{children}</p>
    </div>
  );
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

export default function ConstellationPlayPage() {
  const router = useRouter();
  const [allowed, setAllowed] = useState<boolean | null>(null);

  useEffect(() => {
    // Reading a client-only flag (env / localStorage) once after mount, then redirecting if
    // off — intentional and hydration-safe (matches the project pattern in journal/document-view).
    const ok = isPlayEnabled();
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setAllowed(ok);
    if (!ok) router.replace("/constellation");
  }, [router]);

  const { data, isLoading, error } = useGalaxyQuery();

  // gating: render nothing while deciding / redirecting out
  if (allowed === null || !allowed) return null;

  if (isLoading) return <Centered>Charting the galaxy…</Centered>;

  if (error) {
    return (
      <>
        <ExitLink />
        <Centered>Couldn&apos;t load your galaxy. Try again from the constellation.</Centered>
      </>
    );
  }

  if (!data || data.stars.length === 0) {
    return (
      <>
        <ExitLink />
        <Centered>Your galaxy is still forming — approve a few lessons first, then come fly.</Centered>
      </>
    );
  }

  return (
    <>
      <ExitLink />
      <ConstellationGame galaxy={data} />
      <WarpIn />
    </>
  );
}
