"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function BillingRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/settings/billing");
  }, [router]);
  return null;
}
