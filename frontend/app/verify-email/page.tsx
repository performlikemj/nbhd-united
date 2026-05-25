"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { OnboardingShell } from "@/components/onboarding/onboarding-shell";
import { fetchMe, logout, requestEmailVerification } from "@/lib/api";
import { clearTokens, isLoggedIn } from "@/lib/auth";

type ResendState = "idle" | "sending" | "sent" | "rate-limited" | "error";

export default function VerifyEmailPage() {
  const router = useRouter();
  const [email, setEmail] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [resendState, setResendState] = useState<ResendState>("idle");
  const [checkingStatus, setCheckingStatus] = useState(false);

  // On mount: pull the logged-in user's email + redirect if already verified
  // so the back button doesn't trap a verified user on this page.
  useEffect(() => {
    if (!isLoggedIn()) {
      router.replace("/signup");
      return;
    }
    fetchMe()
      .then((me) => {
        if (me.email_verified) {
          router.replace("/onboarding");
          return;
        }
        setEmail(me.email);
      })
      .catch(() => {
        // Token bad or backend down — back to login is the safest fallback.
        router.replace("/login");
      })
      .finally(() => setLoading(false));
  }, [router]);

  const handleResend = async () => {
    setResendState("sending");
    try {
      await requestEmailVerification();
      setResendState("sent");
    } catch (err) {
      if (err instanceof Error && err.message.includes("429")) {
        setResendState("rate-limited");
      } else {
        setResendState("error");
      }
    }
  };

  const handleCheckStatus = async () => {
    setCheckingStatus(true);
    try {
      const me = await fetchMe();
      if (me.email_verified) {
        router.replace("/onboarding");
      } else {
        // Stay on the page; tell the user we still can't see verification.
        setResendState("idle");
      }
    } finally {
      setCheckingStatus(false);
    }
  };

  const handleUseDifferentEmail = async () => {
    // Best-effort logout — even if it fails we still clear tokens locally.
    try {
      await logout();
    } catch {
      /* noop */
    }
    clearTokens();
    router.replace("/signup");
  };

  if (loading) {
    return (
      <OnboardingShell>
        <div className="w-full max-w-[420px] text-center text-sm text-white/45">
          Loading…
        </div>
      </OnboardingShell>
    );
  }

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

          <h2 className="text-center text-2xl font-bold text-[#e0e3e8] tracking-tight">
            Check your inbox
          </h2>
          <p className="mt-2 text-center text-sm text-white/45 leading-relaxed">
            We sent a verification link to{" "}
            <span className="font-mono text-white/75 break-all">{email}</span>.
            Click it to finish creating your account. The link expires in 3 days.
          </p>

          {resendState === "sent" && (
            <div
              role="status"
              className="mt-6 rounded-xl border border-[#5dd9d0]/30 bg-[#5dd9d0]/10 px-4 py-2.5 text-center text-sm text-[#5dd9d0]"
            >
              Sent a fresh link — give it a minute to arrive.
            </div>
          )}
          {resendState === "rate-limited" && (
            <div
              role="alert"
              className="mt-6 rounded-xl border border-amber-border bg-amber-bg px-4 py-2.5 text-center text-sm text-amber-text"
            >
              Too many resends — try again in an hour.
            </div>
          )}
          {resendState === "error" && (
            <div
              role="alert"
              className="mt-6 rounded-xl border border-rose-border bg-rose-bg px-4 py-2.5 text-center text-sm text-rose-text"
            >
              Couldn&apos;t send right now — please try again in a moment.
            </div>
          )}

          <div className="mt-7 space-y-3">
            <button
              type="button"
              onClick={handleCheckStatus}
              disabled={checkingStatus}
              className="glow-purple w-full rounded-full bg-[#7C6BF0] px-4 py-3 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
            >
              {checkingStatus ? "Checking…" : "I verified my email"}
            </button>

            <button
              type="button"
              onClick={handleResend}
              disabled={resendState === "sending" || resendState === "sent"}
              className="w-full rounded-full border border-white/10 bg-white/[0.04] px-4 py-3 text-sm font-medium text-white/75 transition-all hover:bg-white/[0.08] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
            >
              {resendState === "sending" ? "Sending…" : "Resend verification email"}
            </button>
          </div>

          <p className="mt-7 text-center text-xs text-white/40">
            Wrong email?{" "}
            <button
              type="button"
              onClick={handleUseDifferentEmail}
              className="text-white/60 underline hover:text-white/80"
            >
              Use a different one
            </button>
          </p>
        </div>

        <p className="mt-6 text-center text-sm text-white/40">
          Need help?{" "}
          <Link href="/login" className="text-white/60 underline hover:text-white/80">
            Back to sign in
          </Link>
        </p>
      </div>
    </OnboardingShell>
  );
}
