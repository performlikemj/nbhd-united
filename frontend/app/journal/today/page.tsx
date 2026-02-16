"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function TodayRedirect() {
  const router = useRouter();

  useEffect(() => {
    const today = new Date().toISOString().slice(0, 10);
    router.replace(`/journal#daily/${today}`);
  }, [router]);

  return null;
}
