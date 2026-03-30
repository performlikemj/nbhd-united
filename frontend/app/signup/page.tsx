"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";

import { fetchMe, signup } from "@/lib/api";
import { setTokens } from "@/lib/auth";
import { OnboardingShell } from "@/components/onboarding/onboarding-shell";
import { PasswordStrengthMeter } from "@/components/onboarding/password-strength-meter";

export default function SignupPage() {
  const router = useRouter();
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
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
    try {
      const tokens = await signup(email, password, displayName || undefined);
      setTokens(tokens.access, tokens.refresh);
      try {
        const me = await fetchMe();
        const isOnboardingNeeded = !me.tenant || me.tenant.status !== "active" || !me.tenant.user.telegram_chat_id;
        router.push(isOnboardingNeeded ? "/onboarding" : "/journal");
      } catch {
        router.push("/onboarding");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Signup failed.");
    } finally {
      setLoading(false);
    }
  };

  const inputClass =
    "mt-1 w-full rounded-xl border border-white/10 bg-white/[0.05] px-4 py-3 text-sm text-[#e0e3e8] outline-none placeholder:text-white/25 focus:border-[#5dd9d0]/50 focus:shadow-[0_0_8px_rgba(93,217,208,0.15)] transition";

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
            Your private AI companion, delivered through{" "}
            <span className="text-white/65">Telegram</span> or{" "}
            <span className="text-white/65">LINE</span>. 7-day free trial.
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
              disabled={loading}
              className="glow-purple w-full rounded-full bg-[#7C6BF0] px-4 py-3 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {loading ? "Creating account..." : "Create account"}
            </button>
          </form>

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
