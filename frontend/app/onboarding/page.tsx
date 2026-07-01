"use client";

import { useState } from "react";

import { OnboardingShell } from "@/components/onboarding/onboarding-shell";
import { PersonaScene } from "@/components/onboarding/persona-scene";
import { AppScene } from "@/components/onboarding/app-scene";
import { LaunchSequence } from "@/components/onboarding/launch-sequence";
import { SectionCardSkeleton } from "@/components/skeleton";
import { useMeQuery } from "@/lib/queries";

export default function OnboardingPage() {
  const { data: me, isLoading } = useMeQuery();
  const tenant = me?.tenant;
  const hasTenant = Boolean(tenant);
  // Local-only gate: the "get the app" step no longer requires linking a
  // messaging channel, so completion is a one-tap Continue rather than a
  // server-side flag. Reloading simply shows the step again — harmless.
  const [appStepDone, setAppStepDone] = useState(false);

  if (isLoading) {
    return (
      <OnboardingShell>
        <div className="w-full max-w-[580px]">
          <SectionCardSkeleton lines={6} />
        </div>
      </OnboardingShell>
    );
  }

  // Determine which scene to show: persona (no tenant) → app download → launch.
  let scene: "persona" | "app" | "launch";
  if (!hasTenant) {
    scene = "persona";
  } else if (!appStepDone) {
    scene = "app";
  } else {
    scene = "launch";
  }

  return (
    <OnboardingShell>
      {scene === "persona" && <PersonaScene />}
      {scene === "app" && <AppScene onContinue={() => setAppStepDone(true)} />}
      {scene === "launch" && <LaunchSequence />}
    </OnboardingShell>
  );
}
