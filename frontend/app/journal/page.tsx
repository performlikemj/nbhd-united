"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function JournalRedirect() {
  const router = useRouter();

  useEffect(() => {
    router.replace("/journal/today");
  }, [router]);

  return null;
}
