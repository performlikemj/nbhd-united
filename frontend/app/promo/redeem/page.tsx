"use client";

import { Suspense, useEffect } from "react";
import { useSearchParams } from "next/navigation";

import { OnboardingShell } from "@/components/onboarding/onboarding-shell";

/**
 * Promo redemption bounce page.
 *
 * The campaign email links here ({FRONTEND_URL}/promo/redeem?code=&token=)
 * so the visible link stays on-brand. This static page reads the code+token
 * and forwards to the BACKEND endpoint (/api/v1/tenants/promos/redeem/), which
 * verifies the HMAC, applies the trial extension, then 302s to
 * /promo/redeemed?status=. Without this hop the email link would land on the
 * SPA navigationFallback (no such route) and silently never redeem.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

function PromoRedeemInner() {
  const params = useSearchParams();

  useEffect(() => {
    const code = (params.get("code") || "").trim();
    const token = (params.get("token") || "").trim();
    if (!code || !token) {
      window.location.replace("/promo/redeemed?status=invalid");
      return;
    }
    const qs = new URLSearchParams({ code, token }).toString();
    window.location.replace(`${API_BASE}/api/v1/tenants/promos/redeem/?${qs}`);
  }, [params]);

  return (
    <OnboardingShell>
      <div className="w-full max-w-[460px]">
        <div className="rounded-[24px] bg-[#12161b]/60 backdrop-blur-xl border border-white/[0.06] p-9 text-center text-white/70">
          <p className="text-[15px] leading-[1.65]">Redeeming your offer…</p>
        </div>
      </div>
    </OnboardingShell>
  );
}

export default function PromoRedeemPage() {
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
      <PromoRedeemInner />
    </Suspense>
  );
}
