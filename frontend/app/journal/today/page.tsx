"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function TodayRedirect() {
  const router = useRouter();

  useEffect(() => {
    const d = new Date();
    const today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    router.replace(`/journal#daily/${today}`);
  }, [router]);

  return null;
}
