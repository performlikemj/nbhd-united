"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function UsageRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/settings/usage");
  }, [router]);
  return null;
}
