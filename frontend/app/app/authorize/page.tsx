"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { OnboardingShell } from "@/components/onboarding/onboarding-shell";
import { clearTokens, getAccessToken, isLoggedIn } from "@/lib/auth";
import {
  AuthorizeParams,
  clearAuthorizeParams,
  isValidAuthorizeParams,
  parseAuthorizeParams,
  probeIdentity,
  readAuthorizeParams,
  stashAuthorizeParams,
} from "@/lib/app-authorize";
import {
  AuthorizeStep,
  decideAfterProbe,
  decideInitialStep,
  stepForDifferentAccount,
} from "@/lib/authorize-decision";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

// `working` covers every spinner state (verifying, probing, redirecting,
// finishing); `choose` shows the account-confirmation screen.
type Stage = "working" | "choose";

// The whole flow runs inside the iOS app's ASWebAuthenticationSession, which
// intercepts the nbhd:// scheme and hands the URL to the app's completion
// handler. In a plain browser this navigation is simply a no-op.
function redirectToApp(query: string) {
  window.location.href = `nbhd://auth/callback?${query}`;
}

export default function AppAuthorizePage() {
  const router = useRouter();
  const ran = useRef(false);
  const paramsRef = useRef<AuthorizeParams | null>(null);
  const [stage, setStage] = useState<Stage>("working");
  const [message, setMessage] = useState("Connecting your account…");
  const [email, setEmail] = useState("");

  // Mint the one-time code for the CURRENT browser session and redirect into the
  // app. Only ever reached for a session the user actively chose: a fresh
  // signup/login bounce-back, or an explicit "Continue as <email>".
  async function completeHandoff(params: AuthorizeParams) {
    setStage("working");
    setMessage("Finishing sign-in…");
    const token = getAccessToken();
    if (!token) {
      clearAuthorizeParams();
      redirectToApp("error=server_error");
      return;
    }
    try {
      const res = await fetch(`${API_BASE}/api/v1/auth/authorize/`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          code_challenge: params.codeChallenge,
          code_challenge_method: "S256",
          redirect_uri: params.redirectUri,
          state: params.state,
          client: "ios",
        }),
      });
      if (!res.ok) throw new Error(`authorize ${res.status}`);
      const data = (await res.json()) as { code?: string };
      if (!data.code) throw new Error("no code in response");
      clearAuthorizeParams();
      // `state` is the app's own nonce — echo it back byte-identical (it is
      // base64url, so encodeURIComponent is a no-op but keeps us safe).
      redirectToApp(
        `code=${encodeURIComponent(data.code)}&state=${encodeURIComponent(params.state)}`,
      );
    } catch {
      clearAuthorizeParams();
      redirectToApp("error=server_error");
    }
  }

  async function runStep(step: AuthorizeStep, params: AuthorizeParams) {
    switch (step.kind) {
      case "finish":
        await completeHandoff(params);
        return;
      case "redirect-auth":
        // `clearFirst` performs a local logout (this browser's tokens only) of a
        // dead or explicitly-rejected leftover session before routing.
        if (step.clearFirst) clearTokens();
        setStage("working");
        setMessage(
          step.target === "/login"
            ? "Redirecting you to sign in…"
            : "Redirecting you to sign up…",
        );
        router.replace(step.target);
        return;
      case "choose-account":
        setEmail(step.email);
        setStage("choose");
        return;
      case "probe-identity": {
        setStage("working");
        setMessage("Connecting your account…");
        const identity = await probeIdentity();
        await runStep(decideAfterProbe(identity, params.intent), params);
        return;
      }
    }
  }

  useEffect(() => {
    // Guard the StrictMode double-invoke in dev; in the static export this
    // effect runs once.
    if (ran.current) return;
    ran.current = true;

    // First entry carries the params in the URL; a bounce-back from
    // signup/login arrives with no query, so fall back to the stashed copy.
    const fromUrl = parseAuthorizeParams(window.location.search);
    if (fromUrl && !isValidAuthorizeParams(fromUrl)) {
      redirectToApp("error=invalid_request");
      return;
    }
    const params = fromUrl ?? readAuthorizeParams();
    if (!params || !isValidAuthorizeParams(params)) {
      redirectToApp("error=invalid_request");
      return;
    }

    // Persist for the register/login round trip, then decide what to do. A
    // leftover browser session is NEVER trusted blindly on a first hop — we
    // resolve whose it is and let the user confirm (the fix for new users
    // landing in a stale account). See authorize-decision.ts.
    stashAuthorizeParams(params);
    paramsRef.current = params;

    const step = decideInitialStep({
      firstHop: fromUrl !== null,
      isLoggedIn: isLoggedIn(),
      intent: params.intent,
    });
    void runStep(step, params);
    // Bootstrap runs exactly once (guarded by ran.current above); runStep /
    // completeHandoff are stable component closures we deliberately omit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [router]);

  function handleContinue() {
    const params = paramsRef.current;
    if (!params) return;
    void completeHandoff(params);
  }

  function handleUseDifferent() {
    const params = paramsRef.current;
    if (!params) return;
    void runStep(stepForDifferentAccount(params.intent), params);
  }

  const isRegister = paramsRef.current?.intent !== "signin";

  return (
    <OnboardingShell>
      <div className="w-full max-w-[420px]">
        <div className="rounded-[24px] bg-[#12161b]/60 backdrop-blur-xl border border-white/[0.06] p-7 sm:p-8 shadow-[0_20px_60px_rgba(0,0,0,0.4)]">
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

          {stage === "choose" ? (
            <div>
              <h2 className="text-center text-2xl font-bold text-[#e0e3e8] tracking-tight">
                {isRegister ? "You already have an account" : "You're already signed in"}
              </h2>
              <p className="mt-3 text-center text-sm text-white/45 leading-relaxed">
                Continue with this account, or use a different one.
              </p>

              <div className="mt-5 rounded-xl border border-white/10 bg-white/[0.04] px-4 py-3 text-center">
                <p className="font-mono text-[10px] uppercase tracking-[0.14em] text-white/35">
                  Signed in as
                </p>
                <p className="mt-1 truncate text-sm font-medium text-[#e0e3e8]" title={email}>
                  {email}
                </p>
              </div>

              <div className="mt-6 space-y-3">
                <button
                  type="button"
                  onClick={handleContinue}
                  className="glow-purple w-full rounded-full bg-[#7C6BF0] px-4 py-3 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98]"
                >
                  Continue
                </button>
                <button
                  type="button"
                  onClick={handleUseDifferent}
                  className="w-full rounded-full border border-white/15 bg-white/[0.04] px-4 py-3 text-sm font-semibold text-white/80 transition hover:bg-white/[0.08] active:scale-[0.98]"
                >
                  Use a different account
                </button>
              </div>
            </div>
          ) : (
            <>
              <h2 className="text-center text-2xl font-bold text-[#e0e3e8] tracking-tight">
                Almost there
              </h2>
              <p
                className="mt-3 text-center text-sm text-white/45 leading-relaxed"
                aria-live="polite"
              >
                {message}
              </p>
            </>
          )}
        </div>
      </div>
    </OnboardingShell>
  );
}
