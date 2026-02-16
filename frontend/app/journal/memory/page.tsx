"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function MemoryRedirect() {
  const router = useRouter();

  useEffect(() => {
    router.replace("/journal#memory/memory");
  }, [router]);

  return null;
}
