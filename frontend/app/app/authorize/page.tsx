"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { OnboardingShell } from "@/components/onboarding/onboarding-shell";
import { getAccessToken, isLoggedIn } from "@/lib/auth";
import {
  AuthorizeParams,
  clearAuthorizeParams,
  isValidAuthorizeParams,
  parseAuthorizeParams,
  readAuthorizeParams,
  stashAuthorizeParams,
} from "@/lib/app-authorize";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

// The whole flow runs inside the iOS app's ASWebAuthenticationSession, which
// intercepts the nbhd:// scheme and hands the URL to the app's completion
// handler. In a plain browser this navigation is simply a no-op.
function redirectToApp(query: string) {
  window.location.href = `nbhd://auth/callback?${query}`;
}

export default function AppAuthorizePage() {
  const router = useRouter();
  const ran = useRef(false);
  const [message, setMessage] = useState("Connecting your account…");

  useEffect(() => {
    // Guard the StrictMode double-invoke in dev; in the static export this
    // effect runs once.
    if (ran.current) return;
    ran.current = true;

    async function completeHandoff(params: AuthorizeParams) {
      setMessage("Finishing sign-in…");
      const token = getAccessToken();
      if (!token) {
        // Should not happen — completeHandoff only runs when isLoggedIn().
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

    // Persist for the register/login round trip, then either finish now (the
    // user is already web-authed) or route into the existing auth UI.
    stashAuthorizeParams(params);
    if (isLoggedIn()) {
      void completeHandoff(params);
    } else {
      setMessage("Redirecting you to sign in…");
      router.replace(params.intent === "signin" ? "/login" : "/signup");
    }
  }, [router]);

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
          <h2 className="text-center text-2xl font-bold text-[#e0e3e8] tracking-tight">
            Almost there
          </h2>
          <p className="mt-3 text-center text-sm text-white/45 leading-relaxed" aria-live="polite">
            {message}
          </p>
        </div>
      </div>
    </OnboardingShell>
  );
}
