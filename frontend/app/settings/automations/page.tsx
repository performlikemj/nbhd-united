"use client";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function SettingsAutomationsRedirect() {
  const router = useRouter();
  useEffect(() => { router.replace("/settings/cron-jobs"); }, [router]);
  return null;
}
