"use client";

import { AppStoreBadge } from "@/components/app-store-badge";

/**
 * Onboarding step 2 — point the new user at the iOS app. Replaces the old
 * messaging-channel connect step. There's no gate here: the user grabs the app
 * (or not) and taps Continue to finish provisioning in the launch scene.
 */
export function AppScene({ onContinue }: { onContinue: () => void }) {
  return (
    <div className="w-full max-w-[580px] flex flex-col items-center text-center">
      <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-[#5dd9d0] mb-4">
        STEP 2 OF 3
      </span>

      <h1 className="font-display text-3xl sm:text-5xl font-extrabold text-[#e0e3e8] tracking-tight mb-3 leading-tight">
        Get the NBHD app
      </h1>

      <p className="text-white/50 text-[15px] max-w-[420px] leading-relaxed mb-10">
        Your assistant lives in your pocket. Download the app for iPhone to talk
        by text or voice, share photos, and get daily check-ins wherever you are.
      </p>

      <AppStoreBadge height={56} />

      <button
        type="button"
        onClick={onContinue}
        className="mt-10 glow-purple rounded-full bg-[#7C6BF0] px-10 py-3.5 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-95"
      >
        Continue
      </button>

      <p className="mt-4 text-xs text-white/30">
        You can also keep going right here in your browser.
      </p>
    </div>
  );
}
