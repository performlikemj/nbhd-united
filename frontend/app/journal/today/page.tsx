"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { todayISO } from "@/lib/journal-date";

export default function TodayRedirect() {
  const router = useRouter();

  useEffect(() => {
    router.replace(`/journal#daily/${todayISO()}`);
  }, [router]);

  return null;
}
