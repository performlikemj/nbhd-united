"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { OnboardingShell } from "@/components/onboarding/onboarding-shell";
import { confirmEmailVerification } from "@/lib/api";
import { isLoggedIn } from "@/lib/auth";

type Status = "checking" | "success" | "expired" | "missing";

function VerifyConfirmInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const uid = searchParams.get("uid") ?? "";
  const token = searchParams.get("token") ?? "";

  // Derive the initial state from URL params synchronously so we don't
  // trigger an extra render via setState-in-effect.
  const [statusState, setStatusState] = useState<Status>(
    uid && token ? "checking" : "missing",
  );

  useEffect(() => {
    if (!uid || !token) return;
    confirmEmailVerification(uid, token)
      .then(() => {
        setStatusState("success");
        // Brief pause so the user sees the success state, then route by
        // session: continue onboarding if logged in here, otherwise send
        // them to login (they clicked the link on a different device).
        const target = isLoggedIn() ? "/onboarding" : "/login?verified=1";
        setTimeout(() => router.replace(target), 1200);
      })
      .catch(() => {
        setStatusState("expired");
      });
  }, [uid, token, router]);

  return (
    <OnboardingShell>
      <div className="w-full max-w-[420px]">
        <div className="rounded-[24px] bg-[#12161b]/60 backdrop-blur-xl border border-white/[0.06] p-7 sm:p-8 shadow-[0_20px_60px_rgba(0,0,0,0.4)]">
          <div className="flex justify-center mb-6">
            <div
              className="flex h-10 w-10 items-center justify-center rounded-full border border-[#7C6BF0]/30 bg-[#7C6BF0]/20 shadow-[0_0_20px_rgba(124,107,240,0.3)]"
              aria-hidden="true"
            >
              <svg viewBox="0 0 24 24" fill="none" className="h-5 w-5 text-[#c7bfff]">
                <path
                  d="M12 2L13.09 8.26L18 4L14.74 9.91L21 10L14.74 12.09L18 18L13.09 13.74L12 20L10.91 13.74L6 18L9.26 12.09L3 10L9.26 9.91L6 4L10.91 8.26L12 2Z"
                  fill="currentColor"
                />
              </svg>
            </div>
          </div>

          {statusState === "checking" && (
            <>
              <h2 className="text-center text-2xl font-bold text-[#e0e3e8] tracking-tight">
                Verifying…
              </h2>
              <p className="mt-2 text-center text-sm text-white/45">
                One moment while we confirm your email.
              </p>
            </>
          )}

          {statusState === "success" && (
            <>
              <h2 className="text-center text-2xl font-bold text-[#e0e3e8] tracking-tight">
                Email verified
              </h2>
              <p className="mt-2 text-center text-sm text-white/45">
                Welcome aboard. Taking you to the next step…
              </p>
            </>
          )}

          {statusState === "missing" && (
            <>
              <h2 className="text-center text-2xl font-bold text-[#e0e3e8] tracking-tight">
                Link incomplete
              </h2>
              <p className="mt-2 text-center text-sm text-white/45">
                This verification link is missing required information. Head
                back and request a fresh one.
              </p>
              <div className="mt-7">
                <Link
                  href="/verify-email"
                  className="glow-purple block w-full rounded-full bg-[#7C6BF0] px-4 py-3 text-center text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98] min-h-[44px] leading-[1.4]"
                >
                  Resend verification
                </Link>
              </div>
            </>
          )}

          {statusState === "expired" && (
            <>
              <h2 className="text-center text-2xl font-bold text-[#e0e3e8] tracking-tight">
                Link expired
              </h2>
              <p className="mt-2 text-center text-sm text-white/45">
                This verification link is invalid or has expired. Request a
                fresh one and try again.
              </p>
              <div className="mt-7">
                <Link
                  href="/verify-email"
                  className="glow-purple block w-full rounded-full bg-[#7C6BF0] px-4 py-3 text-center text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98] min-h-[44px] leading-[1.4]"
                >
                  Resend verification
                </Link>
              </div>
            </>
          )}
        </div>
      </div>
    </OnboardingShell>
  );
}

export default function VerifyEmailConfirmPage() {
  return (
    <Suspense
      fallback={
        <OnboardingShell>
          <div className="w-full max-w-[420px] text-center text-sm text-white/45">
            Loading…
          </div>
        </OnboardingShell>
      }
    >
      <VerifyConfirmInner />
    </Suspense>
  );
}
