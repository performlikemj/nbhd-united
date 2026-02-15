"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function AutomationsRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/settings/automations");
  }, [router]);
  return null;
}
