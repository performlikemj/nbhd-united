"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { OnboardingShell } from "@/components/onboarding/onboarding-shell";

/**
 * Promo redemption confirmation page.
 *
 * Reached via the 302 from `GET /api/v1/tenants/promos/redeem/` after
 * the backend has either applied the trial extension or determined
 * why it couldn't. The `?status=` query param drives which copy
 * variation renders — all five are listed below.
 *
 * Static export: Next.js requires `useSearchParams()` to be inside a
 * Suspense boundary, hence the wrapper component.
 */

type Status = "success" | "already" | "expired" | "invalid" | "active_subscription";

type Variant = {
  glyph: string;
  glyphColor: string;
  glyphBg: string;
  glyphBorder: string;
  glyphShadow: string;
  headline: string;
  body: React.ReactNode;
  cta: { label: string; href: string };
};

const VARIANTS: Record<Status, Variant> = {
  success: {
    glyph: "✦",
    glyphColor: "text-[#c7bfff]",
    glyphBg: "bg-[#7C6BF0]/20",
    glyphBorder: "border-[#7C6BF0]/30",
    glyphShadow: "shadow-[0_0_20px_rgba(124,107,240,0.3)]",
    headline: "You're back on, for 14 days.",
    body: (
      <>
        Your trial has been extended. The new end date is reflected on your
        dashboard. We&apos;re glad to have you here.
      </>
    ),
    cta: { label: "Open dashboard", href: "/dashboard" },
  },
  already: {
    glyph: "✧",
    glyphColor: "text-[#a8f0e8]",
    glyphBg: "bg-[#4ECDC4]/15",
    glyphBorder: "border-[#4ECDC4]/30",
    glyphShadow: "shadow-[0_0_18px_rgba(78,205,196,0.25)]",
    headline: "Already claimed.",
    body: (
      <>
        Looks like you&apos;ve already redeemed this one. Your trial is on the
        new end date — see your dashboard for details.
      </>
    ),
    cta: { label: "Open dashboard", href: "/dashboard" },
  },
  expired: {
    glyph: "·",
    glyphColor: "text-white/60",
    glyphBg: "bg-white/[0.04]",
    glyphBorder: "border-white/15",
    glyphShadow: "",
    headline: "This offer has closed.",
    body: (
      <>
        The promotional window ended on{" "}
        <span className="text-white/80">June&nbsp;6,&nbsp;2026</span>. Thanks
        for stopping by — if you&apos;d like to come back, sign-up is open.
      </>
    ),
    cta: { label: "Sign in", href: "/login" },
  },
  invalid: {
    glyph: "?",
    glyphColor: "text-[#e8b4b8]",
    glyphBg: "bg-[#e8b4b8]/12",
    glyphBorder: "border-[#e8b4b8]/30",
    glyphShadow: "",
    headline: "We couldn't read that link.",
    body: (
      <>
        The link may have been altered or expired. If you copied it from an
        email, try clicking through directly. Otherwise, reach out and
        we&apos;ll sort it out.
      </>
    ),
    cta: { label: "Sign in", href: "/login" },
  },
  active_subscription: {
    glyph: "★",
    glyphColor: "text-[#a8f0e8]",
    glyphBg: "bg-[#4ECDC4]/15",
    glyphBorder: "border-[#4ECDC4]/30",
    glyphShadow: "shadow-[0_0_18px_rgba(78,205,196,0.25)]",
    headline: "You're already covered.",
    body: (
      <>
        Your subscription is active — no trial to extend. Thanks for being
        with us.
      </>
    ),
    cta: { label: "Open dashboard", href: "/dashboard" },
  },
};

function PromoRedeemedInner() {
  const params = useSearchParams();
  const rawStatus = (params.get("status") || "").toLowerCase();
  const status: Status = (
    rawStatus in VARIANTS ? rawStatus : "invalid"
  ) as Status;
  const v = VARIANTS[status];

  return (
    <OnboardingShell>
      <div className="w-full max-w-[460px]">
        <div className="rounded-[24px] bg-[#12161b]/60 backdrop-blur-xl border border-white/[0.06] p-7 sm:p-9 shadow-[0_20px_60px_rgba(0,0,0,0.4)]">
          {/* Status glyph */}
          <div className="flex justify-center mb-7">
            <div
              className={`flex h-12 w-12 items-center justify-center rounded-full border ${v.glyphBorder} ${v.glyphBg} ${v.glyphShadow}`}
            >
              <span
                className={`font-serif text-2xl leading-none ${v.glyphColor}`}
              >
                {v.glyph}
              </span>
            </div>
          </div>

          {/* Headline — Instrument Serif italic to match Email 2 */}
          <h1 className="text-center font-serif italic text-[34px] sm:text-[40px] font-normal leading-[1.05] tracking-tight text-[#e0e3e8]">
            {v.headline}
          </h1>

          {/* Body */}
          <p className="mt-5 text-center text-[15px] leading-[1.65] text-white/70">
            {v.body}
          </p>

          {/* CTA */}
          <div className="mt-8 flex justify-center">
            <Link
              href={v.cta.href}
              className="glow-purple inline-flex min-h-[44px] items-center rounded-full bg-[#7C6BF0] px-6 py-3 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98]"
            >
              {v.cta.label}
            </Link>
          </div>
        </div>

        {/* Constellation accent — echoes Email 2 */}
        <div className="mt-6 flex justify-center">
          <p
            className="font-mono text-[10px] uppercase tracking-[0.28em] text-white/30"
            aria-hidden="true"
          >
            <span className="text-[#7C6BF0]/55">✦</span>
            {"  "}
            <span className="text-white/30">neighborhoodunited.org</span>
            {"  "}
            <span className="text-[#4ECDC4]/55">✧</span>
          </p>
        </div>
      </div>
    </OnboardingShell>
  );
}

export default function PromoRedeemedPage() {
  return (
    <Suspense
      fallback={
        <OnboardingShell>
          <div className="w-full max-w-[460px]">
            <div className="rounded-[24px] bg-[#12161b]/60 border border-white/[0.06] p-9 text-center text-white/60">
              Loading…
            </div>
          </div>
        </OnboardingShell>
      }
    >
      <PromoRedeemedInner />
    </Suspense>
  );
}
