"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { FormEvent, Suspense, useEffect, useState } from "react";

import {
  AppleSignInButton,
  type AppleAuthenticationResult,
} from "@/components/apple-sign-in-button";
import { fetchMe, signup } from "@/lib/api";
import {
  completeAuthentication,
  getAccessToken,
  getAuthenticationEpoch,
} from "@/lib/auth";
import { hasPendingAppAuthorize } from "@/lib/app-authorize";
import { stashInviteToken } from "@/lib/invite-token";
import { decidePostAuthRoute } from "@/lib/post-auth-route";
import { OnboardingShell } from "@/components/onboarding/onboarding-shell";
import { PasswordStrengthMeter } from "@/components/onboarding/password-strength-meter";

function SignupPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const fromApp = searchParams.get("from") === "app";

  // Neighborhood invite handoff: stash `?invite=<token>` now — tenant
  // provisioning (and the invite claim) happens later, in PersonaScene
  // during /onboarding, by which point the query param is gone.
  useEffect(() => {
    const invite = searchParams.get("invite");
    if (invite) stashInviteToken(invite);
  }, [searchParams]);

  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [appleBusy, setAppleBusy] = useState(false);
  const [returnToApp, setReturnToApp] = useState<{
    created: boolean;
    email: string;
  } | null>(null);

  const finishWebRouting = async (created: boolean) => {
    if (created) {
      const destination = decidePostAuthRoute({
        hasPendingHandoff: false,
        fromApp: false,
        created,
        needsOnboarding: false,
      });
      router.push(destination === "journal" ? "/journal" : "/onboarding");
      return;
    }

    let needsOnboarding = true;
    try {
      const me = await fetchMe();
      needsOnboarding = !me.tenant || me.tenant.status !== "active";
    } catch {
      // Preserve the existing safe fallback to onboarding.
    }
    const destination = decidePostAuthRoute({
      hasPendingHandoff: false,
      fromApp: false,
      created,
      needsOnboarding,
    });
    router.push(destination === "journal" ? "/journal" : "/onboarding");
  };

  const finishAuthentication = async (
    result: AppleAuthenticationResult,
    attemptEpoch: number,
    attemptAccessToken: string | null,
  ) => {
    if (
      getAccessToken() !== attemptAccessToken ||
      getAuthenticationEpoch() !== attemptEpoch
    ) {
      return;
    }
    completeAuthentication(result);
    const created = Boolean(result.created);
    const destination = decidePostAuthRoute({
      hasPendingHandoff: hasPendingAppAuthorize(),
      fromApp,
      created,
      needsOnboarding: false,
    });
    if (destination === "app-authorize") {
      router.replace("/app/authorize");
      return;
    }
    if (destination === "return-to-app") {
      setReturnToApp({ created, email });
      return;
    }
    await finishWebRouting(created);
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (appleBusy) return;
    setError("");

    if (password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }

    setLoading(true);
    const attemptEpoch = getAuthenticationEpoch();
    const attemptAccessToken = getAccessToken();
    try {
      const tokens = await signup(
        email,
        password,
        displayName || undefined,
        fromApp ? "ios_handoff" : undefined,
      );
      await finishAuthentication(
        { ...tokens, created: true },
        attemptEpoch,
        attemptAccessToken,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Signup failed.");
    } finally {
      setLoading(false);
    }
  };

  const inputClass =
    "mt-1 w-full rounded-xl border border-white/10 bg-white/[0.05] px-4 py-3 text-sm text-[#e0e3e8] outline-none placeholder:text-white/25 focus:border-[#5dd9d0]/50 focus:shadow-[0_0_8px_rgba(93,217,208,0.15)] transition";

  if (returnToApp) {
    return (
      <OnboardingShell>
        <div className="w-full max-w-[420px]">
          <div className="rounded-[24px] border border-white/[0.06] bg-[#12161b]/60 p-7 text-center shadow-[0_20px_60px_rgba(0,0,0,0.4)] backdrop-blur-xl sm:p-8">
            <h2 className="font-headline text-2xl font-bold tracking-tight text-[#e0e3e8]">
              Your account is ready
            </h2>
            <p className="mt-3 text-sm leading-relaxed text-white/45">
              Return to the NBHD app and tap &ldquo;Create my space&rdquo; again.{" "}
              {returnToApp.email ? (
                <>
                  You&apos;ll be offered &ldquo;Continue as {returnToApp.email}&rdquo; to
                  finish signing in.
                </>
              ) : (
                <>You&apos;ll be offered the option to continue with your account.</>
              )}
            </p>
            <button
              type="button"
              onClick={() => void finishWebRouting(returnToApp.created)}
              className="mt-6 min-h-[44px] text-sm font-medium text-white/60 underline transition-colors hover:text-white/80"
            >
              Continue on the web instead
            </button>
          </div>
        </div>
      </OnboardingShell>
    );
  }

  return (
    <OnboardingShell>
      <div className="w-full max-w-[420px]">
        {/* Glass card */}
        <div className="rounded-[24px] bg-[#12161b]/60 backdrop-blur-xl border border-white/[0.06] p-7 sm:p-8 shadow-[0_20px_60px_rgba(0,0,0,0.4)]">
          {/* Brand mark */}
          <div className="flex justify-center mb-6">
            <div className="flex h-10 w-10 items-center justify-center rounded-full border border-[#7C6BF0]/30 bg-[#7C6BF0]/20 shadow-[0_0_20px_rgba(124,107,240,0.3)]">
              <svg viewBox="0 0 24 24" fill="none" className="h-5 w-5 text-[#c7bfff]">
                <path
                  d="M12 2L13.09 8.26L18 4L14.74 9.91L21 10L14.74 12.09L18 18L13.09 13.74L12 20L10.91 13.74L6 18L9.26 12.09L3 10L9.26 9.91L6 4L10.91 8.26L12 2Z"
                  fill="currentColor"
                />
              </svg>
            </div>
          </div>

          <h2 className="text-center text-2xl font-bold text-[#e0e3e8] tracking-tight">
            Begin your journey
          </h2>
          <p className="mt-2 text-center text-sm text-white/45 leading-relaxed">
            Your private AI companion, in the{" "}
            <span className="text-white/65">NBHD app</span>. 30-day free trial.
          </p>

          <form onSubmit={handleSubmit} className="mt-7 space-y-4">
            <div>
              <label htmlFor="displayName" className="block font-mono text-[10px] uppercase tracking-[0.14em] text-white/40">
                What should your assistant call you?
              </label>
              <input
                id="displayName"
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                className={inputClass}
                placeholder="Your name"
              />
            </div>

            <div>
              <label htmlFor="email" className="block font-mono text-[10px] uppercase tracking-[0.14em] text-white/40">
                Email
              </label>
              <input
                id="email"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className={inputClass}
                placeholder="you@example.com"
              />
            </div>

            <div>
              <label htmlFor="password" className="block font-mono text-[10px] uppercase tracking-[0.14em] text-white/40">
                Password
              </label>
              <input
                id="password"
                type="password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className={inputClass}
                placeholder="Create a password"
              />
              <PasswordStrengthMeter password={password} />
            </div>

            <div>
              <label htmlFor="confirmPassword" className="block font-mono text-[10px] uppercase tracking-[0.14em] text-white/40">
                Confirm Password
              </label>
              <input
                id="confirmPassword"
                type="password"
                required
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className={inputClass}
                placeholder="Confirm your password"
              />
            </div>

            {error && (
              <p className="rounded-xl border border-rose-500/20 bg-rose-500/10 px-4 py-2.5 text-sm text-rose-300">
                {error}
              </p>
            )}

            <button
              type="submit"
              disabled={loading || appleBusy}
              className="glow-purple w-full rounded-full bg-[#7C6BF0] px-4 py-3 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {loading ? "Creating account..." : "Create account"}
            </button>
          </form>

          <AppleSignInButton
            flow="authenticate"
            disabled={loading}
            onAuthenticated={finishAuthentication}
            onBusyChange={setAppleBusy}
            showDivider
          />

          <p className="mt-5 text-center text-[11px] text-white/25 leading-relaxed">
            By creating an account, you agree to our{" "}
            <Link href="/legal/terms" className="underline hover:text-white/40">Terms of Service</Link>{" "}
            and{" "}
            <Link href="/legal/privacy" className="underline hover:text-white/40">Privacy Policy</Link>.
          </p>
        </div>

        <p className="mt-6 text-center text-sm text-white/40">
          Already have an account?{" "}
          <Link href="/login" className="text-white/60 underline hover:text-white/80">Sign in</Link>
        </p>
      </div>
    </OnboardingShell>
  );
}

export default function SignupPage() {
  return (
    <Suspense fallback={null}>
      <SignupPageInner />
    </Suspense>
  );
}
