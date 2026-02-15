"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";

function IntegrationsRedirectInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  useEffect(() => {
    const qs = searchParams.toString();
    router.replace(`/settings/integrations${qs ? `?${qs}` : ""}`);
  }, [router, searchParams]);
  return null;
}

export default function IntegrationsRedirect() {
  return (
    <Suspense fallback={null}>
      <IntegrationsRedirectInner />
    </Suspense>
  );
}
