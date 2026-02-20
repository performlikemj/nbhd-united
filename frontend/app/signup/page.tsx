"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";

import { signup } from "@/lib/api";
import { setTokens } from "@/lib/auth";

export default function SignupPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const tokens = await signup(email, password, displayName || undefined);
      setTokens(tokens.access, tokens.refresh);
      router.push("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Signup failed.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <div className="w-full max-w-md rounded-panel border border-border bg-surface/90 p-8 shadow-panel animate-reveal">
        <p className="font-mono text-xs uppercase tracking-[0.24em] text-ink-muted">Neighborhood United</p>
        <h2 className="mt-2 text-2xl font-semibold text-ink">Create account</h2>
        <p className="mt-1 text-sm text-ink-muted">Sign up to get started with your private AI assistant.</p>

        <form onSubmit={handleSubmit} className="mt-6 space-y-4">
          <div>
            <label htmlFor="displayName" className="block font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">
              Display Name
            </label>
            <input
              id="displayName"
              type="text"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              className="mt-1 w-full rounded-panel border border-border bg-surface px-4 py-2.5 text-sm text-ink outline-none focus:border-border-strong"
              placeholder="Your name (optional)"
            />
          </div>

          <div>
            <label htmlFor="email" className="block font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">
              Email
            </label>
            <input
              id="email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1 w-full rounded-panel border border-border bg-surface px-4 py-2.5 text-sm text-ink outline-none focus:border-border-strong"
              placeholder="you@example.com"
            />
          </div>

          <div>
            <label htmlFor="password" className="block font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">
              Password
            </label>
            <input
              id="password"
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded-panel border border-border bg-surface px-4 py-2.5 text-sm text-ink outline-none focus:border-border-strong"
              placeholder="Create a password"
            />
          </div>

          {error && (
            <p className="rounded-panel border border-rose-border bg-rose-bg px-4 py-2.5 text-sm text-rose-text">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-full bg-accent px-4 py-2.5 text-sm font-medium text-white transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-55"
          >
            {loading ? "Creating account..." : "Create account"}
          </button>
        </form>

        <p className="mt-4 text-center text-xs text-ink-faint">
          By creating an account, you agree to our{" "}
          <Link href="/legal/terms" className="underline hover:text-ink-muted">
            Terms of Service
          </Link>{" "}
          and{" "}
          <Link href="/legal/privacy" className="underline hover:text-ink-muted">
            Privacy Policy
          </Link>
          .
        </p>

        <p className="mt-6 text-center text-sm text-ink-muted">
          Already have an account?{" "}
          <Link href="/login" className="text-ink underline hover:text-ink-muted">
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
