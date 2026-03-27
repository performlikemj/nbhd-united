"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { isLoggedIn } from "@/lib/auth";
import { Starfield } from "@/components/landing/starfield";
import { ConstellationLines } from "@/components/landing/constellation-lines";
import { SynapseNetwork } from "@/components/landing/synapse-network";

const steps = [
  {
    title: "Talk naturally",
    description:
      "Daily check-ins, voice notes, stream of consciousness. Just talk\u00a0\u2014 your assistant is always listening.",
    color: "c-purple",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" className="h-7 w-7" aria-hidden="true">
        <path
          d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5Z"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    ),
  },
  {
    title: "Patterns emerge",
    description:
      "Your assistant extracts lessons, tracks goals, and connects the dots you can\u2019t see yet.",
    color: "c-teal",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" className="h-7 w-7" aria-hidden="true">
        <circle cx="12" cy="12" r="2" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="5" cy="7" r="1.5" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="19" cy="7" r="1.5" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="5" cy="17" r="1.5" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="19" cy="17" r="1.5" stroke="currentColor" strokeWidth="1.5" />
        <path d="M6.5 8L10.5 11M13.5 11L17.5 8M6.5 16L10.5 13M13.5 13L17.5 16" stroke="currentColor" strokeWidth="1" />
      </svg>
    ),
  },
  {
    title: "See your constellation",
    description:
      "A living visual map of who you\u2019re becoming. Your growth, your connections, your universe.",
    color: "c-pink",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" className="h-7 w-7" aria-hidden="true">
        <path
          d="M12 2L13.09 8.26L18 4L14.74 9.91L21 10L14.74 12.09L18 18L13.09 13.74L12 20L10.91 13.74L6 18L9.26 12.09L3 10L9.26 9.91L6 4L10.91 8.26L12 2Z"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinejoin="round"
        />
      </svg>
    ),
  },
];

const colorMap: Record<string, { bg: string; text: string; hoverBg: string }> = {
  "c-purple": {
    bg: "bg-c-purple/10",
    text: "text-c-purple",
    hoverBg: "group-hover:bg-c-purple/20",
  },
  "c-teal": {
    bg: "bg-c-teal/10",
    text: "text-c-teal",
    hoverBg: "group-hover:bg-c-teal/20",
  },
  "c-pink": {
    bg: "bg-c-pink/10",
    text: "text-c-pink",
    hoverBg: "group-hover:bg-c-pink/20",
  },
};

export default function LandingPage() {
  const router = useRouter();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (isLoggedIn()) {
      router.replace("/journal");
    } else {
      setReady(true);
    }
  }, [router]);

  if (!ready) return null;

  return (
    <div className="landing-dark flex min-h-screen flex-col">
      {/* ── Hero ── */}
      <section className="constellation-bg relative flex min-h-screen flex-col items-center justify-center overflow-hidden px-6 pt-20">
        <Starfield />
        <ConstellationLines />
        <SynapseNetwork className="opacity-[0.12]" />

        <div className="relative z-10 max-w-4xl space-y-8 text-center">
          {/* Logo mark */}
          <div className="animate-reveal-1 flex justify-center">
            <div className="glow-purple flex h-12 w-12 items-center justify-center rounded-full border border-c-purple/30 bg-c-purple/20">
              <svg viewBox="0 0 24 24" fill="none" className="h-6 w-6 text-c-purple" aria-hidden="true">
                <path
                  d="M12 2L13.09 8.26L18 4L14.74 9.91L21 10L14.74 12.09L18 18L13.09 13.74L12 20L10.91 13.74L6 18L9.26 12.09L3 10L9.26 9.91L6 4L10.91 8.26L12 2Z"
                  fill="currentColor"
                />
              </svg>
            </div>
          </div>

          <h1 className="animate-reveal-2 font-headline text-[clamp(2.5rem,5vw+0.5rem,4.5rem)] font-bold leading-tight tracking-tight text-c-text">
            Explore the universe{" "}
            <br />
            <span className="bg-gradient-to-r from-c-purple via-c-pink to-c-teal bg-clip-text text-transparent">
              inside you
            </span>
          </h1>

          <p className="animate-reveal-3 mx-auto max-w-2xl text-lg font-light leading-relaxed text-c-text-muted md:text-xl">
            A private AI companion that listens, learns, and helps you
            grow&nbsp;&mdash; through natural conversation on Telegram and LINE.
          </p>

          <div className="animate-reveal-4 flex flex-col items-center justify-center gap-4 pt-4 sm:flex-row">
            <Link
              href="/signup"
              className="glow-purple glow-purple-hover inline-flex min-h-[44px] items-center rounded-lg bg-c-purple px-8 py-4 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-95"
            >
              Begin your journey
            </Link>
            <Link
              href="/login"
              className="inline-flex min-h-[44px] items-center rounded-lg border border-white/20 bg-transparent px-8 py-4 text-sm font-semibold text-c-text transition-all hover:bg-white/5 active:scale-95"
            >
              Sign in
            </Link>
          </div>
        </div>

        {/* Scroll indicator */}
        <div className="absolute bottom-10 left-1/2 flex -translate-x-1/2 flex-col items-center gap-2 opacity-40">
          <span className="text-[10px] uppercase tracking-[0.25em]">
            Scroll to explore
          </span>
          <div className="h-12 w-px bg-gradient-to-b from-white to-transparent" />
        </div>
      </section>

      {/* ── How It Works ── */}
      <section className="mx-auto w-full max-w-7xl px-6 py-24">
        <div className="grid grid-cols-1 gap-8 md:grid-cols-3">
          {steps.map((step) => {
            const colors = colorMap[step.color];
            return (
              <div
                key={step.title}
                className="glass-card group flex flex-col gap-6 rounded-xl p-8 transition-transform duration-500 hover:-translate-y-1"
              >
                <div
                  className={`flex h-14 w-14 items-center justify-center rounded-full transition-colors ${colors.bg} ${colors.text} ${colors.hoverBg}`}
                >
                  {step.icon}
                </div>
                <div>
                  <h3 className="font-headline mb-3 text-xl font-semibold text-c-text">
                    {step.title}
                  </h3>
                  <p className="leading-relaxed text-c-text-muted">
                    {step.description}
                  </p>
                </div>
              </div>
            );
          })}
        </div>
      </section>

      {/* ── The Vision ── */}
      <section className="relative overflow-hidden bg-black/40 px-6 py-32">
        <Starfield className="opacity-40" />
        <SynapseNetwork className="opacity-[0.08] scale-x-[-1]" />
        <div className="relative z-10 mx-auto max-w-4xl text-center">
          <span className="mb-8 block text-xs font-semibold uppercase tracking-[0.3em] text-c-teal">
            The Vision
          </span>
          <blockquote className="font-serif text-[clamp(1.5rem,3vw+0.5rem,3rem)] italic leading-snug text-slate-200">
            &ldquo;There are as many neurons in your brain as stars in the Milky
            Way. We carry a universe inside us.{" "}
            <span className="text-c-pink">Neighborhood United</span> helps you
            explore yours.&rdquo;
          </blockquote>
          <div className="mt-16 flex items-center justify-center gap-6 opacity-30">
            <div className="h-px w-24 bg-gradient-to-r from-transparent to-slate-400" />
            <svg viewBox="0 0 24 24" className="h-5 w-5 text-slate-400" fill="currentColor" aria-hidden="true">
              <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2Zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9Z" opacity="0" />
              <path d="M15.5 2.5L12 8l-3.5-5.5L12 5.5l3.5-3ZM22 12l-5.5-3.5L22 12l-5.5 3.5L22 12ZM2 12l5.5-3.5L2 12l5.5 3.5L2 12ZM12 22l3.5-5.5L12 22l-3.5-5.5L12 22ZM8.5 2.5L12 8l3.5-5.5" stroke="currentColor" strokeWidth="1" fill="none" />
            </svg>
            <div className="h-px w-24 bg-gradient-to-l from-transparent to-slate-400" />
          </div>
        </div>
      </section>

      {/* ── Final CTA ── */}
      <section className="px-6 py-32 text-center">
        <div className="mx-auto max-w-2xl space-y-10">
          <h2 className="font-headline text-[clamp(2rem,4vw+0.5rem,3.25rem)] font-bold tracking-tight text-c-text">
            Your constellation is waiting.
          </h2>
          <div className="flex justify-center">
            <Link
              href="/signup"
              className="glow-purple group relative inline-flex min-h-[44px] items-center overflow-hidden rounded-lg bg-c-purple px-12 py-5 text-lg font-bold text-white transition-all duration-300 hover:scale-105"
            >
              <span className="relative z-10">Get started</span>
              <div className="absolute inset-0 translate-y-full bg-white/10 transition-transform duration-300 group-hover:translate-y-0" />
            </Link>
          </div>
        </div>
      </section>
    </div>
  );
}
