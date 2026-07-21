import type { Metadata } from "next";
import Link from "next/link";

import { Starfield } from "@/components/landing/starfield";
import { ConstellationLines } from "@/components/landing/constellation-lines";
import { SynapseNetwork } from "@/components/landing/synapse-network";
import { SiteFooter } from "@/components/site-footer";

const STRIPE_BUY_URL = "https://buy.stripe.com/cNi28sfe41JxcqWfVR4F200";
const DMG_URL = "https://performlikemj.github.io/yardtalk/";

export const metadata: Metadata = {
  title: "YardTalk — work session recorder for Mac",
  description:
    "YardTalk is a macOS menu-bar app that records narrated work sessions, transcribes them locally, and turns them into end-of-session reports that feed your nbhd assistant.",
  openGraph: {
    title: "YardTalk — work session recorder for Mac",
    description:
      "Narrate your work sessions. YardTalk transcribes them on your Mac and hands the story to your nbhd assistant.",
    images: [{ url: "/yardtalk-icon.png", width: 512, height: 512 }],
  },
};

type Feature = {
  title: string;
  description: string;
  color: "c-purple" | "c-teal" | "c-pink";
  icon: React.ReactNode;
};

const features: Feature[] = [
  {
    title: "Record as you work",
    description:
      "A menu-bar button starts a session. Narrate what you’re doing out loud — no setup, no timers to babysit.",
    color: "c-purple",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" className="h-7 w-7" aria-hidden="true">
        <rect x="9" y="2" width="6" height="12" rx="3" stroke="currentColor" strokeWidth="1.5" />
        <path
          d="M5 11a7 7 0 0 0 14 0M12 18v3M8.5 21h7"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    ),
  },
  {
    title: "Transcribed on your Mac",
    description:
      "Speech becomes text locally. Your audio never leaves the machine — nothing is uploaded to transcribe it.",
    color: "c-teal",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" className="h-7 w-7" aria-hidden="true">
        <path
          d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3Z"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinejoin="round"
        />
        <path
          d="M9.2 12l2 2 3.6-3.8"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    ),
  },
  {
    title: "End-of-session reports",
    description:
      "Close a session and YardTalk writes it up — what you did, what you decided, what’s still open.",
    color: "c-pink",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" className="h-7 w-7" aria-hidden="true">
        <path
          d="M6 3h8l4 4v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinejoin="round"
        />
        <path
          d="M14 3v4h4M8.5 12h7M8.5 15.5h7M8.5 19h4"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    ),
  },
  {
    title: "Flows into your assistant",
    description:
      "Each report lands in your nbhd assistant’s memory, so it already knows what you worked on when you check in.",
    color: "c-purple",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" className="h-7 w-7" aria-hidden="true">
        <circle cx="12" cy="12" r="2" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="5" cy="6" r="1.5" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="19" cy="6" r="1.5" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="19" cy="18" r="1.5" stroke="currentColor" strokeWidth="1.5" />
        <path
          d="M6.4 6.9 10.3 11M13.7 11 17.6 6.9M13.6 13.1 17.6 17.2"
          stroke="currentColor"
          strokeWidth="1"
          strokeLinecap="round"
        />
      </svg>
    ),
  },
];

const colorMap: Record<Feature["color"], { bg: string; text: string }> = {
  "c-purple": { bg: "bg-c-purple/10", text: "text-c-purple" },
  "c-teal": { bg: "bg-c-teal/10", text: "text-c-teal" },
  "c-pink": { bg: "bg-c-pink/10", text: "text-c-pink" },
};

const paidPerks = [
  "License key delivered by email",
  "Works on up to 3 Macs",
  "14-day free trial before you pay",
  "Free updates",
];

const subscriberPerks = [
  "No license key to manage",
  "Link the app to your nbhd account",
  "Included while your subscription is active",
  "Trial subscriptions excluded",
];

export default function YardTalkPage() {
  return (
    <div className="landing-dark flex min-h-screen flex-col">
      {/* ── Hero ── */}
      <section className="constellation-bg relative flex min-h-screen flex-col items-center justify-center overflow-hidden px-6 py-24">
        <Starfield />
        <ConstellationLines />
        <SynapseNetwork className="opacity-[0.12]" />

        <div className="relative z-10 mx-auto max-w-3xl space-y-8 text-center">
          <div className="animate-reveal-1 flex justify-center">
            <div className="glow-purple rounded-3xl">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src="/yardtalk-icon.png"
                alt="YardTalk app icon"
                width={112}
                height={112}
                className="h-24 w-24 rounded-3xl sm:h-28 sm:w-28"
              />
            </div>
          </div>

          <div className="animate-reveal-2 space-y-4">
            <span className="block text-xs font-semibold uppercase tracking-[0.3em] text-c-teal">
              For macOS
            </span>
            <h1 className="font-headline text-[clamp(2.5rem,5vw+0.5rem,4.5rem)] font-bold leading-tight tracking-tight text-c-text">
              YardTalk
            </h1>
          </div>

          <p className="animate-reveal-3 mx-auto max-w-2xl text-lg font-light leading-relaxed text-c-text-muted md:text-xl">
            Narrate your work sessions out loud. YardTalk records them,
            transcribes them right on your Mac, and turns each one into a report
            your nbhd assistant remembers.
          </p>

          <div className="animate-reveal-4 flex flex-col items-center justify-center gap-4 pt-2 sm:flex-row">
            <a
              href={STRIPE_BUY_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="glow-purple glow-purple-hover inline-flex min-h-[44px] items-center rounded-lg bg-c-purple px-8 py-4 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-95"
            >
              Buy &mdash; $20
            </a>
            <a
              href="#pricing"
              className="inline-flex min-h-[44px] items-center rounded-lg border border-white/20 bg-transparent px-8 py-4 text-sm font-semibold text-c-text transition-all hover:bg-white/5 active:scale-95"
            >
              Free with nbhd
            </a>
          </div>

          <p className="animate-reveal-4 text-xs text-c-text-faint">
            14-day full trial &middot; no account required to start
          </p>
        </div>
      </section>

      {/* ── What it does ── */}
      <section className="mx-auto w-full max-w-7xl px-6 py-24">
        <div className="mx-auto mb-14 max-w-2xl text-center">
          <span className="mb-4 block text-xs font-semibold uppercase tracking-[0.3em] text-c-teal">
            What it does
          </span>
          <h2 className="font-headline text-[clamp(2rem,4vw+0.5rem,3.25rem)] font-bold tracking-tight text-c-text">
            Talk through the work. Keep the story.
          </h2>
        </div>

        <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-4">
          {features.map((feature) => {
            const colors = colorMap[feature.color];
            return (
              <div
                key={feature.title}
                className="glass-card flex flex-col gap-5 rounded-xl p-6 transition-transform duration-500 hover:-translate-y-1"
              >
                <div
                  className={`flex h-12 w-12 items-center justify-center rounded-full ${colors.bg} ${colors.text}`}
                >
                  {feature.icon}
                </div>
                <div>
                  <h3 className="font-headline mb-2 text-lg font-semibold text-c-text">
                    {feature.title}
                  </h3>
                  <p className="text-sm leading-relaxed text-c-text-muted">
                    {feature.description}
                  </p>
                </div>
              </div>
            );
          })}
        </div>
      </section>

      {/* ── Pricing ── */}
      <section id="pricing" className="relative overflow-hidden bg-black/40 px-6 py-28">
        <SynapseNetwork className="opacity-[0.06]" />
        <div className="relative z-10 mx-auto max-w-4xl">
          <div className="mx-auto mb-14 max-w-2xl text-center">
            <span className="mb-4 block text-xs font-semibold uppercase tracking-[0.3em] text-c-pink">
              Two ways to get it
            </span>
            <h2 className="font-headline text-[clamp(2rem,4vw+0.5rem,3.25rem)] font-bold tracking-tight text-c-text">
              Buy it once, or get it free
            </h2>
          </div>

          <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
            {/* $20 one-time */}
            <div className="glass-card flex flex-col rounded-2xl border-c-purple/30 p-8">
              <span className="text-xs font-semibold uppercase tracking-[0.2em] text-c-purple">
                One-time purchase
              </span>
              <div className="mt-4 flex items-baseline gap-2">
                <span className="font-headline text-5xl font-bold text-c-text">$20</span>
                <span className="text-sm text-c-text-faint">once</span>
              </div>
              <p className="mt-3 text-sm leading-relaxed text-c-text-muted">
                Own it outright. Start with a 14-day free trial — no card needed
                to try it.
              </p>
              <ul className="mt-6 flex-1 space-y-3">
                {paidPerks.map((perk) => (
                  <li key={perk} className="flex items-start gap-3 text-sm text-c-text-muted">
                    <svg
                      viewBox="0 0 24 24"
                      fill="none"
                      className="mt-0.5 h-4 w-4 flex-shrink-0 text-c-purple"
                      aria-hidden="true"
                    >
                      <path
                        d="M5 12.5 10 17l9-10"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                    <span>{perk}</span>
                  </li>
                ))}
              </ul>
              <a
                href={STRIPE_BUY_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="glow-purple glow-purple-hover mt-8 inline-flex min-h-[44px] items-center justify-center rounded-lg bg-c-purple px-6 py-3.5 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-95"
              >
                Buy a license &mdash; $20
              </a>
            </div>

            {/* Free with nbhd */}
            <div className="glass-card flex flex-col rounded-2xl border-c-teal/30 p-8">
              <span className="text-xs font-semibold uppercase tracking-[0.2em] text-c-teal">
                Free with nbhd
              </span>
              <div className="mt-4 flex items-baseline gap-2">
                <span className="font-headline text-5xl font-bold text-c-text">$0</span>
                <span className="text-sm text-c-text-faint">with a paid plan</span>
              </div>
              <p className="mt-3 text-sm leading-relaxed text-c-text-muted">
                Already subscribed to nbhd? YardTalk is included. Link the app to
                your account and skip the license key.
              </p>
              <ul className="mt-6 flex-1 space-y-3">
                {subscriberPerks.map((perk) => (
                  <li key={perk} className="flex items-start gap-3 text-sm text-c-text-muted">
                    <svg
                      viewBox="0 0 24 24"
                      fill="none"
                      className="mt-0.5 h-4 w-4 flex-shrink-0 text-c-teal"
                      aria-hidden="true"
                    >
                      <path
                        d="M5 12.5 10 17l9-10"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                    <span>{perk}</span>
                  </li>
                ))}
              </ul>
              <Link
                href="/signup"
                className="mt-8 inline-flex min-h-[44px] items-center justify-center rounded-lg border border-white/20 bg-transparent px-6 py-3.5 text-sm font-semibold text-c-text transition-all hover:bg-white/5 active:scale-95"
              >
                Get an nbhd subscription
              </Link>
            </div>
          </div>
        </div>
      </section>

      {/* ── Download ── */}
      <section className="px-6 py-28 text-center">
        <div className="mx-auto max-w-2xl space-y-6">
          <h2 className="font-headline text-[clamp(2rem,4vw+0.5rem,3.25rem)] font-bold tracking-tight text-c-text">
            Try it on your Mac today.
          </h2>
          <p className="text-base leading-relaxed text-c-text-muted">
            Download the notarized app and start a full 14-day trial. No account
            required &mdash; buy a license or link your nbhd subscription whenever
            you&rsquo;re ready.
          </p>
          <div className="flex flex-col items-center gap-4 pt-2">
            <a
              href={DMG_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="glow-purple glow-purple-hover inline-flex min-h-[44px] items-center gap-2.5 rounded-lg bg-c-purple px-8 py-4 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-95"
            >
              <svg viewBox="0 0 24 24" fill="none" className="h-5 w-5" aria-hidden="true">
                <path
                  d="M12 3v12m0 0 4-4m-4 4-4-4M5 19h14"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              Download for macOS
            </a>
            <span className="text-xs text-c-text-faint">
              14-day full trial &middot; no account required
            </span>
          </div>
        </div>
      </section>

      <SiteFooter />
    </div>
  );
}
